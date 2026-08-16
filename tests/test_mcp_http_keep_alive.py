from __future__ import annotations

import mcp_server


def test_http_keep_alive_default_and_bounds(monkeypatch):
    monkeypatch.delenv("MCP_HTTP_KEEP_ALIVE_SECONDS", raising=False)
    assert mcp_server._http_keep_alive_seconds() == 30

    monkeypatch.setenv("MCP_HTTP_KEEP_ALIVE_SECONDS", "45")
    assert mcp_server._http_keep_alive_seconds() == 45

    monkeypatch.setenv("MCP_HTTP_KEEP_ALIVE_SECONDS", "1")
    assert mcp_server._http_keep_alive_seconds() == 10

    monkeypatch.setenv("MCP_HTTP_KEEP_ALIVE_SECONDS", "999")
    assert mcp_server._http_keep_alive_seconds() == 300

    monkeypatch.setenv("MCP_HTTP_KEEP_ALIVE_SECONDS", "invalid")
    assert mcp_server._http_keep_alive_seconds() == 30
