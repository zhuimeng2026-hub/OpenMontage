from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts.frameflow_observer import (
    ObserverConfig,
    ObserverHandler,
    ThreadingHTTPServer,
    metric_samples,
    redact,
)


TOKEN = "observer-test-token-1234567890"


def request_json(url: str, token: str | None = None) -> tuple[int, dict]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=2) as response:
            return response.status, json.loads(response.read())
    except HTTPError as exc:
        return exc.code, json.loads(exc.read())


@contextmanager
def observer_server(tmp_path: Path, *, allow_cidr: list[str] | None = None):
    metrics = tmp_path / "metrics.jsonl"
    metrics.write_text(
        '{"timestamp":"first","cpu_percent":10}\n'
        '{"timestamp":"second","cpu_percent":20}\n'
        '{"type":"summary","peaks":{"cpu_percent":20}}\n',
        encoding="utf-8",
    )
    monitor = tmp_path / "monitor.log"
    monitor.write_text("MCP_API_TOKEN=topsecret\nnormal line\n", encoding="utf-8")
    args = SimpleNamespace(
        metrics_file=str(metrics),
        monitor_log=str(monitor),
        nginx_access_log=str(tmp_path / "access.log"),
        nginx_error_log=str(tmp_path / "error.log"),
        bff_unit="frameflow-bff.service",
        mcp_unit="openmontage-mcp.service",
        allow_cidr=allow_cidr or ["127.0.0.1/32"],
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), ObserverHandler)
    server.observer_config = ObserverConfig(args, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", metrics
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_metric_samples_ignore_summary_and_invalid_lines(tmp_path: Path):
    path = tmp_path / "metrics.jsonl"
    path.write_text(
        'not-json\n{"timestamp":"one"}\n{"type":"summary"}\n{"timestamp":"two"}\n',
        encoding="utf-8",
    )
    assert [sample["timestamp"] for sample in metric_samples(path, 10)] == ["one", "two"]


def test_observer_auth_latest_tail_and_log_redaction(tmp_path: Path):
    with observer_server(tmp_path) as (base_url, _metrics):
        assert request_json(f"{base_url}/health")[0] == 200
        assert request_json(f"{base_url}/v1/metrics/latest")[0] == 401

        status, latest = request_json(f"{base_url}/v1/metrics/latest", TOKEN)
        assert status == 200
        assert latest["timestamp"] == "second"

        status, history = request_json(f"{base_url}/v1/metrics/tail?limit=10", TOKEN)
        assert status == 200
        assert history["count"] == 2

        status, logs = request_json(f"{base_url}/v1/logs?source=monitor&lines=10", TOKEN)
        assert status == 200
        assert "topsecret" not in json.dumps(logs)
        assert "[REDACTED]" in json.dumps(logs)


def test_observer_rejects_unknown_source_and_disallowed_client(tmp_path: Path):
    with observer_server(tmp_path) as (base_url, _metrics):
        status, payload = request_json(f"{base_url}/v1/logs?source=unknown", TOKEN)
        assert status == 400
        assert "mcp" in payload["allowed"]

    with observer_server(tmp_path, allow_cidr=["10.0.0.0/8"]) as (base_url, _metrics):
        assert request_json(f"{base_url}/health")[0] == 200
        assert request_json(f"{base_url}/v1/metrics/latest", TOKEN)[0] == 403


@pytest.mark.parametrize(
    "raw,secret",
    [
        ("Authorization: Bearer abc123", "abc123"),
        ("api_key=private-value", "private-value"),
        ("https://host/path?access_token=query-secret", "query-secret"),
    ],
)
def test_redact(raw: str, secret: str):
    value = redact(raw)
    assert secret not in value
    assert "[REDACTED]" in value
