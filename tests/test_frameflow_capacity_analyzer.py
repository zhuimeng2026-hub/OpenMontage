from __future__ import annotations

from scripts.frameflow_capacity_analyzer import analyze, percentile


def sample(timestamp: str, cpu: float, memory: float, disk: float, swap: float = 0) -> dict:
    return {
        "timestamp": timestamp,
        "machine": {"cpu_count": 12},
        "cpu_percent": cpu,
        "memory": {"used_percent": memory, "swap_used_gb": swap},
        "load": {"1m": cpu / 10},
        "disk": {"busy_percent": disk},
        "processes": {"remotion": {"count": 2, "rss_mb": 500, "cpu_percent": cpu / 2}},
    }


def report(render_ok: bool = True) -> dict:
    return {
        "peak_render_overlap": 2,
        "records": [
            {
                "label": "one",
                "started_at": 1767225600,
                "ended_at": 1767225660,
                "elapsed_seconds": 60,
                "queue_wait_seconds": 2,
                "render_seconds": 55,
                "render_ok": render_ok,
                "failure_stage": None if render_ok else "poll_render",
                "error": None if render_ok else "failed",
                "status": {"status": "rendered" if render_ok else "failed"},
            }
        ],
    }


def test_percentile_interpolates():
    assert percentile([0, 10], 95) == 9.5
    assert percentile([], 95) is None


def test_analyze_stable_case():
    metrics = [
        sample("2026-01-01T00:00:00+00:00", 40, 50, 20),
        sample("2026-01-01T00:00:30+00:00", 50, 55, 30),
        sample("2026-01-01T00:01:00+00:00", 60, 60, 40),
    ]
    result = analyze(report(), metrics)
    assert result["assessment"] == "stable"
    assert result["workload"]["successful_jobs"] == 1
    assert result["resources"]["cpu_count"] == 12
    assert result["resources"]["process_peaks"]["remotion"]["max_count"] == 2


def test_analyze_rejects_failed_or_saturated_case():
    metrics = [
        sample("2026-01-01T00:00:00+00:00", 99, 86, 95, 0),
        sample("2026-01-01T00:00:30+00:00", 99, 90, 99, 0.2),
        sample("2026-01-01T00:01:00+00:00", 99, 92, 100, 0.5),
    ]
    result = analyze(report(render_ok=False), metrics)
    assert result["assessment"] == "unstable"
    assert result["suggested_action"] == "stop_and_diagnose"
    assert {"cpu", "memory", "swap", "disk", "job_failures"}.issubset(result["bottlenecks"])
