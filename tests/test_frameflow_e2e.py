from __future__ import annotations

from pathlib import Path

import requests

from frameflow.bff.frameflow_e2e import BFF, peak_render_overlap


class FailingSession:
    def request(self, *_args, **_kwargs):
        raise requests.Timeout("upstream did not answer")


def test_bff_request_reports_transport_stage_and_elapsed_time():
    client = BFF("http://127.0.0.1:8080", request_timeout=0.1)
    client.http = FailingSession()

    try:
        client.request("POST", "/api/mcp", json={})
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("transport timeout should fail")

    assert "POST /api/mcp" in message
    assert "transport failed after" in message
    assert "Timeout" in message


def test_run_one_returns_diagnostic_record_when_upload_fails(tmp_path: Path):
    client = BFF("http://127.0.0.1:8080", request_timeout=1)

    def fail_upload(*_args, **_kwargs):
        raise RuntimeError("synthetic upload failure")

    client.upload = fail_upload
    record = client.run_one(
        script="ecommerce-product-demo",
        images=5,
        duration=12,
        label="failure-test",
        chunk_size=400_000,
        poll=0.01,
        output_root=tmp_path,
        overall_timeout=1,
    )

    assert record["failure_stage"] == "upload_image_1"
    assert "synthetic upload failure" in record["error"]
    assert record["outputs"] == []
    assert record["duration_ok"] is False


def test_peak_render_overlap_counts_only_render_intervals():
    records = [
        {"rendering_started_at": 10.0, "render_completed_at": 20.0},
        {"rendering_started_at": 12.0, "render_completed_at": 18.0},
        {"rendering_started_at": 14.0, "render_completed_at": 16.0},
        {"rendering_started_at": None, "render_completed_at": None},
    ]
    assert peak_render_overlap(records) == 3
