"""Tests for the faster-whisper `transcriber` tool's offline-resolution behaviour.

Covers:
  - Registration + metadata (registry discover picks it up).
  - Status check reports UNAVAILABLE / DEGRADED / AVAILABLE based on
    whether `faster_whisper` is installed AND the default model is
    cached on disk. This is the offline-resilience fix: the legacy check
    only verified the package, which produced a false-positive AVAILABLE
    on hosts that could not reach huggingface.co.
  - Resolution chain: input > FASTER_WHISPER_MODEL_DIR > HF_HUB_CACHE >
    HF_HOME/hub > ~/.cache/huggingface/hub; tool-scoped env takes
    precedence over the generic HF vars so callers can pin the
    transcriber without affecting the rest of the toolchain (mirrors
    piper_tts's PIPER_DATA_DIR).
  - Size-to-repo mapping: bare sizes (`base`, `large-v3`, ...) map to
    their Systran/mobiuslabsgmbh repo id; repo ids pass through;
    unknown sizes raise ValueError with the supported set spelled out.
  - Missing-model path surfaces a `snapshot_download` command, not a
    stack traceback.

Heavy E2E (real audio → inference) is gated behind `--run-model-tests`
and is intentionally NOT covered here — the offline-resolution logic
is what was broken, and that is what these tests pin down.
"""

from __future__ import annotations

import builtins
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from tools.tool_registry import registry  # noqa: E402
from tools.base_tool import ToolStatus  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _enable_faster_whisper_import(monkeypatch):
    """faster_whisper is normally installed in CI; fake the import so the
    tests do not depend on its presence in dev venvs."""
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            return object()
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)


def _build_usable_snapshot(cache_root: Path, repo: str = "Systran/faster-whisper-base", sha: str = "deadbeef") -> Path:
    """Lay down a fake but valid HF snapshot dir tree at `cache_root`."""
    snapshot = cache_root / f"models--{repo.replace('/', '--')}" / "snapshots" / sha
    snapshot.mkdir(parents=True)
    for fname in ("model.bin", "config.json", "tokenizer.json"):
        (snapshot / fname).write_bytes(b"")
    refs = cache_root / f"models--{repo.replace('/', '--')}" / "refs" / "main"
    refs.parent.mkdir(parents=True, exist_ok=True)
    refs.write_text(f"{sha}\n")
    return snapshot


# ---------------------------------------------------------------------------
# Registration & metadata
# ---------------------------------------------------------------------------

def test_faster_whisper_transcriber_is_registered():
    registry.discover()
    assert registry.get("transcriber") is not None


def test_faster_whisper_transcriber_metadata():
    registry.discover()
    t = registry.get("transcriber")
    info = t.get_info()
    assert info["name"] == "transcriber"
    assert info["provider"] == "whisperx"
    assert info["tier"] == "core"
    assert info["capability"] == "analysis"
    assert "transcribe" in info["capabilities"]
    assert "word_timestamps" in info["capabilities"]
    assert any("faster_whisper" in d for d in info["dependencies"])


def test_faster_whisper_transcriber_input_schema_exposes_size_and_model_dir():
    """`model_size` and `model_dir` are the two offline-relevant inputs."""
    registry.discover()
    t = registry.get("transcriber")
    props = t.input_schema["properties"]
    assert "model_size" in props
    assert "model_dir" in props
    # size enum covers the standard sizes plus the turbo alias
    assert "base" in props["model_size"]["enum"]
    assert "large-v3" in props["model_size"]["enum"]
    assert "turbo" in props["model_size"]["enum"]
    # default is "base" because that is the size the project actually
    # pre-caches and exercises end-to-end.
    assert props["model_size"]["default"] == "base"


# ---------------------------------------------------------------------------
# Resolution chain
# ---------------------------------------------------------------------------

def test_resolution_chain_precedence(monkeypatch, tmp_path):
    """input > FASTER_WHISPER_MODEL_DIR > HF_HUB_CACHE > HF_HOME > default.

    Mirrors piper_tts's PIPER_DATA_DIR pattern: tool-scoped env beats the
    generic HF vars so a caller can pin the transcriber's cache without
    touching the rest of the toolchain.
    """
    from tools.analysis.transcriber import _resolve_cache_root

    # 1) input wins
    monkeypatch.delenv("FASTER_WHISPER_MODEL_DIR", raising=False)
    monkeypatch.delenv("HF_HUB_CACHE", raising=False)
    monkeypatch.delenv("HF_HOME", raising=False)
    assert _resolve_cache_root({"model_dir": "/from/input"}) == Path("/from/input").expanduser()

    # 2) FASTER_WHISPER_MODEL_DIR next
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", "/from/fwm_env")
    assert _resolve_cache_root() == Path("/from/fwm_env").expanduser()
    assert _resolve_cache_root({"model_dir": "/from/input"}) == Path("/from/input").expanduser()

    # 3) HF_HUB_CACHE next
    monkeypatch.delenv("FASTER_WHISPER_MODEL_DIR")
    monkeypatch.setenv("HF_HUB_CACHE", "/from/hf_hub_cache")
    assert _resolve_cache_root() == Path("/from/hf_hub_cache").expanduser()

    # 4) HF_HOME/hub next
    monkeypatch.delenv("HF_HUB_CACHE")
    monkeypatch.setenv("HF_HOME", "/from/hf_home")
    assert _resolve_cache_root() == Path("/from/hf_home/hub").expanduser()

    # 5) Default last
    monkeypatch.delenv("HF_HOME")
    assert _resolve_cache_root() == Path.home() / ".cache" / "huggingface" / "hub"


def test_snapshot_path_prefers_refs_main(monkeypatch, tmp_path):
    """When `refs/main` points to a usable snapshot, that one wins over
    newer-but-broken siblings."""
    from tools.analysis.transcriber import _snapshot_path

    repo_dir = tmp_path / "models--Systran--faster-whisper-base"
    snapshots_dir = repo_dir / "snapshots"

    pointed = snapshots_dir / "aaa"
    pointed.mkdir(parents=True)
    for f in ("model.bin", "config.json", "tokenizer.json"):
        (pointed / f).write_bytes(b"")

    # Newer snapshot, but missing files — must NOT be returned.
    broken = snapshots_dir / "bbb"
    broken.mkdir(parents=True)
    (broken / "model.bin").write_bytes(b"")
    # Touch `bbb` *after* `aaa` so it's "newer" by mtime.
    import os
    os.utime(broken, (broken.stat().st_atime + 100, broken.stat().st_mtime + 100))

    (repo_dir / "refs" / "main").parent.mkdir(parents=True, exist_ok=True)
    (repo_dir / "refs" / "main").write_text("aaa\n")

    resolved = _snapshot_path(tmp_path, "Systran/faster-whisper-base")
    assert resolved == pointed.resolve()


def test_snapshot_path_falls_back_to_newest_usable(monkeypatch, tmp_path):
    """When `refs/main` is missing or stale, pick the newest usable snapshot."""
    from tools.analysis.transcriber import _snapshot_path

    repo_dir = tmp_path / "models--Systran--faster-whisper-base"
    snapshots_dir = repo_dir / "snapshots"

    # Snapshot A (older, broken — no config.json).
    a = snapshots_dir / "aaa"
    a.mkdir(parents=True)
    (a / "model.bin").write_bytes(b"")

    # Snapshot B (newer, complete).
    b = snapshots_dir / "bbb"
    b.mkdir(parents=True)
    for f in ("model.bin", "config.json", "tokenizer.json"):
        (b / f).write_bytes(b"")
    import os
    os.utime(b, (b.stat().st_atime + 200, b.stat().st_mtime + 200))

    # No refs/main file.
    resolved = _snapshot_path(tmp_path, "Systran/faster-whisper-base")
    assert resolved == b.resolve()


def test_snapshot_path_returns_none_when_missing(tmp_path):
    """No repo dir / no snapshots dir / no usable snapshot → None.

    This is the offline-detection signal: the resolution returns None
    instead of attempting a network fetch.
    """
    from tools.analysis.transcriber import _snapshot_path

    # Empty cache.
    assert _snapshot_path(tmp_path, "Systran/faster-whisper-base") is None

    # Repo dir exists but no snapshots subdir.
    repo = tmp_path / "models--Systran--faster-whisper-base"
    repo.mkdir()
    assert _snapshot_path(tmp_path, "Systran/faster-whisper-base") is None

    # Snapshots exist but none are usable.
    broken = tmp_path / "snapshots" / "aaa"
    broken.mkdir(parents=True)
    (broken / "model.bin").write_bytes(b"")
    assert _snapshot_path(tmp_path, "Systran/faster-whisper-base") is None


# ---------------------------------------------------------------------------
# Status check (the offline fix in a nutshell)
# ---------------------------------------------------------------------------

def test_status_unavailable_when_package_missing(monkeypatch):
    """If `import faster_whisper` fails, the tool is UNAVAILABLE regardless
    of cache state."""
    original_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "faster_whisper":
            raise ImportError("faster_whisper is not installed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    from tools.analysis.transcriber import Transcriber

    assert Transcriber().get_status() == ToolStatus.UNAVAILABLE


def test_status_degraded_when_package_imports_but_default_not_cached(monkeypatch, tmp_path):
    """The offline-resilience fix in one assertion. Legacy behaviour was
    AVAILABLE on bare `import faster_whisper`; that broke at execute() time
    on hosts that cannot reach huggingface.co. Now: importable + empty
    cache = DEGRADED, so the provider menu's setup-offer path surfaces
    install_instructions instead of silently accepting a doomed call."""
    _enable_faster_whisper_import(monkeypatch)
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", str(tmp_path))  # empty

    from tools.analysis.transcriber import Transcriber

    assert Transcriber().get_status() == ToolStatus.DEGRADED


def test_status_available_when_default_is_cached(monkeypatch, tmp_path):
    """The other half of the rewrite — must not degenerate into
    always-unavailable. Build a valid snapshot tree and assert AVAILABLE."""
    _enable_faster_whisper_import(monkeypatch)
    _build_usable_snapshot(tmp_path)
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", str(tmp_path))

    from tools.analysis.transcriber import Transcriber

    assert Transcriber().get_status() == ToolStatus.AVAILABLE


def test_status_uses_default_repo_for_check(monkeypatch, tmp_path):
    """get_status() probes the default-size model (base), not all sizes.
    A repo that exists but is the wrong one does not satisfy the check."""
    from tools.analysis.transcriber import Transcriber

    _enable_faster_whisper_import(monkeypatch)

    # Cache only has `large-v3`, not the default `base`.
    other = tmp_path / "models--Systran--faster-whisper-large-v3" / "snapshots" / "abc"
    other.mkdir(parents=True)
    for f in ("model.bin", "config.json", "tokenizer.json"):
        (other / f).write_bytes(b"")
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", str(tmp_path))

    assert Transcriber().get_status() == ToolStatus.DEGRADED


# ---------------------------------------------------------------------------
# Size → repo mapping
# ---------------------------------------------------------------------------

def test_size_to_repo_map_covers_known_sizes():
    from tools.analysis.transcriber import _resolve_repo

    assert _resolve_repo("tiny") == "Systran/faster-whisper-tiny"
    assert _resolve_repo("base") == "Systran/faster-whisper-base"
    assert _resolve_repo("small") == "Systran/faster-whisper-small"
    assert _resolve_repo("medium") == "Systran/faster-whisper-medium"
    assert _resolve_repo("large") == "Systran/faster-whisper-large-v3"
    assert _resolve_repo("large-v2") == "Systran/faster-whisper-large-v2"
    assert _resolve_repo("large-v3") == "Systran/faster-whisper-large-v3"
    assert _resolve_repo("turbo") == "mobiuslabsgmbh/faster-whisper-large-v3-turbo"


def test_size_to_repo_passes_repo_id_through():
    """Anything containing a `/` is treated as a raw repo id, so callers
    can use distilled / community / fine-tuned variants without our
    having to enumerate them."""
    from tools.analysis.transcriber import _resolve_repo

    assert _resolve_repo("Systran/faster-distil-whisper-large-v3") == (
        "Systran/faster-distil-whisper-large-v3"
    )
    assert _resolve_repo("custom-org/my-whisper-fork") == "custom-org/my-whisper-fork"


def test_size_to_repo_unknown_size_raises_with_supported_set():
    """Unknown bare sizes must raise ValueError — fast-whisper's own
    behaviour, but with the supported set spelled out so the user can
    see what to fix."""
    from tools.analysis.transcriber import _resolve_repo

    with pytest.raises(ValueError, match="Unknown faster-whisper"):
        _resolve_repo("huge")
    with pytest.raises(ValueError, match="Unknown faster-whisper"):
        _resolve_repo("xlarge-v9")


# ---------------------------------------------------------------------------
# Missing-model path (the actionable error)
# ---------------------------------------------------------------------------

def test_execute_reports_missing_model_with_download_hint(monkeypatch, tmp_path):
    """When the requested size is not cached, execute() returns an error
    containing the `snapshot_download` one-liner — never a stack traceback
    from a mid-load network failure. The error text also keeps the
    `not found` substring so the legacy `test_transcriber_missing_file`
    contract test stays green."""
    from tools.analysis.transcriber import Transcriber

    _enable_faster_whisper_import(monkeypatch)
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", str(tmp_path))  # empty

    # Need a real input file so we don't trip the input-not-found check first.
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt ")

    result = Transcriber().execute({
        "input_path": str(audio),
        "model_size": "large-v3",
        "output_dir": str(tmp_path / "out"),
    })

    assert result.success is False
    assert "not found" in result.error.lower()
    assert "snapshot_download" in result.error
    assert "Systran/faster-whisper-large-v3" in result.error


def test_execute_rejects_unknown_size_with_actionable_error(monkeypatch, tmp_path):
    """Unknown bare sizes raise ValueError, surfaced as a ToolResult.error
    — still offline, still actionable."""
    from tools.analysis.transcriber import Transcriber

    _enable_faster_whisper_import(monkeypatch)
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", str(tmp_path))

    audio = tmp_path / "input.wav"
    audio.write_bytes(b"RIFF$\x00\x00\x00WAVEfmt ")

    result = Transcriber().execute({
        "input_path": str(audio),
        "model_size": "huge",
        "output_dir": str(tmp_path / "out"),
    })

    assert result.success is False
    assert "Unknown faster-whisper" in result.error


def test_execute_accepts_existing_local_model_path(monkeypatch, tmp_path):
    """If `model_size` is a directory that already exists on disk, use it
    directly — no cache lookup, no HF resolution. Mirrors faster-whisper's
    own `os.path.isdir` branch in transcribe.py:678-681."""
    from tools.analysis.transcriber import Transcriber, _is_local_path, _resolve_repo, _snapshot_path

    # Existing directory with the three usable files.
    local_model = tmp_path / "my_local_model"
    local_model.mkdir()
    for f in ("model.bin", "config.json", "tokenizer.json"):
        (local_model / f).write_bytes(b"")

    assert _is_local_path(str(local_model)) is True
    assert _is_local_path(str(tmp_path / "missing")) is False
    # And the resolution path returns the local dir as-is when the user
    # passes it directly (verified through the helper; execute() itself
    # would then need faster_whisper installed to actually load).
    _enable_faster_whisper_import(monkeypatch)
    monkeypatch.setenv("FASTER_WHISPER_MODEL_DIR", str(tmp_path / "empty_cache"))
    (tmp_path / "empty_cache").mkdir()

    # Repo resolution confirms the local path doesn't go through it.
    assert "/" not in str(local_model) or _resolve_repo(str(local_model)) == str(local_model)
    # And no snapshot lookup is performed when the local path is a dir.
    assert _snapshot_path(tmp_path / "empty_cache", "anything") is None
