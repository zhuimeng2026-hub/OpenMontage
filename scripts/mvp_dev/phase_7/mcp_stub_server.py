#!/usr/bin/env python3
"""Phase 7 stub MCP server (extends phase 6's mcp_stub_server.py).

Differences from phase 6:
  - Adds --succeed-render CLI flag. Default keeps phase 6 behavior (render
    returns an error payload to exercise the Refund path). With the flag,
    render returns a successful artifact so phase 7 can verify the full
    SAMPLE_READY → approve → WAITING_APPROVAL → render → COMPLETED path.

Other stages (storyboard / animatic / sample / status) behave identically
to phase 6.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys
import time


def make_render_artifact(stage: str, project_id: str, succeed_render: bool) -> dict:
    ext_run = f"stub-run-{stage}-{int(time.time() * 1000)}"
    om_proj = f"om-{project_id[:8]}-fake"

    if stage == "storyboard":
        return {
            "done": True,
            "external_run_id": ext_run,
            "om_project_id": om_proj,
            "artifact": {
                "scenes": [
                    {"scene_id": 1, "preview_url": f"http://stub/storyboard/{ext_run}/scene_1.png", "duration": 2.4},
                    {"scene_id": 2, "preview_url": f"http://stub/storyboard/{ext_run}/scene_2.png", "duration": 2.1},
                    {"scene_id": 3, "preview_url": f"http://stub/storyboard/{ext_run}/scene_3.png", "duration": 2.7},
                ],
            },
        }
    if stage == "animatic":
        return {
            "done": True,
            "external_run_id": ext_run,
            "om_project_id": om_proj,
            "artifact": {
                "preview_url": f"http://stub/animatic/{ext_run}.mp4",
                "duration_seconds": 20.0,
                "resolution": "540x960",
            },
        }
    if stage == "sample":
        return {
            "done": True,
            "external_run_id": ext_run,
            "om_project_id": om_proj,
            "artifact": {
                "files": [
                    f"http://stub/sample/{ext_run}/scene_3.mp4",
                    f"http://stub/sample/{ext_run}/scene_5.mp4",
                ],
                "scene_ids": [3, 5],
            },
        }
    if stage == "render":
        if succeed_render:
            return {
                "done": True,
                "external_run_id": ext_run,
                "om_project_id": om_proj,
                "artifact": {
                    "preview_url": f"http://stub/render/{ext_run}.mp4",
                    "duration_seconds": 20.0,
                    "resolution": "1080x1920",
                },
            }
        # phase 6 behavior: render intentionally fails to verify quota Refund.
        return {
            "done": True,
            "external_run_id": ext_run,
            "om_project_id": om_proj,
            "error": "stub: render intentionally fails to verify quota Refund",
        }
    return {"done": True, "external_run_id": ext_run, "om_project_id": om_proj}


def make_status_response(external_run_id: str) -> dict:
    return {
        "status": "succeeded",
        "progress": 1.0,
        "artifact": {"external_run_id": external_run_id},
    }


# Module-level flag set in main(); Handler reads via SUCCEED_RENDER.
SUCCEED_RENDER = False


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("[mcp-stub] " + fmt % args + "\n")

    def do_POST(self):
        if self.path != "/mcp":
            self.send_error(404, "unknown path")
            return
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length > 0 else b"{}"
        try:
            req = json.loads(body)
        except Exception:
            self._send(400, {"error": "bad json"})
            return
        method = req.get("method", "")
        req_id = req.get("id")

        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "phase-7-stub", "version": "0.0.1"},
            }})
            return
        if method == "notifications/initialized":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "stub-sid-stable")
            self.end_headers()
            return
        if method == "tools/list":
            self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": {
                "tools": [{"name": "video_compose", "description": "stub video_compose"}],
            }})
            return
        if method == "tools/call":
            params = req.get("params", {}) or {}
            name = params.get("name", "")
            args = params.get("arguments", {}) or {}
            if name != "video_compose":
                self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": json.dumps({"error": f"unknown tool {name}"})}],
                }})
                return
            op = args.get("operation", "")
            if op == "render":
                stage = args.get("stage", "")
                payload = make_render_artifact(stage, args.get("project_id", ""), SUCCEED_RENDER)
                self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                }})
                return
            if op == "status":
                payload = make_status_response(args.get("external_run_id", ""))
                self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": {
                    "content": [{"type": "text", "text": json.dumps(payload)}],
                }})
                return
            self._send(200, {"jsonrpc": "2.0", "id": req_id, "result": {
                "content": [{"type": "text", "text": json.dumps({"error": f"unknown op {op}"})}],
            }})
            return

        self._send(200, {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": f"unknown method {method}"}})

    def _send(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Mcp-Session-Id", "stub-sid-stable")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    global SUCCEED_RENDER
    args = sys.argv[1:]
    port = 18910
    for arg in args:
        if arg == "--succeed-render":
            SUCCEED_RENDER = True
        elif arg.isdigit():
            port = int(arg)
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    sys.stdout.write(f"[mcp-stub] listening on 127.0.0.1:{port} succeed_render={SUCCEED_RENDER}\n")
    sys.stdout.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()