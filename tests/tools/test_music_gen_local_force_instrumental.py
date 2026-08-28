"""Tests for ``MusicGenLocal.force_instrumental`` contract (RFC sec 7.3).

Source of truth: ``docs/music-gen-local-rfc-2026-08-28.md`` sec 7.3, with
sec 4.4 defining the semantics:

* ``force_instrumental=True`` (default) is a no-op; the prompt passes through
  the model verbatim. The mandate (``skills/creative/music-gen-usage.md``)
  is honored by being the default, not by injecting an "instrumental only"
  prefix into the prompt.
* ``force_instrumental=False`` is a hard failure with an error pointing the
  caller at ``suno_music`` — MusicGen has no vocal pathway.

These tests mock ``transformers.pipeline``, ``soundfile.write``, ``torch``,
and the HF cache root so they run on hosts that do not have the MusicGen
weights downloaded. The ``get_status()`` weight-cache gate is satisfied by
populating a fake ``HF_HOME`` with the expected snapshot marker directory.

Mirrors the structure of ``tests/tools/test_music_gen_force_instrumental.py``:
fake module injection via ``sys.modules`` + an ``installed_fakes`` helper
that records what the model was called with.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

# Conditional import: matches the pattern in
# ``tests/tools/test_music_gen_local_device.py``. If the tool has not yet
# landed on this branch, the whole module collapses to SKIPPED at collection
# time so pytest never errors.
try:
    from tools.audio.music_gen_local import MusicGenLocal  # noqa: F401

    _HAS_TOOL = True
except Exception:  # noqa: BLE001 - ImportError, ModuleNotFoundError, etc.
    MusicGenLocal = None  # type: ignore[assignment]
    _HAS_TOOL = False


pytestmark = pytest.mark.skipif(
    not _HAS_TOOL,
    reason=(
        "music_gen_local tool not yet implemented (RFC sec 7.3). "
        "Land tools/audio/music_gen_local.py to enable these tests."
    ),
)


# ---------------------------------------------------------------------------
# Fake-module helpers
# ---------------------------------------------------------------------------


def _make_fake_torch() -> types.ModuleType:
    """CPU-only ``torch`` stub. Avoids touching the real CUDA/MPS probes."""
    fake = types.ModuleType("torch")
    fake.cuda = SimpleNamespace(is_available=lambda: False)
    fake.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: False))
    return fake


def _make_fake_soundfile() -> types.ModuleType:
    """``soundfile`` stub whose ``write`` is a no-op (no disk side effects)."""
    fake = types.ModuleType("soundfile")

    def _no_write(*_args: Any, **_kwargs: Any) -> None:
        return None

    fake.write = _no_write
    return fake


def _make_fake_transformers(captured: dict) -> types.ModuleType:
    """``transformers`` stub. ``pipeline(...)`` returns a ``FakePipe`` that
    records the forward kwargs and yields a 1-second silent waveform.
    """
    fake = types.ModuleType("transformers")

    def fake_pipeline(task: Any = None, model: Any = None, **kwargs: Any) -> Any:
        captured["pipeline_init"] = {"task": task, "model": model, **kwargs}

        class FakePipe:
            def __call__(self, *args: Any, **kwargs: Any) -> Any:
                captured["pipe_call"] = {"args": args, "kwargs": kwargs}
                # Real transformers returns a list of dicts; mirror that.
                return [
                    {
                        "audio": __import__("numpy").zeros(
                            32_000, dtype="float32"
                        ),
                        "sampling_rate": 32_000,
                    }
                ]

        return FakePipe()

    fake.pipeline = fake_pipeline
    return fake


def _seed_hf_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> Path:
    """Populate a fake HF cache with the marker directory so ``get_status()``
    returns AVAILABLE. Returns the cache root path.
    """
    cache_root = tmp_path / "hf_cache"
    marker = cache_root / "hub" / "models--facebook--musicgen-small"
    marker.mkdir(parents=True)
    monkeypatch.setenv("HF_HOME", str(cache_root))
    return cache_root


def _install_fakes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> dict:
    """Wire all of the fakes (torch, soundfile, transformers, HF cache) and
    return the ``captured`` dict the FakePipe writes into.
    """
    captured: dict = {}
    monkeypatch.setitem(sys.modules, "torch", _make_fake_torch())
    monkeypatch.setitem(sys.modules, "soundfile", _make_fake_soundfile())
    monkeypatch.setitem(sys.modules, "transformers", _make_fake_transformers(captured))
    _seed_hf_cache(monkeypatch, tmp_path)
    return captured


def _all_strings(obj: Any) -> list:
    """Recursively flatten ``obj`` into the list of string leaves it contains.

    The pipeline call is a nested structure (positional list of prompts
    wrapped in a list, plus a ``generate_kwargs`` dict). Recursive
    flattening keeps the assertion agnostic to how the tool chooses to pass
    the prompt through.
    """
    if isinstance(obj, str):
        return [obj]
    if isinstance(obj, (list, tuple)):
        out: list = []
        for item in obj:
            out.extend(_all_strings(item))
        return out
    if isinstance(obj, dict):
        out = []
        for v in obj.values():
            out.extend(_all_strings(v))
        return out
    return []


# ---------------------------------------------------------------------------
# RFC sec 7.3 tests
# ---------------------------------------------------------------------------


def test_default_force_instrumental_true_passes_prompt_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Default call passes the prompt through verbatim to the model.

    No "instrumental only" prefix is injected — ``force_instrumental=True``
    is a no-op (RFC sec 4.4). The model is invoked with a payload that
    contains the input prompt string unchanged.
    """
    assert _HAS_TOOL
    captured = _install_fakes(monkeypatch, tmp_path)
    out = tmp_path / "bg.wav"

    result = MusicGenLocal().execute(
        {
            "prompt": "gentle ambient piano",
            "duration_seconds": 10,
            "output_path": str(out),
        }
    )

    assert result.success is True, (
        f"execute() returned failure: error={result.error!r}"
    )

    pipe_call = captured.get("pipe_call")
    assert pipe_call is not None, (
        "pipeline(...) was constructed but never invoked; "
        "execute() must call the pipeline on the default path."
    )

    all_strs = _all_strings(pipe_call["args"]) + _all_strings(pipe_call["kwargs"])

    # 1. The input prompt is forwarded to the model.
    assert "gentle ambient piano" in all_strs, (
        f"input prompt not forwarded to model; "
        f"args={pipe_call['args']!r} kwargs={pipe_call['kwargs']!r}"
    )

    # 2. No "instrumental only" forced prefix was injected (RFC sec 4.4).
    for s in all_strs:
        assert "instrumental only" not in s.lower(), (
            f"forced 'instrumental only' prefix detected in pipeline call: "
            f"{s!r}; force_instrumental=True must be a no-op."
        )


def test_force_instrumental_false_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """Explicit ``force_instrumental=False`` must hard-fail before the
    pipeline runs, and the error must point the caller at ``suno_music``.
    """
    assert _HAS_TOOL
    captured = _install_fakes(monkeypatch, tmp_path)
    out = tmp_path / "bg.wav"

    result = MusicGenLocal().execute(
        {
            "prompt": "lead vocal pop",
            "duration_seconds": 10,
            "force_instrumental": False,
            "output_path": str(out),
        }
    )

    assert result.success is False, (
        "force_instrumental=False must produce success=False; "
        f"got {result!r}"
    )
    assert "suno_music" in (result.error or ""), (
        f"error must mention suno_music (caller-routing hint per RFC sec 4.4); "
        f"got: {result.error!r}"
    )

    # Per RFC sec 4.4 this is a hard fail BEFORE the pipeline call. Pin the
    # ordering: the pipeline must not have been invoked, so a vocal prompt
    # can never silently collapse to instrumental.
    assert "pipe_call" not in captured, (
        "pipeline was invoked even though force_instrumental=False should "
        "short-circuit before model loading."
    )


def test_schema_default_is_true() -> None:
    """Schema parity with ``music_gen``: ``force_instrumental`` defaults True.

    ``input_schema["properties"]["force_instrumental"]["default"] is True``
    is the literal assertion called out in RFC sec 7.3 row 3.
    """
    assert _HAS_TOOL
    props = MusicGenLocal().input_schema["properties"]["force_instrumental"]
    assert props["type"] == "boolean"
    assert props["default"] is True, (
        f"force_instrumental default must be True (mirrors music_gen); "
        f"got default={props['default']!r}"
    )
