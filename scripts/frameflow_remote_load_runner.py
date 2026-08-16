#!/usr/bin/env python3
"""Run staged FrameFlow load tests while collecting authenticated remote metrics."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

if __package__:
    from scripts.frameflow_capacity_analyzer import analyze, sample_epoch
else:
    from frameflow_capacity_analyzer import analyze, sample_epoch


REPO_ROOT = Path(__file__).resolve().parent.parent
E2E_SCRIPT = REPO_ROOT / "frameflow" / "bff" / "frameflow_e2e.py"


class MetricsCollector:
    def __init__(self, observer: str, token: str, interval: float) -> None:
        self.url = observer.rstrip("/") + "/v1/metrics/latest"
        self.headers = {"Authorization": f"Bearer {token}"}
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self._timestamps: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def fetch(self) -> dict[str, Any]:
        response = requests.get(self.url, headers=self.headers, timeout=5)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict) or not value.get("timestamp"):
            raise RuntimeError("observer returned no timestamped metric sample")
        return value

    def _poll(self) -> None:
        while not self._stop.is_set():
            try:
                sample = self.fetch()
                timestamp = str(sample["timestamp"])
                if timestamp not in self._timestamps:
                    self._timestamps.add(timestamp)
                    self.samples.append(sample)
            except Exception as exc:  # collector errors are reported with the stage
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.interval)

    def start(self) -> dict[str, Any]:
        first = self.fetch()
        self._timestamps.add(str(first["timestamp"]))
        self.samples.append(first)
        self._thread = threading.Thread(target=self._poll, daemon=True)
        self._thread.start()
        return first

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(2, self.interval * 2))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def write_jsonl(path: Path, values: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for value in values:
            handle.write(json.dumps(value, ensure_ascii=False) + "\n")


def parse_stages(raw: str) -> list[int]:
    try:
        stages = [int(item.strip()) for item in raw.split(",") if item.strip()]
    except ValueError as exc:
        raise argparse.ArgumentTypeError("stages must be comma-separated integers") from exc
    if not stages or stages != sorted(set(stages)) or any(stage < 1 or stage > 12 for stage in stages):
        raise argparse.ArgumentTypeError("stages must be unique ascending values between 1 and 12")
    return stages


def e2e_command(args: argparse.Namespace, jobs: int) -> list[str]:
    command = [
        sys.executable, str(E2E_SCRIPT), "load", "--jobs", str(jobs),
        "--bff", args.bff, "--images", str(args.images),
        "--request-timeout-seconds", str(args.request_timeout_seconds),
        "--timeout-seconds", str(args.timeout_seconds),
        "--poll-seconds", str(args.poll_seconds), "--remote-output",
    ]
    if args.require_publish:
        command.append("--require-publish")
    return command


def e2e_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("FRAMEFLOW_OBSERVER_TOKEN", None)
    return environment


def run_stage(args: argparse.Namespace, jobs: int, output_dir: Path, token: str) -> dict[str, Any]:
    collector = MetricsCollector(args.observer, token, args.metrics_interval)
    first = collector.start()
    first_epoch = sample_epoch(first)
    clock_skew = abs(time.time() - first_epoch) if first_epoch is not None else None
    command = e2e_command(args, jobs)
    started = time.time()
    try:
        result = subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            env=e2e_environment(),
            timeout=args.timeout_seconds + args.request_timeout_seconds + 60,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = None
        process_error = f"controller timeout: {exc}"
    else:
        process_error = None
    finally:
        collector.stop()
    ended = time.time()

    metrics_path = output_dir / f"metrics-jobs-{jobs}.jsonl"
    report_path = output_dir / f"e2e-jobs-{jobs}.json"
    assessment_path = output_dir / f"assessment-jobs-{jobs}.json"
    write_jsonl(metrics_path, collector.samples)

    report = None
    if result is not None:
        try:
            report = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            process_error = f"invalid E2E JSON: {exc}; stdout={result.stdout[:500]!r}"
    if report is None:
        report = {
            "mode": "load", "jobs": jobs, "records": [],
            "controller_error": process_error,
        }
    write_json(report_path, report)

    if report.get("records"):
        assessment = analyze(report, collector.samples)
    else:
        assessment = {
            "assessment": "unstable",
            "suggested_action": "stop_and_diagnose",
            "bottlenecks": ["controller_or_e2e_failure"],
            "failures": [{"error": process_error}],
        }
    assessment["controller"] = {
        "jobs": jobs,
        "started_at": started,
        "ended_at": ended,
        "clock_skew_seconds": round(clock_skew, 2) if clock_skew is not None else None,
        "observer_errors": collector.errors[-20:],
        "e2e_exit_code": result.returncode if result is not None else None,
        "e2e_stderr": result.stderr[-2000:] if result is not None else None,
    }
    if clock_skew is None or clock_skew > 10:
        assessment["assessment"] = "unstable"
        assessment["suggested_action"] = "stop_and_fix_clock_sync"
        assessment.setdefault("bottlenecks", []).append("clock_skew")
    if len(collector.samples) < 3:
        assessment["assessment"] = "unstable"
        assessment["suggested_action"] = "stop_and_fix_observer"
        assessment.setdefault("bottlenecks", []).append("insufficient_live_metrics")
    write_json(assessment_path, assessment)
    return assessment


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bff", default="http://192.168.20.173:8080")
    parser.add_argument("--observer", default="http://192.168.20.173:9910")
    parser.add_argument("--stages", type=parse_stages, default=parse_stages("1,2,4,5,6"))
    parser.add_argument("--images", type=int, default=8)
    parser.add_argument("--metrics-interval", type=float, default=1)
    parser.add_argument("--request-timeout-seconds", type=float, default=150)
    parser.add_argument("--timeout-seconds", type=float, default=1800)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--cooldown-seconds", type=float, default=30)
    parser.add_argument("--require-publish", action="store_true")
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    if not 5 <= args.images <= 10:
        parser.error("--images must be between 5 and 10")
    if args.metrics_interval < 0.5:
        parser.error("--metrics-interval must be at least 0.5 seconds")
    if args.cooldown_seconds < 0:
        parser.error("--cooldown-seconds cannot be negative")

    token = os.environ.get("FRAMEFLOW_OBSERVER_TOKEN", "")
    if len(token) < 24:
        parser.error("FRAMEFLOW_OBSERVER_TOKEN must contain the remote observer token")
    output_dir = args.output_dir or (
        REPO_ROOT / "local_run" / f"frameflow-load-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    stages = []
    for stage_index, jobs in enumerate(args.stages):
        print(f"starting jobs={jobs}", flush=True)
        try:
            assessment = run_stage(args, jobs, output_dir, token)
        except Exception as exc:
            assessment = {
                "assessment": "unstable", "suggested_action": "stop_and_diagnose",
                "bottlenecks": ["observer_preflight"],
                "failures": [{"error": f"{type(exc).__name__}: {exc}"}],
            }
            write_json(output_dir / f"assessment-jobs-{jobs}.json", assessment)
        stages.append({"jobs": jobs, **assessment})
        print(f"jobs={jobs} assessment={assessment['assessment']}", flush=True)
        if assessment["assessment"] != "stable":
            break
        if stage_index < len(args.stages) - 1 and args.cooldown_seconds > 0:
            print(f"cooldown seconds={args.cooldown_seconds}", flush=True)
            time.sleep(args.cooldown_seconds)

    stable_jobs = [item["jobs"] for item in stages if item["assessment"] == "stable"]
    summary = {
        "output_dir": str(output_dir),
        "requested_stages": args.stages,
        "completed_stages": [item["jobs"] for item in stages],
        "max_stable_jobs": max(stable_jobs) if stable_jobs else 0,
        "stages": stages,
    }
    write_json(output_dir / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if stages and all(item["assessment"] == "stable" for item in stages) else 2


if __name__ == "__main__":
    raise SystemExit(main())
