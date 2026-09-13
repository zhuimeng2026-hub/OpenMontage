#!/usr/bin/env python3
"""Combine a FrameFlow E2E report with observer JSONL into a capacity assessment."""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


def percentile(values: Iterable[float], percent: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * percent / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def rounded(value: float | None) -> float | None:
    return round(value, 2) if value is not None else None


def nested(sample: dict[str, Any], *keys: str) -> float | None:
    value: Any = sample
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return float(value) if isinstance(value, (int, float)) else None


def metric_timestamp_epoch(sample: dict[str, Any]) -> float | None:
    raw = sample.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def sample_epoch(sample: dict[str, Any]) -> float | None:
    received = sample.get("_collector_received_at")
    if isinstance(received, (int, float)):
        return float(received)
    return metric_timestamp_epoch(sample)


def load_metrics(path: Path) -> list[dict[str, Any]]:
    samples = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and value.get("type") != "summary" and sample_epoch(value) is not None:
            samples.append(value)
    return samples


def numeric(samples: list[dict[str, Any]], *keys: str) -> list[float]:
    return [value for sample in samples if (value := nested(sample, *keys)) is not None]


def series_stats(values: list[float]) -> dict[str, float | None]:
    return {
        "avg": rounded(statistics.fmean(values)) if values else None,
        "p95": rounded(percentile(values, 95)),
        "max": rounded(max(values)) if values else None,
    }


def process_peaks(samples: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    names = sorted({
        name
        for sample in samples
        for name in (sample.get("processes") or {})
    })
    result = {}
    for name in names:
        items = [(sample.get("processes") or {}).get(name) or {} for sample in samples]
        result[name] = {
            "max_count": max((float(item.get("count", 0)) for item in items), default=0),
            "max_rss_mb": max((float(item.get("rss_mb", 0)) for item in items), default=0),
            "max_cpu_percent": max((float(item.get("cpu_percent", 0)) for item in items), default=0),
        }
    return result


def analyze(report: dict[str, Any], metrics: list[dict[str, Any]]) -> dict[str, Any]:
    records = report.get("records") or []
    starts = [float(item["started_at"]) for item in records if item.get("started_at") is not None]
    ends = [float(item["ended_at"]) for item in records if item.get("ended_at") is not None]
    if not starts or not ends:
        raise ValueError("report has no complete job time range")
    window_start, window_end = min(starts), max(ends)
    window = [
        sample for sample in metrics
        if (epoch := sample_epoch(sample)) is not None and window_start - 2 <= epoch <= window_end + 2
    ]

    elapsed = [float(item["elapsed_seconds"]) for item in records if item.get("elapsed_seconds") is not None]
    queue_wait = [float(item["queue_wait_seconds"]) for item in records if item.get("queue_wait_seconds") is not None]
    render_time = [float(item["render_seconds"]) for item in records if item.get("render_seconds") is not None]
    successful = sum(bool(item.get("render_ok")) for item in records)
    wall_seconds = max(0.001, window_end - window_start)

    cpu = numeric(window, "cpu_percent")
    memory = numeric(window, "memory", "used_percent")
    swap = numeric(window, "memory", "swap_used_gb")
    disk_busy = numeric(window, "disk", "busy_percent")
    load1 = numeric(window, "load", "1m")
    cpu_count_values = numeric(window, "machine", "cpu_count")
    swap_growth = (swap[-1] - swap[0]) if len(swap) >= 2 else 0.0

    thresholds = {
        "all_jobs_rendered": successful == len(records) and bool(records),
        "metrics_present": len(window) >= 3,
        "cpu_p95_below_92": (percentile(cpu, 95) or 0) < 92,
        "memory_max_below_85": (max(memory) if memory else 100) < 85,
        "swap_growth_below_0_25_gb": swap_growth < 0.25,
        "disk_p95_below_90": (percentile(disk_busy, 95) or 0) < 90,
    }
    stable = all(thresholds.values())
    bottlenecks = []
    if cpu and (percentile(cpu, 95) or 0) >= 85:
        bottlenecks.append("cpu")
    if memory and max(memory) >= 80:
        bottlenecks.append("memory")
    if swap_growth >= 0.1:
        bottlenecks.append("swap")
    if disk_busy and (percentile(disk_busy, 95) or 0) >= 80:
        bottlenecks.append("disk")
    if successful != len(records):
        bottlenecks.append("job_failures")
    if not window:
        bottlenecks.append("missing_metrics")

    return {
        "assessment": "stable" if stable else "unstable",
        "suggested_action": "increase_to_next_step" if stable else "stop_and_diagnose",
        "bottlenecks": bottlenecks,
        "thresholds": thresholds,
        "workload": {
            "jobs": len(records),
            "successful_jobs": successful,
            "failed_jobs": len(records) - successful,
            "wall_seconds": rounded(wall_seconds),
            "throughput_jobs_per_hour": rounded(successful / wall_seconds * 3600),
            "peak_render_overlap": report.get("peak_render_overlap"),
            "elapsed_seconds": series_stats(elapsed),
            "queue_wait_seconds": series_stats(queue_wait),
            "render_seconds": series_stats(render_time),
        },
        "resources": {
            "metric_samples": len(window),
            "cpu_count": int(max(cpu_count_values)) if cpu_count_values else None,
            "cpu_percent": series_stats(cpu),
            "memory_percent": series_stats(memory),
            "swap_used_gb": {**series_stats(swap), "growth": rounded(swap_growth)},
            "load1": series_stats(load1),
            "disk_busy_percent": series_stats(disk_busy),
            "process_peaks": process_peaks(window),
        },
        "failures": [
            {
                "label": item.get("label"),
                "stage": item.get("failure_stage"),
                "error": item.get("error"),
                "status": (item.get("status") or {}).get("status"),
            }
            for item in records if not item.get("render_ok")
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True, help="frameflow_e2e.py JSON report")
    parser.add_argument("--metrics", type=Path, required=True, help="frameflow_perf_monitor.py JSONL")
    parser.add_argument("--output", type=Path, help="optional assessment JSON path")
    args = parser.parse_args()

    assessment = analyze(
        json.loads(args.report.read_text(encoding="utf-8")),
        load_metrics(args.metrics),
    )
    rendered = json.dumps(assessment, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if assessment["assessment"] == "stable" else 2


if __name__ == "__main__":
    raise SystemExit(main())
