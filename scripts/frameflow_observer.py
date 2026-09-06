#!/usr/bin/env python3
"""Authenticated, read-only HTTP observer for FrameFlow load tests.

The server exposes recent performance samples and a small, explicit allowlist
of local logs. It has no mutation endpoints and uses only the Python standard
library so it can run on a minimal Ubuntu host.
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import re
import subprocess
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse


SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s\"']+"),
    re.compile(
        r"(?i)((?:mcp_api_token|api[_-]?token|api[_-]?key|app[_-]?secret|access[_-]?token)"
        r"\s*[:=]\s*)[^\s\"']+"
    ),
    re.compile(r"(?i)([?&](?:access_token|token|key)=)[^&\s]+"),
)


def redact(value: str) -> str:
    for pattern in SECRET_PATTERNS:
        value = pattern.sub(r"\1[REDACTED]", value)
    return value


def tail_lines(path: Path, limit: int, max_bytes: int = 2 * 1024 * 1024) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read()
    except OSError as exc:
        return [f"unavailable: {exc}"]
    lines = raw.decode("utf-8", errors="replace").splitlines()
    return lines[-limit:]


def journal_lines(unit: str, limit: int) -> list[str]:
    try:
        result = subprocess.run(
            [
                "journalctl",
                "--unit",
                unit,
                "--lines",
                str(limit),
                "--no-pager",
                "--output=short-iso",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [f"unavailable: {exc}"]
    output = result.stdout if result.returncode == 0 else result.stderr
    return output.splitlines()[-limit:]


def metric_samples(path: Path, limit: int) -> list[dict[str, object]]:
    """Return real samples only, ignoring monitor summary/control records."""
    samples: list[dict[str, object]] = []
    for line in tail_lines(path, max(limit * 2, 50)):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") != "summary" and "timestamp" in value:
            samples.append(value)
    return samples[-limit:]


class ObserverConfig:
    def __init__(self, args: argparse.Namespace, token: str) -> None:
        self.token = token
        self.started_at = time.time()
        self.metrics_file = Path(args.metrics_file)
        self.file_sources = {
            "monitor": Path(args.monitor_log),
            "nginx-access": Path(args.nginx_access_log),
            "nginx-error": Path(args.nginx_error_log),
        }
        self.journal_sources = {
            "bff": args.bff_unit,
            "mcp": args.mcp_unit,
        }
        self.allowed_networks = [ipaddress.ip_network(item) for item in args.allow_cidr]

    def client_allowed(self, address: str) -> bool:
        if not self.allowed_networks:
            return True
        try:
            client = ipaddress.ip_address(address)
        except ValueError:
            return False
        return any(client in network for network in self.allowed_networks)


class ObserverHandler(BaseHTTPRequestHandler):
    server_version = "FrameFlowObserver/1.0"

    @property
    def config(self) -> ObserverConfig:
        return self.server.observer_config  # type: ignore[attr-defined]

    def send_json(self, status: HTTPStatus, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def authenticated(self) -> bool:
        supplied = self.headers.get("Authorization", "")
        if supplied.startswith("Bearer "):
            supplied = supplied[7:]
        else:
            supplied = self.headers.get("X-Observer-Token", "")
        return hmac.compare_digest(supplied, self.config.token)

    def requested_limit(self, query: dict[str, list[str]], default: int = 200) -> int:
        try:
            requested = int(query.get("limit", query.get("lines", [str(default)]))[0])
        except (ValueError, IndexError):
            requested = default
        return max(1, min(requested, 1000))

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            self.send_json(
                HTTPStatus.OK,
                {"ok": True, "uptime_seconds": round(time.time() - self.config.started_at, 1)},
            )
            return
        if not self.config.client_allowed(self.client_address[0]):
            self.send_json(HTTPStatus.FORBIDDEN, {"error": "source address not allowed"})
            return
        if not self.authenticated():
            self.send_json(HTTPStatus.UNAUTHORIZED, {"error": "invalid observer token"})
            return

        query = parse_qs(parsed.query)
        limit = self.requested_limit(query)
        if parsed.path == "/v1/metrics/latest":
            samples = metric_samples(self.config.metrics_file, 1)
            if not samples:
                self.send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "no valid metric sample"})
                return
            self.send_json(HTTPStatus.OK, samples[-1])
            return
        if parsed.path == "/v1/metrics/tail":
            samples = metric_samples(self.config.metrics_file, limit)
            self.send_json(HTTPStatus.OK, {"count": len(samples), "samples": samples})
            return
        if parsed.path == "/v1/logs":
            source = query.get("source", [""])[0]
            if source in self.config.file_sources:
                lines = tail_lines(self.config.file_sources[source], limit)
            elif source in self.config.journal_sources:
                lines = journal_lines(self.config.journal_sources[source], limit)
            else:
                allowed = sorted((*self.config.file_sources, *self.config.journal_sources))
                self.send_json(HTTPStatus.BAD_REQUEST, {"error": "unknown source", "allowed": allowed})
                return
            self.send_json(
                HTTPStatus.OK,
                {"source": source, "count": len(lines), "lines": [redact(line) for line in lines]},
            )
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def log_message(self, format_string: str, *args: object) -> None:
        message = format_string % args
        print(f"{self.log_date_time_string()} client={self.client_address[0]} {redact(message)}", flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9910)
    parser.add_argument("--metrics-file", default="/var/log/frameflow-observer/metrics.jsonl")
    parser.add_argument("--monitor-log", default="/var/log/frameflow-observer/monitor.log")
    parser.add_argument("--nginx-access-log", default="/var/log/nginx/access.log")
    parser.add_argument("--nginx-error-log", default="/var/log/nginx/error.log")
    parser.add_argument("--bff-unit", default="frameflow-bff.service")
    parser.add_argument("--mcp-unit", default="openmontage-mcp.service")
    parser.add_argument("--token-env", default="FRAMEFLOW_OBSERVER_TOKEN")
    parser.add_argument(
        "--allow-cidr",
        action="append",
        default=[],
        help="Allowed client CIDR; repeat as needed. Empty allows all addresses with a valid token.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    token = os.environ.get(args.token_env, "")
    if len(token) < 24:
        raise SystemExit(f"{args.token_env} must be set to a random value of at least 24 characters")
    config = ObserverConfig(args, token)
    server = ThreadingHTTPServer((args.host, args.port), ObserverHandler)
    server.observer_config = config  # type: ignore[attr-defined]
    print(
        f"FrameFlow observer listening on {args.host}:{args.port}; "
        f"allowed_cidrs={args.allow_cidr or ['token-authenticated clients']}",
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
