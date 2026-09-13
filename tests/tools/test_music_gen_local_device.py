"""Tests for ``MusicGenLocal._device`` device-resolution helper.

RFC: ``docs/music-gen-local-rfc-2026-08-28.md`` §7.4.

These tests mock ``torch`` at the ``sys.modules`` level so they run on hosts
that don't have torch installed (and so device detection is fully
deterministic). The cached-device contract is pinned: ``_device()`` must
re-evaluate on first call and then return the cached value forever after,
so a downstream change to ``torch.cuda.is_available()`` between calls does
not silently flip the runtime.

When the ``music_gen_local`` tool itself is not yet implemented (the RFC
defines the tool but the module is not yet on disk), the entire module
collapses to ``SKIPPED`` via a module-level ``pytestmark``. The tests
themselves are self-contained and only depend on the public contract
``MusicGenLocal._device()`` plus the ``_device_cached`` instance attribute.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

# Conditional import: the RFC defines the tool but does not yet ship the
# implementation. Skip the whole module cleanly until the tool lands.
try:
    from tools.audio.music_gen_local import MusicGenLocal  # noqa: F401

    _HAS_TOOL = True
except Exception:  # noqa: BLE001 - ImportError, but also ModuleNotFoundError etc.
    MusicGenLocal = None  # type: ignore[assignment]
    _HAS_TOOL = False


pytestmark = pytest.mark.skipif(
    not _HAS_TOOL,
    reason=(
        "music_gen_local tool not yet implemented (RFC §7.4). "
        "Land tools/audio/music_gen_local.py to enable these tests."
    ),
)


# ---------------------------------------------------------------------------
# Fake torch helpers
# ---------------------------------------------------------------------------


def _make_fake_torch(
    *,
    cuda: bool,
    mps: bool,
    cuda_side_effect: Any = None,
) -> types.ModuleType:
    """Build a minimal fake ``torch`` module with controllable device queries.

    ``cuda_side_effect`` overrides ``torch.cuda.is_available`` entirely; pass
    a callable that takes no arguments and either returns a bool or raises.
    When unset, ``torch.cuda.is_available`` returns the value of ``cuda``.
    """
    cuda_fn = cuda_side_effect if cuda_side_effect is not None else (lambda: cuda)
    fake = types.ModuleType("torch")
    fake.cuda = SimpleNamespace(is_available=cuda_fn)
    fake.backends = SimpleNamespace(mps=SimpleNamespace(is_available=lambda: mps))
    return fake


@pytest.fixture()
def fresh_tool(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Construct a fresh ``MusicGenLocal`` instance with a stubbed ``torch``.

    The instance is fully isolated: each test gets a brand-new object so the
    ``_device_cached`` attribute starts unset, matching the RFC's per-instance
    cache contract. The fixture also installs a baseline ``torch`` stub
    (``cuda=False, mps=False``) so tests that don't care about torch still
    see a deterministic module while monkeypatching what they need.
    """
    assert _HAS_TOOL, "fixture must not be used while pytestmark is skipping"
    monkeypatch.setitem(sys.modules, "torch", _make_fake_torch(cuda=False, mps=False))
    return MusicGenLocal()


# ---------------------------------------------------------------------------
# _device — basic routing (RFC §4.5)
# ---------------------------------------------------------------------------


def test_device_picks_cuda_when_available(monkeypatch: pytest.MonkeyPatch, fresh_tool: Any) -> None:
    """CUDA takes priority over MPS when available."""
    monkeypatch.setitem(
        sys.modules,
        "torch",
        _make_fake_torch(cuda=True, mps=True),
    )
    fresh_tool._device_cached = None  # belt-and-braces reset

    assert fresh_tool._device() == "cuda"
    # Subsequent calls must return the same cached value.
    assert fresh_tool._device() == "cuda"


def test_device_picks_mps_when_apple_silicon(monkeypatch: pytest.MonkeyPatch, fresh_tool: Any) -> None:
    """Apple Silicon (no CUDA, MPS available) routes to 'mps'."""
    monkeypatch.setitem(
        sys.modules,
        "torch",
        _make_fake_torch(cuda=False, mps=True),
    )
    fresh_tool._device_cached = None

    assert fresh_tool._device() == "mps"
    assert fresh_tool._device() == "mps"


def test_device_falls_back_to_cpu(monkeypatch: pytest.MonkeyPatch, fresh_tool: Any) -> None:
    """CPU is the final fallback when neither CUDA nor MPS is available."""
    # The fresh_tool fixture already installs cuda=False / mps=False. Re-install
    # explicitly so the intent is obvious in the test body.
    monkeypatch.setitem(
        sys.modules,
        "torch",
        _make_fake_torch(cuda=False, mps=False),
    )
    fresh_tool._device_cached = None

    assert fresh_tool._device() == "cpu"
    assert fresh_tool._device() == "cpu"


def test_device_is_cached(monkeypatch: pytest.MonkeyPatch, fresh_tool: Any) -> None:
    """Second call must not re-check ``torch``.

    We install a counter on ``torch.cuda.is_available`` that returns ``False``
    on the first call and raises ``RuntimeError`` on every subsequent call.
    The first ``_device()`` invocation routes to ``"cpu"`` (CUDA False, MPS
    False). The second invocation must short-circuit on the cached value and
    never touch ``torch`` — if it did, it would raise.
    """
    calls = {"cuda": 0}

    def cuda_available() -> bool:
        calls["cuda"] += 1
        if calls["cuda"] > 1:
            raise RuntimeError(
                f"torch.cuda.is_available was called a second time "
                f"(call #{calls['cuda']}); _device() must cache the result."
            )
        return False

    monkeypatch.setitem(
        sys.modules,
        "torch",
        _make_fake_torch(cuda=False, mps=False, cuda_side_effect=cuda_available),
    )
    fresh_tool._device_cached = None

    first = fresh_tool._device()
    assert first == "cpu", f"expected first call to return 'cpu', got {first!r}"

    # If the cache is honored, this must not raise and must not re-enter torch.
    second = fresh_tool._device()
    assert second == "cpu"
    assert calls["cuda"] == 1, (
        f"expected torch.cuda.is_available to be called exactly once, "
        f"got {calls['cuda']}"
    )