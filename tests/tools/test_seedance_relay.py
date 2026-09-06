"""Unit tests for SeedanceRelay — Seedance 2.0 video generation routed through a
new-api compatible relay (中转站).

No network access: env vars are set/cleared via monkeypatch and
tools.video._relay.generate_via_relay is patched. tools/base_tool.py calls
_load_dotenv() at import, so a .env file could pre-populate env vars — the
status tests explicitly set/clear them to stay deterministic.
"""

from __future__ import annotations

import pytest

from tools.base_tool import ToolStatus
from tools.video._relay import RelayError
from tools.video.seedance_relay import SeedanceRelay


def _clear_relay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VIDEO_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("VIDEO_RELAY_API_KEY", raising=False)


def _set_relay_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDEO_RELAY_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("VIDEO_RELAY_API_KEY", "relay-test-key")


def _fake_relay_result(output_path: str) -> dict:
    return {
        "gateway": "new-api",
        "task_id": "task_123",
        "model": "seedance-2-0",
        "remote_url": "http://relay.example/video.mp4",
        "output": output_path,
        "output_path": output_path,
        "format": "mp4",
    }


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------

def test_get_status_unavailable_when_env_vars_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_relay_env(monkeypatch)
    assert SeedanceRelay().get_status() == ToolStatus.UNAVAILABLE


def test_get_status_available_when_both_env_vars_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_relay_env(monkeypatch)
    assert SeedanceRelay().get_status() == ToolStatus.AVAILABLE


# ---------------------------------------------------------------------------
# execute
# ---------------------------------------------------------------------------

def test_execute_without_env_vars_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_relay_env(monkeypatch)
    result = SeedanceRelay().execute({"prompt": "a cat jumps"})
    assert result.success is False
    assert "VIDEO_RELAY_BASE_URL" in (result.error or "")


def test_execute_happy_path_default_model(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _set_relay_env(monkeypatch)
    output_path = str(tmp_path / "clip.mp4")

    from tools.video import seedance_relay as mod
    monkeypatch.setattr(mod, "generate_via_relay", lambda **kw: _fake_relay_result(output_path))

    result = SeedanceRelay().execute(
        {"prompt": "a cinematic sunset over the ocean", "output_path": output_path}
    )

    assert result.success is True, result.error
    assert result.data["provider"] == "seedance_relay"
    assert result.data["gateway"] == "new-api"
    assert result.data["output_path"] == output_path
    assert result.model == "seedance-2-0"
    assert output_path in result.artifacts


def test_execute_fast_variant_maps_to_fast_model(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _set_relay_env(monkeypatch)
    output_path = str(tmp_path / "clip.mp4")
    captured: dict = {}

    from tools.video import seedance_relay as mod

    def fake_generate(**kw):
        captured.update(kw)
        return _fake_relay_result(output_path)

    monkeypatch.setattr(mod, "generate_via_relay", fake_generate)

    result = SeedanceRelay().execute(
        {"prompt": "x", "model_variant": "fast", "output_path": output_path}
    )

    assert result.success is True, result.error
    assert captured["model"] == "seedance-2-0-fast"
    assert result.model == "seedance-2-0-fast"


def test_execute_model_name_overrides_variant_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _set_relay_env(monkeypatch)
    output_path = str(tmp_path / "clip.mp4")
    captured: dict = {}

    from tools.video import seedance_relay as mod

    def fake_generate(**kw):
        captured.update(kw)
        return _fake_relay_result(output_path)

    monkeypatch.setattr(mod, "generate_via_relay", fake_generate)

    result = SeedanceRelay().execute(
        {
            "prompt": "x",
            "model_variant": "fast",
            "model_name": "seedance-custom",
            "output_path": output_path,
        }
    )

    assert result.success is True, result.error
    assert captured["model"] == "seedance-custom"
    assert result.model == "seedance-custom"


def test_execute_relay_error_returns_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_relay_env(monkeypatch)

    from tools.video import seedance_relay as mod

    def boom(**kw):
        raise RelayError("relay video task failed: out of quota")

    monkeypatch.setattr(mod, "generate_via_relay", boom)

    result = SeedanceRelay().execute({"prompt": "x"})

    assert result.success is False
    assert "Seedance relay video generation failed" in (result.error or "")


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------

def test_estimate_cost_standard_greater_than_fast() -> None:
    tool = SeedanceRelay()
    standard = tool.estimate_cost({"model_variant": "standard", "duration": "5"})
    fast = tool.estimate_cost({"model_variant": "fast", "duration": "5"})
    assert standard > fast
