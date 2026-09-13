"""Tests for music_gen_local._write_output (RFC §4.6 + §7.5).

Output format spec (RFC §4.6):

  - ``output_path`` ending in ``.wav`` → write WAV via ``soundfile``, no ffmpeg.
  - ``output_path`` ending in ``.mp3`` → write a tmp WAV, transcode with
    ffmpeg (``-codec:a libmp3lame -q:a 2``), then delete the tmp WAV.

These tests mock ``soundfile``, ``subprocess.run``, and ``tempfile`` so they
run without real audio dependencies. The numpy array itself is real (numpy
is a venv dep, not a new requirement).

NOTE: ``tools/audio/music_gen_local.py`` does not exist yet (this test file
is the contract — see RFC §6 step 1). Until that file lands, all tests in
this module are skipped via the ``pytestmark`` below. Once the tool is
implemented per RFC §4.6, the tests run and pin the output-format behaviour.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile as _tempfile
import types
from pathlib import Path

import numpy as np
import pytest

try:
    from tools.audio import music_gen_local as mgl

    _TOOL_AVAILABLE = True
except ImportError:  # pragma: no cover - tool not yet implemented (RFC §6)
    mgl = None  # type: ignore[assignment]
    _TOOL_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _TOOL_AVAILABLE,
    reason="tools.audio.music_gen_local not yet implemented (RFC §4.6 / §6 step 1)",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_soundfile() -> types.ModuleType:
    """Build a fake ``soundfile`` module whose ``sf.write`` records its args.

    The fake also creates a stub file on disk so callers can assert that
    ``output_path.exists()`` after a successful write.
    """

    fake = types.ModuleType("soundfile")

    def fake_write(file, data, samplerate, *args, **kwargs):  # noqa: ANN001, D401
        path = Path(str(file))
        path.parent.mkdir(parents=True, exist_ok=True)
        # Real soundfile writes binary WAV; we don't care about contents
        # for these tests — only that *something* was written.
        path.write_bytes(b"FAKE_WAV")

    fake.write = fake_write
    return fake


def _install_fake_soundfile(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """Install the fake ``soundfile`` into ``sys.modules``.

    The RFC spec imports ``soundfile`` lazily inside ``_write_output``:

        import soundfile as sf
        sf.write(...)

    Python's import statement checks ``sys.modules`` first, so patching it
    before calling the function makes the lazy import resolve to our fake.
    """

    fake = _make_fake_soundfile()
    monkeypatch.setitem(sys.modules, "soundfile", fake)
    return fake


def _install_fake_subprocess(
    monkeypatch: pytest.MonkeyPatch, captured: dict
) -> None:
    """Replace ``subprocess.run`` on the tool's module with a recording stub."""

    def fake_run(*args, **kwargs):  # noqa: ANN001
        # Match the real subprocess.run signature: positional cmd list OR
        # keyword ``args=``.
        if args:
            captured["args"] = list(args[0])
        else:
            captured["args"] = list(kwargs.get("args", []))
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=captured["args"], returncode=0
        )

    # Patch on the actual subprocess module so any reference inside the tool
    # (``subprocess.run`` at module scope, per RFC §4.6) is intercepted.
    monkeypatch.setattr(mgl.subprocess, "run", fake_run)


# ---------------------------------------------------------------------------
# §7.5 tests (names verbatim from RFC)
# ---------------------------------------------------------------------------


def test_wav_output_default(monkeypatch, tmp_path):
    """``output_path`` ends ``.wav`` → soundfile.write called, no ffmpeg."""
    _install_fake_soundfile(monkeypatch)
    captured: dict = {}
    _install_fake_subprocess(monkeypatch, captured)

    out = tmp_path / "music.wav"
    wav = np.zeros((32000,), dtype=np.float32)  # 1 second @ 32 kHz of silence

    mgl.MusicGenLocal()._write_output(wav, 32000, out)

    # File was written by soundfile (the fake created it on disk).
    assert out.exists(), "WAV output file was not created by soundfile"
    assert out.read_bytes() == b"FAKE_WAV"

    # No ffmpeg subprocess invocation.
    assert captured == {}, (
        f"subprocess.run must NOT be called for .wav output, got {captured!r}"
    )


def test_mp3_output_invokes_ffmpeg(monkeypatch, tmp_path):
    """``output_path`` ends ``.mp3`` → ffmpeg runs with libmp3lame / -q:a 2."""
    _install_fake_soundfile(monkeypatch)
    captured: dict = {}
    _install_fake_subprocess(monkeypatch, captured)

    out = tmp_path / "music.mp3"
    wav = np.zeros((32000,), dtype=np.float32)

    mgl.MusicGenLocal()._write_output(wav, 32000, out)

    cmd = captured.get("args", [])
    assert cmd, "ffmpeg was not invoked"
    assert cmd[0] == "ffmpeg", f"first argv element must be 'ffmpeg', got {cmd[0]!r}"
    assert "-codec:a" in cmd, f"argv missing -codec:a: {cmd!r}"
    assert "libmp3lame" in cmd, f"argv missing libmp3lame codec: {cmd!r}"
    assert "-q:a" in cmd, f"argv missing -q:a quality flag: {cmd!r}"

    # -q:a 2: the quality value immediately follows the flag.
    qa_idx = cmd.index("-q:a")
    assert cmd[qa_idx + 1] == "2", (
        f"-q:a quality must be '2' per RFC §4.6, got {cmd[qa_idx + 1]!r}"
    )


def test_mp3_output_cleans_up_tmp_wav(monkeypatch, tmp_path):
    """Tmp WAV sibling is deleted after a successful MP3 transcode.

    The RFC spec writes the WAV to a sibling tmp file then unlinks it on
    success. To observe this, we redirect ``tempfile.NamedTemporaryFile`` to
    write under ``tmp_path`` so the test can inspect what survives.
    """

    fake_sf = _install_fake_soundfile(monkeypatch)
    captured: dict = {}
    _install_fake_subprocess(monkeypatch, captured)

    # Track every file soundfile.write touches.
    written_paths: list[Path] = []

    def tracking_write(file, data, samplerate, *args, **kwargs):  # noqa: ANN001
        path = Path(str(file))
        written_paths.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"FAKE_WAV")

    fake_sf.write = tracking_write

    # Pin NamedTemporaryFile to a directory under tmp_path so we can glob it.
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir(exist_ok=True)
    original_namedtempfile = _tempfile.NamedTemporaryFile

    def redirected_namedtempfile(*args, **kwargs):  # noqa: ANN001
        kwargs["dir"] = str(tmp_dir)
        kwargs["delete"] = False
        return original_namedtempfile(*args, **kwargs)

    monkeypatch.setattr(_tempfile, "NamedTemporaryFile", redirected_namedtempfile)

    out = tmp_path / "music.mp3"
    wav = np.zeros((32000,), dtype=np.float32)

    mgl.MusicGenLocal()._write_output(wav, 32000, out)

    # ffmpeg ran (sanity).
    assert captured.get("args"), "ffmpeg was not invoked"

    # soundfile.write was called once — for the tmp WAV (RFC writes the tmp
    # WAV and then transcodes; the final mp3 is written by ffmpeg, not sf).
    assert written_paths, "soundfile.write was never called"

    # The tmp WAV must have been unlinked after the ffmpeg call succeeded.
    survivors = list(tmp_dir.glob("*.wav"))
    assert survivors == [], (
        f"tmp WAV sibling was not cleaned up after success: {survivors!r}"
    )
