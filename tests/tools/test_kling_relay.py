"""Unit tests for KlingRelay (new-api relay-backed Kling video generation).

All tests are offline: env vars are managed via monkeypatch and the shared
relay client (tools.video._relay.generate_via_relay) is patched so no network
request ever leaves the process.
"""

from __future__ import annotations

from unittest import mock

import pytest

from tools.base_tool import ToolStatus
from tools.video._relay import RelayError
from tools.video.kling_relay import KlingRelay


def _env_off(monkeypatch):
    monkeypatch.delenv("VIDEO_RELAY_BASE_URL", raising=False)
    monkeypatch.delenv("VIDEO_RELAY_API_KEY", raising=False)


def _env_on(monkeypatch):
    monkeypatch.setenv("VIDEO_RELAY_BASE_URL", "http://127.0.0.1:3000")
    monkeypatch.setenv("VIDEO_RELAY_API_KEY", "relay-token-123")


def _fake_result(output_path="/tmp/kling_relay_out.mp4"):
    return {
        "gateway": "new-api",
        "task_id": "task-123",
        "model": KlingRelay.DEFAULT_MODEL,
        "remote_url": "https://relay.example/kling_relay_out.mp4",
        "output": output_path,
        "output_path": output_path,
        "format": "mp4",
        "file_size_bytes": 1234,
    }


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def test_status_unavailable_when_env_missing(monkeypatch):
    _env_off(monkeypatch)
    assert KlingRelay().get_status() == ToolStatus.UNAVAILABLE


def test_status_available_when_env_set(monkeypatch):
    _env_on(monkeypatch)
    assert KlingRelay().get_status() == ToolStatus.AVAILABLE


# ---------------------------------------------------------------------------
# execute() — missing deps
# ---------------------------------------------------------------------------

def test_execute_without_env_returns_failure(monkeypatch):
    _env_off(monkeypatch)
    result = KlingRelay().execute({"prompt": "a cat"})
    assert result.success is False
    assert "VIDEO_RELAY_BASE_URL" in result.error


# ---------------------------------------------------------------------------
# execute() — happy path
# ---------------------------------------------------------------------------

def test_execute_happy_path(monkeypatch):
    _env_on(monkeypatch)
    tool = KlingRelay()
    fake = _fake_result()
    with mock.patch("tools.video._relay.generate_via_relay", return_value=fake) as gen:
        result = tool.execute({"prompt": "a cat running through a field"})

    assert result.success is True
    assert result.data["provider"] == "kling_relay"
    assert result.data["gateway"] == "new-api"
    assert result.data["model"] == KlingRelay.DEFAULT_MODEL
    assert result.data["output_path"] == "/tmp/kling_relay_out.mp4"
    assert result.artifacts == ["/tmp/kling_relay_out.mp4"]
    assert result.model == KlingRelay.DEFAULT_MODEL
    assert result.cost_usd == tool.estimate_cost({"prompt": "x"})

    # Relay client was invoked with resolved env vars + default model.
    kwargs = gen.call_args.kwargs
    assert kwargs["base_url"] == "http://127.0.0.1:3000"
    assert kwargs["api_key"] == "relay-token-123"
    assert kwargs["model"] == KlingRelay.DEFAULT_MODEL
    assert kwargs["operation"] == "text_to_video"


def test_model_name_overrides_model_variant_mapping(monkeypatch):
    _env_on(monkeypatch)
    tool = KlingRelay()
    fake = _fake_result()
    with mock.patch("tools.video._relay.generate_via_relay", return_value=fake) as gen:
        result = tool.execute(
            {
                "prompt": "a cat",
                "model_variant": "v2.1/master",
                "model_name": "kling-v1-5",
            }
        )

    assert result.success is True
    assert result.model == "kling-v1-5"
    assert gen.call_args.kwargs["model"] == "kling-v1-5"


def test_model_variant_maps_through_model_map(monkeypatch):
    _env_on(monkeypatch)
    tool = KlingRelay()
    fake = _fake_result()
    with mock.patch("tools.video._relay.generate_via_relay", return_value=fake) as gen:
        result = tool.execute({"prompt": "a cat", "model_variant": "v2.1/standard"})

    assert result.success is True
    assert result.model == KlingRelay.MODEL_MAP["v2.1/standard"]
    assert gen.call_args.kwargs["model"] == KlingRelay.MODEL_MAP["v2.1/standard"]


def test_execute_metadata_only_includes_present_keys(monkeypatch):
    _env_on(monkeypatch)
    tool = KlingRelay()
    fake = _fake_result()
    with mock.patch("tools.video._relay.generate_via_relay", return_value=fake) as gen:
        tool.execute(
            {
                "prompt": "a cat",
                "aspect_ratio": "9:16",
                "mode": "pro",
                "cfg_scale": 0.6,
                # negative_prompt omitted intentionally
            }
        )

    metadata = gen.call_args.kwargs["metadata"]
    assert metadata == {"aspect_ratio": "9:16", "mode": "pro", "cfg_scale": 0.6}


# ---------------------------------------------------------------------------
# execute() — failures
# ---------------------------------------------------------------------------

def test_execute_relay_error(monkeypatch):
    _env_on(monkeypatch)
    tool = KlingRelay()
    with mock.patch(
        "tools.video._relay.generate_via_relay",
        side_effect=RelayError("relay video task failed: oops"),
    ):
        result = tool.execute({"prompt": "a cat"})

    assert result.success is False
    assert "Kling relay video generation failed" in result.error
    assert "relay video task failed" in result.error


def test_image_to_video_without_image_url_mentions_image_url(monkeypatch):
    _env_on(monkeypatch)
    tool = KlingRelay()
    with mock.patch(
        "tools.video._relay.generate_via_relay",
        side_effect=RelayError(
            "image_to_video via relay requires an image_url (public URL); "
            "local paths are not uploaded by the relay path."
        ),
    ):
        result = tool.execute({"prompt": "a cat", "operation": "image_to_video"})

    assert result.success is False
    assert "Kling relay video generation failed" in result.error
    assert "image_url" in result.error


# ---------------------------------------------------------------------------
# estimate_cost
# ---------------------------------------------------------------------------

def test_estimate_cost_master_gt_standard():
    tool = KlingRelay()
    master = tool.estimate_cost({"model_variant": "v2.1/master", "duration": "5"})
    standard = tool.estimate_cost({"model_variant": "v2.1/standard", "duration": "5"})
    pro = tool.estimate_cost({"model_variant": "v2.1/pro", "duration": "5"})

    assert master > standard
    assert pro > standard
    assert standard == pytest.approx(0.08)
    assert pro == pytest.approx(0.16)
    assert master == pytest.approx(0.24)


def test_estimate_cost_scales_with_duration():
    tool = KlingRelay()
    dur5 = tool.estimate_cost({"model_variant": "v2.1/master", "duration": "5"})
    dur10 = tool.estimate_cost({"model_variant": "v2.1/master", "duration": "10"})
    assert dur10 == pytest.approx(dur5 * 2)
