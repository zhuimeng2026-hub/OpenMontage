"""RFC §7.1 — get_status() honesty contract for ``music_gen_local``.

Pins the three honest gates from RFC §4.5:

- ``ToolStatus.UNAVAILABLE`` + reason mentions "weights not cached" when the
  HuggingFace cache has no ``facebook/musicgen-small`` snapshot.
- ``ToolStatus.UNAVAILABLE`` + reason names the missing dep when transformers
  cannot be imported.
- ``ToolStatus.AVAILABLE`` only when transformers + torch + soundfile are
  importable AND the ``models--facebook--musicgen-small`` marker exists.
- ``get_status()`` never invokes ``transformers.pipeline(...)`` — RFC §4.5
  forbids silent first-call downloads (the Piper 1.4.2 → 1.7.0 trap).

The suite is hermetic: ``transformers`` / ``torch`` / ``soundfile`` are
mocked at ``sys.modules`` so it runs on hosts without those ML packages
(which is most CI environments).
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Tool under test. The RFC defines this tool; whether the implementation
# file is on disk at test time depends on the surrounding repo state.
# ---------------------------------------------------------------------------

try:
    from tools.audio.music_gen_local import MusicGenLocal  # noqa: E402

    _TOOL_AVAILABLE = True
except Exception:  # noqa: BLE001 - ImportError, ModuleNotFoundError, etc.
    MusicGenLocal = None  # type: ignore[assignment,misc]
    _TOOL_AVAILABLE = False


pytestmark = pytest.mark.skipif(
    not _TOOL_AVAILABLE,
    reason=(
        "tools.audio.music_gen_local not yet implemented (RFC §6 step 1). "
        "Land the tool file to enable these tests."
    ),
)


# ---------------------------------------------------------------------------
# Fake-module helpers — keep the suite hermetic w.r.t. heavy ML deps.
# ---------------------------------------------------------------------------


class _PipelineSpy:
    """Records every call to ``transformers.pipeline`` and raises if invoked.

    RFC §4.5 ("No silent download") forbids ``pipeline(...)`` during status
    checks. We enforce that by making any invocation raise an ``AssertionError``
    so the test either observes zero calls (good) or catches the error as
    proof that the contract was violated.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        raise AssertionError(
            "transformers.pipeline() was invoked during get_status() - "
            "RFC sec 4.5 forbids this; status checks must never trigger "
            "model downloads or loads."
        )


def _install_fake_deps(monkeypatch: pytest.MonkeyPatch) -> _PipelineSpy:
    """Install fake transformers / torch / soundfile; return the pipeline spy."""
    spy = _PipelineSpy()

    # transformers — module with `.pipeline` attribute bound to the spy so
    # `from transformers import pipeline` resolves to a callable that records
    # every invocation.
    fake_transformers = types.ModuleType("transformers")
    fake_transformers.pipeline = spy  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    # Stub the dotted submodule so `from transformers.pipeline import X`
    # never falls back to a real loader.
    monkeypatch.setitem(
        sys.modules, "transformers.pipeline", types.ModuleType("transformers.pipeline")
    )

    # torch — minimal stub. get_status() only needs the import to succeed;
    # _device() (called elsewhere) consumes cuda.is_available / mps.is_available.
    fake_torch = types.ModuleType("torch")
    fake_torch.cuda = types.SimpleNamespace(is_available=lambda: False)
    fake_torch.backends = types.SimpleNamespace(
        mps=types.SimpleNamespace(is_available=lambda: False)
    )
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    # soundfile — empty module; get_status() only imports it.
    monkeypatch.setitem(sys.modules, "soundfile", types.ModuleType("soundfile"))

    return spy


def _mark_transformers_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force ``import transformers`` to fail.

    Per CPython's import machinery, a ``None`` entry in ``sys.modules`` for a
    given name causes any subsequent ``import <name>`` to raise ImportError
    — so this single key is enough to make both ``import transformers`` and
    ``from transformers import pipeline`` fail consistently. Import order
    is irrelevant: regardless of whether torch or soundfile is checked
    first, the failure path is reached.
    """
    monkeypatch.setitem(sys.modules, "transformers", None)


def _hf_cache_with_marker(tmp_path: Path) -> Path:
    """Populate tmp_path with the MusicGen snapshot marker.

    RFC §4.5 inspects ``HF_HOME/hub/models--facebook--musicgen-small``
    (HuggingFace's standard cache layout). The task spec also references a
    flat ``tmp_path/models--facebook--musicgen-small`` path. We create both
    so the test passes regardless of which layout the implementation
    expects.
    """
    (tmp_path / "models--facebook--musicgen-small").mkdir(parents=True)
    (tmp_path / "hub" / "models--facebook--musicgen-small").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Tests — names verbatim from RFC §7.1
# ---------------------------------------------------------------------------


def test_status_unavailable_when_weights_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Empty HF cache → UNAVAILABLE; reason mentions "weights not cached"."""
    _install_fake_deps(monkeypatch)
    # Empty cache dir - no MusicGen snapshot present.
    monkeypatch.setenv("HF_HOME", str(tmp_path))

    result = MusicGenLocal().get_status()

    assert result.status == "unavailable"
    assert "weights not cached" in result.reason


def test_status_unavailable_when_transformers_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``sys.modules['transformers'] = None`` → UNAVAILABLE; reason names the dep."""
    _mark_transformers_missing(monkeypatch)
    monkeypatch.setenv("HF_HOME", str(tmp_path))

    result = MusicGenLocal().get_status()

    assert result.status == "unavailable"
    # RFC §4.5: reason is "missing dependency: {e}" where e names the failing module.
    assert "transformers" in result.reason


def test_status_available_with_cached_weights(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Pre-populated HF cache → AVAILABLE."""
    _install_fake_deps(monkeypatch)
    hf_home = _hf_cache_with_marker(tmp_path)
    monkeypatch.setenv("HF_HOME", str(hf_home))

    result = MusicGenLocal().get_status()

    assert result.status == "available"


def test_status_does_not_trigger_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``get_status()`` must never invoke ``transformers.pipeline(...)``.

    RFC §4.5 forbids status checks from triggering downloads or model loads
    (the Piper 1.4.2 → 1.7.0 trap). The spy counts every call and raises if
    invoked; ``get_status()`` must complete without raising and the call
    count must remain zero.
    """
    spy = _install_fake_deps(monkeypatch)
    hf_home = _hf_cache_with_marker(tmp_path)
    monkeypatch.setenv("HF_HOME", str(hf_home))

    MusicGenLocal().get_status()  # must not raise

    assert spy.calls == [], (
        "transformers.pipeline() was called during get_status() - "
        "RFC §4.5 forbids status checks from triggering downloads or loads."
    )