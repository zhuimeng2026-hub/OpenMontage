"""Regression: MCP scene_detect wrapper — commit b061a71 feat(mcp): expose scene_detect via MCP.

After b061a71 the MCP server's `scene_detect` tool is the canonical entry
point that bag-video-mvp clients call. It must:

  - require a non-empty `input_path`
  - delegate to the registry's scene_detect tool
  - return a structured failure (not raise) when the tool is missing
  - return a structured failure when the underlying tool raises
  - not silently leak Python tracebacks as the `error` string

These tests cover the wrapper at mcp_server.py:953-1001 without booting a
real MCP transport or PySceneDetect — we patch the registry and drive the
async wrapper synchronously via asyncio.run().
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch


def _run(coro):
    """Drive an async coroutine from a sync pytest test."""
    return asyncio.run(coro)


def _wrapper():
    """Lazy import — avoid loading mcp_server.py at module-collection time."""
    import mcp_server  # noqa: F401
    return mcp_server.scene_detect


# ---- happy path ----


def test_delegates_to_registry_scene_detect_tool(tmp_path: Path):
    fake_path = tmp_path / "in.mp4"
    fake_path.write_bytes(b"\x00")

    # The wrapper unpacks the tool's ToolResult into an ExecuteResult
    # (pydantic) — cost_usd / duration_seconds must be real numbers (0.0 is
    # fine), not None, or the pydantic validator rejects them and we'd be
    # testing the wrong failure mode.
    fake_tool = MagicMock()
    fake_tool.execute.return_value = MagicMock(
        success=True,
        data={"scenes": [{"start_seconds": 0, "end_seconds": 2}]},
        error=None,
        artifacts=[],
        cost_usd=0.0,
        duration_seconds=0.0,
        model="pyscenedetect",
    )

    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.return_value = fake_tool
        result = _run(wrapper(input_path=str(fake_path), threshold=0.3))

    assert result.success is True
    assert result.data == {"scenes": [{"start_seconds": 0, "end_seconds": 2}]}
    registry.get.assert_called_once_with("scene_detect")
    fake_tool.execute.assert_called_once()


# ---- failure modes must be structured, not raised ----


def test_returns_structured_failure_when_tool_not_registered():
    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.return_value = None
        result = _run(wrapper(input_path="/tmp/anything.mp4"))

    assert result.success is False
    assert "scene_detect tool not registered" in result.error


def test_returns_structured_failure_when_tool_raises(tmp_path: Path):
    fake_path = tmp_path / "in.mp4"
    fake_path.write_bytes(b"\x00")

    fake_tool = MagicMock()
    fake_tool.execute.side_effect = RuntimeError("ffmpeg crashed")

    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.return_value = fake_tool
        result = _run(wrapper(input_path=str(fake_path)))

    assert result.success is False
    # The wrapper prefixes the exception class — must not leak a traceback.
    assert "RuntimeError" in result.error
    assert "ffmpeg crashed" in result.error
    assert "Traceback" not in result.error


# ---- input validation: delegated to FastMCP, not the wrapper ----
#
# FastMCP validates the JSON-RPC schema upstream of `scene_detect(...)` and
# rejects missing/empty required strings before our function ever runs. The
# wrapper therefore does not need to defend against missing input_path; a
# regression there would show up as a 4xx at the transport layer, not as a
# wrong-shape ExecuteResult. We pin that contract with two negative-shape
# checks: a missing arg raises Python TypeError (caught by FastMCP's
# tool-dispatch layer) and an empty-string arg flows through to the tool,
# which itself surfaces a structured failure.


def test_missing_input_path_raises_type_error():
    """Regression: input_path is required by signature. FastMCP catches this."""
    wrapper = _wrapper()
    try:
        _run(wrapper())  # no input_path
    except TypeError as exc:
        assert "input_path" in str(exc)
        return
    raise AssertionError("expected TypeError for missing required input_path")


def test_empty_input_path_flows_to_tool_which_returns_structured_failure(tmp_path: Path):
    """Regression: empty input_path is propagated; tool handles its own validation."""
    fake_tool = MagicMock()
    fake_tool.execute.return_value = MagicMock(
        success=False,
        data={},
        error="input_path must be a non-empty path to a video file",
        artifacts=[],
        cost_usd=0.0,
        duration_seconds=0.0,
        model=None,
    )

    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.return_value = fake_tool
        result = _run(wrapper(input_path=""))

    assert result.success is False
    assert "input_path" in result.error
    fake_tool.execute.assert_called_once()