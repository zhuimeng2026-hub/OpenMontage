"""Regression: MCP weiyun_gen_share_link wrapper — guard at the wrapper layer.

Before this fix, ``mcp_server.weiyun_gen_share_link`` declared its parameters
with mutable defaults (``file_list: list[str] = []``, ``dir_list: list[str] = []``)
and silently forwarded ``inputs = {"mcp_session_id": ...}`` to the inner
``weiyun_share_link`` tool whenever a caller invoked it with no arguments
or with empty lists. The inner tool then returned
``"file_list or dir_list is required to generate a share link"`` — visible
to the agent / vclaw caller as a generic ToolResult failure rather than a
clear wrapper-layer contract violation.

The fix:

  - Use ``None`` defaults so the wrapper is not vulnerable to the
    classic Python mutable-default-argument bug (each no-arg call sharing
    the same ``[]`` object).
  - Validate at the wrapper layer and return a structured
    ``{"success": False, "error": "file_list or dir_list is required ..."}``
    directly to the caller. The inner tool is **not** invoked when the
    contract is violated, so the failure is cheap and unambiguous.

These tests cover the wrapper at ``mcp_server.py:2625`` without booting a
real MCP transport or Weiyun — we patch the registry and drive the async
wrapper synchronously via ``asyncio.run``.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch


def _run(coro):
    """Drive an async coroutine from a sync pytest test."""
    return asyncio.run(coro)


def _wrapper():
    """Lazy import — avoid loading mcp_server.py at module-collection time."""
    import mcp_server  # noqa: F401
    return mcp_server.weiyun_gen_share_link


# ---- empty-arg contract: short-circuit before calling the inner tool ----


def test_no_arguments_returns_structured_failure_without_invoking_inner_tool():
    """Caller invoking weiyun_gen_share_link() with no file_list/dir_list
    must get a structured contract error at the wrapper layer — NOT a
    deep ToolResult error from the inner weiyun_share_link tool."""
    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        # The inner tool must NOT be looked up; the wrapper short-circuits
        # before registry.get("weiyun_share_link"). Patch .get to raise so a
        # regression that forgets the guard surfaces as a clear test
        # failure rather than silently passing.
        registry.get.side_effect = AssertionError(
            "wrapper must not invoke the inner tool when file_list+dir_list are empty"
        )
        result = _run(wrapper())

    assert result == {
        "success": False,
        "error": "file_list or dir_list is required to generate a share link",
    }


def test_explicit_empty_lists_return_structured_failure():
    """Caller passing file_list=[] and dir_list=[] explicitly must hit the
    same guard — empty default vs explicit empty must not diverge."""
    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.side_effect = AssertionError(
            "wrapper must not invoke the inner tool when both lists are empty"
        )
        result = _run(wrapper(file_list=[], dir_list=[]))

    assert result["success"] is False
    assert "file_list or dir_list is required" in result["error"]


def test_only_dir_list_provided_passes_guard():
    """A non-empty dir_list alone must satisfy the contract and forward."""
    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.return_value = None  # would surface as "not registered"
        result = _run(wrapper(dir_list=["dir-1"]))

    # The guard let it through to the inner-tool-not-registered branch —
    # which is the *expected* path. The test pins that dir_list alone is
    # enough to satisfy the contract.
    assert result["success"] is False
    assert "weiyun_share_link tool is not registered" in result["error"]


def test_file_list_provided_forwards_to_inner_tool():
    """A non-empty file_list must reach the inner tool. The guard must
    not fire on a valid invocation."""
    wrapper = _wrapper()
    sentinel_tool = object()  # any non-None value bypasses the "not registered" guard
    with patch("mcp_server._run_tool_sync") as run_sync:
        with patch("mcp_server.registry") as registry:
            registry.get.return_value = sentinel_tool
            run_sync.return_value = type("R", (), {
                "success": True,
                "data": {"short_url": "https://share.weiyun.com/abc"},
                "artifacts": [],
                "error": None,
            })()
            result = _run(wrapper(file_list=["file-1"], share_name="demo"))

    assert result["success"] is True
    assert result["data"]["short_url"] == "https://share.weiyun.com/abc"
    # registry.get("weiyun_share_link") was called once.
    registry.get.assert_called_once_with("weiyun_share_link")
    # _run_tool_sync was called exactly once with (sentinel_tool, forwarded_inputs).
    args, _ = run_sync.call_args
    assert args[0] is sentinel_tool
    forwarded = args[1]
    assert forwarded["file_list"] == ["file-1"]
    assert forwarded["share_name"] == "demo"


# ---- mutable-default-argument regression ----


def test_no_arguments_twice_does_not_share_state():
    """A second no-arg call must produce the same structured error and
    not be tainted by any state from the first call. Pre-fix, the
    mutable default ``[]`` was shared across calls; this pins that we
    now use ``None`` defaults + explicit list() materialization."""
    wrapper = _wrapper()
    with patch("mcp_server.registry") as registry:
        registry.get.side_effect = AssertionError("inner tool must not be called")

        first = _run(wrapper())
        second = _run(wrapper())

    assert first["success"] is False
    assert second["success"] is False
    assert first == second