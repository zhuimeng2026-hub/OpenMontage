#!/usr/bin/env python3
"""Phase 6 gate stub MCP server.

Listens on POST /mcp (the URL MCP_BASE_URL points to) and answers a minimal
JSON-RPC 2.0 stream that satisfies the mvpclient hand-shake:

  - initialize / notifications/initialized → ok
  - tools/call name=video_compose operation=render → synchronous artifact
    matching the §23 scope shapes (storyboard / animatic / sample / render).
  - tools/call name=video_compose operation=status   → always "succeeded"
    (we skip the async polling path; the gate tests the sync fast-path).

If MCP_BASE_URL is unset in the BFF environment, the gate's "503 fail-loud"
test runs against a BFF that never received a MCP_BASE_URL, so this server
is not contacted at all. Good — we have one stub covering the success path,
one environment covering the failure path.

Outputs request/response lines to stdout so the gate log can correlate the
BFF's `[mcp-http] response method=tools/call ...` lines with our side.
"""
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import sys
import time


def make_render_artifact(stage: str, project_id: str) -> dict:
    """Build the §23-shaped artifact for each stage.

    Storyboard → scenes[]; animatic → preview_url; sample → files[]; render → final_url.
    All carry external_run_id + om_project_id so the runner can stamp
    production_jobs.artifacts_json without further upstream calls.
    """
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
        # The stub marks render jobs as failing to exercise the Refund path.
        # Other stages return success.
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


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Silence default per-request stderr noise.
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
                "serverInfo": {"name": "phase-6-stub", "version": "0.0.1"},
            }})
            return
        if method == "notifications/initialized":
            # No id → no response needed (per JSON-RPC), but our BFF still
            # expects a parseable envelope. Send 200 with empty body.
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
                payload = make_render_artifact(stage, args.get("project_id", ""))
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

        # Unknown method.
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
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18910
    httpd = HTTPServer(("127.0.0.1", port), Handler)
    sys.stdout.write(f"[mcp-stub] listening on 127.0.0.1:{port}\n")
    sys.stdout.flush()
    httpd.serve_forever()


if __name__ == "__main__":
    main()