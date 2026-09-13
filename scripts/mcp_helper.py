#!/usr/bin/env python3
"""MCP JSON-RPC helper for the mcp-decompose-and-recompose skill.

Subcommands:
  info  <tool>                     - show input_schema + cost/best_for
  exec  <tool> <inputs.json>       - call execute_tool via MCP
  dry   <tool> <inputs.json>       - call dry_run_tool (cost / runtime estimate)
  upload <tool> <inputs.json>      - call any upload tool (upload_asset, ...)

Session bootstrap (one-time, prints the session id to stdout):
  python3 scripts/mcp_helper.py init

Reads MCP_API_TOKEN from .env in cwd. Persists the session id to
/tmp/mcp_session.txt so subsequent calls in the same shell don't have to
re-initialize. Override MCP_URL / SESSION_FILE / TOKEN_FILE via env if needed.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MCP_URL = os.environ.get("OPENMONTAGE_MCP_URL", "http://localhost:8900/mcp")
SESSION_FILE = Path(os.environ.get("MCP_SESSION_FILE", "/tmp/mcp_session.txt"))
TOKEN_FILE = Path(os.environ.get("MCP_TOKEN_FILE", ".env"))


def load_token() -> str:
    """Read MCP_API_TOKEN from TOKEN_FILE (one-line `KEY=value` per line)."""
    if not TOKEN_FILE.exists():
        sys.exit(f"Token file {TOKEN_FILE} not found; set MCP_TOKEN_FILE or create .env with MCP_API_TOKEN")
    for line in TOKEN_FILE.read_text().splitlines():
        if line.startswith("MCP_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"MCP_API_TOKEN not found in {TOKEN_FILE}")


def init_session(token: str) -> str:
    """Send initialize and persist the resulting mcp-session-id."""
    body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "mcp-decompose-and-recompose", "version": "1"},
        },
    }
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        sid = r.headers.get("mcp-session-id", "").strip()
        r.read()  # drain
    if not sid:
        sys.exit("initialize returned no mcp-session-id header")
    SESSION_FILE.write_text(sid)
    return sid


def call(method: str, params: dict, token: str, session: str, *, timeout: int = 600):
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
            "mcp-session-id": session,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read().decode()
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        # streamable-http sometimes returns SSE
        for ln in payload.splitlines():
            if ln.startswith("data:"):
                return json.loads(ln[5:].strip())
        raise


def cmd_init():
    sid = init_session(load_token())
    print(sid)


def cmd_info(tool: str):
    token = load_token()
    session = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else init_session(token)
    resp = call("tools/call", {"name": "get_tool_info", "arguments": {"tool_name": tool}}, token, session)
    info = resp.get("result", {}).get("structuredContent") or resp.get("result", {})
    sch = info.get("input_schema") or info.get("inputSchema") or {}
    props = sch.get("properties", {}) if isinstance(sch, dict) else {}
    print(f"=== {tool} input_schema ===")
    print(json.dumps(
        {k: (v.get("title", k) if isinstance(v, dict) else v)
         for k, v in list(props.items())[:18]}, indent=2))
    print(f"=== {tool} cost/best_for ===")
    print(json.dumps({k: info.get(k) for k in ("best_for", "not_good_for", "cost", "runtime")}, indent=2))


def cmd_exec(tool: str, inputs_path: str, *, dry: bool = False):
    token = load_token()
    session = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else init_session(token)
    inputs = json.loads(Path(inputs_path).read_text())
    method = "dry_run_tool" if dry else "execute_tool"
    resp = call("tools/call", {"name": method, "arguments": {"tool_name": tool, "inputs": inputs}}, token, session)
    sc = resp.get("result", {}).get("structuredContent") or resp.get("result", {})
    print(json.dumps(sc, indent=2, ensure_ascii=False))


def cmd_upload(tool: str, inputs_path: str):
    token = load_token()
    session = SESSION_FILE.read_text().strip() if SESSION_FILE.exists() else init_session(token)
    args = json.loads(Path(inputs_path).read_text())
    resp = call("tools/call", {"name": tool, "arguments": args}, token, session)
    sc = resp.get("result", {}).get("structuredContent") or resp.get("result", {})
    print(json.dumps(sc, indent=2, ensure_ascii=False))


def main():
    if len(sys.argv) < 2:
        sys.exit("Usage: mcp_helper.py init | info <tool> | exec <tool> <inputs.json>"
                 " | dry <tool> <inputs.json> | upload <tool> <inputs.json>")
    cmd = sys.argv[1]
    if cmd == "init":
        cmd_init()
    elif cmd == "info":
        cmd_info(sys.argv[2])
    elif cmd in ("exec", "dry"):
        cmd_exec(sys.argv[2], sys.argv[3], dry=(cmd == "dry"))
    elif cmd == "upload":
        cmd_upload(sys.argv[2], sys.argv[3])
    else:
        sys.exit(f"Unknown subcommand: {cmd}")


if __name__ == "__main__":
    main()