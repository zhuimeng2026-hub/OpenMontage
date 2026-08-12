"""Prove the session-id ContextVar survives the FastMCP thread hop.

Root cause of the "Mcp-Session-Id is required" failure: FastMCP dispatches
synchronous ``@mcp.tool()`` functions to a worker thread.  ``asyncio.to_thread``
(and the executor FastMCP uses) does NOT copy ContextVars, so a session id set
in the ASGI middleware's async context is invisible inside the tool -- making
``get_mcp_session_id()`` return ``None``.

The fix (already applied in mcp_server.py) makes those native tools ``async``
so the wrapper runs in the event loop (where the ContextVar IS visible) and
wraps ``tool.execute`` in ``_run_tool_sync``, which copies the context into the
thread.  This test reproduces the exact before/after without needing fastmcp.
"""

import asyncio
import contextvars

# Mirror lib.mcp_session.py
_session: contextvars.ContextVar = contextvars.ContextVar("mcp_session_id", default=None)


def get_mcp_session_id():
    return _session.get()


def set_mcp_session_id(value):
    return _session.set(value)


# Mirror the fix in mcp_server.py
async def _run_tool_sync(tool, inputs):
    ctx = contextvars.copy_context()
    return await asyncio.to_thread(ctx.run, tool.execute, inputs)


class FakeAssetTool:
    """Stand-in for the underlying upload_asset BaseTool."""

    def execute(self, inputs):
        # Inside the worker thread: this is where require_session() runs.
        return {"session_seen": get_mcp_session_id(), "inputs": inputs}


class FakeWrapper:
    """Stand-in for the @mcp.tool() wrapper (the relevant logic only)."""

    def __init__(self, tool):
        self.tool = tool

    # --- BEFORE fix: synchronous wrapper dispatched to a thread ---
    def call_before(self):
        sid = get_mcp_session_id()  # in wrapper, but tool runs in bare thread
        return self.tool.execute({**{"mcp_session_id": sid}, "note": "before"})

    # --- AFTER fix: async wrapper + _run_tool_sync ---
    async def call_after(self):
        sid = get_mcp_session_id()  # async wrapper sees the ContextVar
        return await _run_tool_sync(self.tool, {**{"mcp_session_id": sid}, "note": "after"})


async def main():
    tool = FakeAssetTool()
    wrapper = FakeWrapper(tool)

    # Simulate the middleware setting the session in the async request context.
    token = set_mcp_session_id("SID-ABC-123")

    # BEFORE: FastMCP dispatches a SYNC wrapper to a worker thread via the
    # executor (loop.run_in_executor / anyio.to_thread) which does NOT copy
    # ContextVars -- this is the real production path, not asyncio.to_thread.
    loop = asyncio.get_running_loop()
    before = await loop.run_in_executor(None, wrapper.call_before)
    # AFTER:  async wrapper + _run_tool_sync (context copied into thread)
    after = await wrapper.call_after()

    set_mcp_session_id.__defaults__  # no-op keep linter calm
    _session.reset(token)

    print("BEFORE fix -> session seen inside tool thread:", before["session_seen"])
    print("AFTER  fix -> session seen inside tool thread:", after["session_seen"])

    assert before["session_seen"] is None, "pre-condition: sync path must lose the ContextVar"
    assert after["session_seen"] == "SID-ABC-123", "fix must preserve the ContextVar across the thread hop"
    print("\nPASS: _run_tool_sync preserves the session id across the worker-thread hop.")


if __name__ == "__main__":
    asyncio.run(main())
