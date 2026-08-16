#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Environment-driven FrameFlow BFF diagnostic.

Exercises the production-facing BFF contract (chunk upload, render submission,
status polling) without ever handling MCP_API_TOKEN.  The
token belongs to the BFF and must not be sent by this client.

Examples:
  python frameflow_e2e.py single --images 8
  python frameflow_e2e.py parallel-two --images 8 --script-a ecommerce-product-demo \
      --script-b cinematic-montage
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import os
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests
from PIL import Image, ImageDraw


TERMINAL = {"published", "done", "success", "completed", "finished", "failed", "error"}


def retryable_poll_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return any(signature in message for signature in (
        "transport failed", "http 502", "http 503", "http 504",
        "connection reset", "remote end closed connection", "eof",
    ))


def png_bytes(index: int, label: str) -> bytes:
    image = Image.new("RGB", (540, 960), ((index * 67) % 256, (index * 97 + 31) % 256, (index * 131 + 83) % 256))
    draw = ImageDraw.Draw(image)
    for y in range(0, image.height, 48):
        draw.line((0, y, image.width, y), fill=(255, 255, 255), width=2)
    draw.text((32, image.height // 2), f"FrameFlow E2E\n{label}\nimage {index + 1}", fill=(255, 255, 255))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def dig(value, *keys):
    current = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


class BFF:
    def __init__(self, base: str, request_timeout: float):
        self.base = base.rstrip("/")
        self.request_timeout = request_timeout
        self.http = requests.Session()  # cookie ff_sid is deliberately per worker

    def request(self, method: str, path: str, **kwargs):
        started = time.monotonic()
        try:
            response = self.http.request(
                method, self.base + path, timeout=self.request_timeout, **kwargs
            )
        except requests.RequestException as exc:
            elapsed = time.monotonic() - started
            raise RuntimeError(
                f"{method} {path}: transport failed after {elapsed:.1f}s: "
                f"{type(exc).__name__}: {exc}"
            ) from exc
        if response.status_code >= 400:
            raise RuntimeError(f"{method} {path}: HTTP {response.status_code}: {response.text[:500]}")
        try:
            return response.json()
        except requests.JSONDecodeError as exc:
            raise RuntimeError(
                f"{method} {path}: non-JSON HTTP {response.status_code}: {response.text[:500]}"
            ) from exc

    def mcp(self, tool: str, args: dict):
        # Never add Authorization here. MCP_API_TOKEN stays inside the BFF.
        return self.request("POST", "/api/mcp", json={"tool": tool, "args": args})

    def upload(self, project: str, index: int, label: str, chunk_size: int):
        data = png_bytes(index, label)
        filename = f"{label}-{index + 1:02d}.png"
        started = self.mcp("upload_asset_chunk", {
            "operation": "start", "project_id": project, "filename": filename,
            "total_bytes": len(data), "mime_type": "image/png",
        })
        upload_id = started.get("upload_id") or dig(started, "data", "upload_id")
        if not upload_id:
            raise RuntimeError(f"chunk start did not return upload_id for {filename}: {started}")
        for offset in range(0, len(data), chunk_size):
            piece = data[offset:offset + chunk_size]
            result = self.mcp("upload_asset_chunk", {
                "operation": "append", "project_id": project, "filename": filename,
                "upload_id": upload_id, "offset": offset,
                "chunk_base64": base64.b64encode(piece).decode("ascii"),
            })
            if result.get("success") is False:
                raise RuntimeError(f"append failed for {filename}: {result}")
        result = self.mcp("upload_asset_chunk", {
            "operation": "complete", "project_id": project, "filename": filename,
            "upload_id": upload_id,
        })
        if result.get("success") is False:
            raise RuntimeError(f"complete failed for {filename}: {result}")

    def run_one(self, script: str, images: int, duration: float, label: str,
                chunk_size: int, poll: float, output_root: Path, overall_timeout: float):
        started = time.time()
        project = f"frameflow-e2e-{label}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}"
        record = {
            "label": label, "script_id": script, "project_id": project,
            "started_at": started, "render_job_id": None, "status": None,
            "failure_stage": None, "error": None,
        }
        stage = "upload"
        try:
            for index in range(images):
                stage = f"upload_image_{index + 1}"
                self.upload(project, index, label, chunk_size)
            stage = "submit_render"
            render = self.mcp("create_remotion_video_share", {
                "project_id": project, "script_id": script,
                "duration_per_image": duration, "aspect_ratio": "9:16",
                "title": f"FrameFlow E2E {label}",
            })
            job_id = render.get("render_job_id")
            if not job_id:
                raise RuntimeError(f"render response lacks render_job_id: {render}")
            record["render_job_id"] = job_id
            submitted_at = time.time()
        except Exception as exc:  # keep the rest of a load stage observable
            ended = time.time()
            record.update({
                "ended_at": ended,
                "elapsed_seconds": round(ended - started, 3),
                "failure_stage": stage,
                "error": f"{type(exc).__name__}: {exc}",
                "outputs": [],
                "duration_ok": False,
            })
            return record

        rendering_started_at = None
        render_completed_at = None
        final = None
        stage = "poll_render"
        consecutive_poll_errors = 0
        try:
            while time.time() - started < overall_timeout:
                try:
                    status = self.mcp("get_render_status", {"render_job_id": job_id})
                    consecutive_poll_errors = 0
                except RuntimeError as exc:
                    consecutive_poll_errors += 1
                    if retryable_poll_error(exc) and consecutive_poll_errors <= 3:
                        time.sleep(poll)
                        continue
                    raise
                if rendering_started_at is None and status.get("render_phase") == "rendering":
                    rendering_started_at = time.time()
                if render_completed_at is None and status.get("video_path"):
                    render_completed_at = time.time()
                state = str(status.get("status", "")).lower()
                if state in TERMINAL:
                    final = status
                    break
                time.sleep(poll)
            if final is None:
                raise TimeoutError(f"workflow exceeded {overall_timeout:.0f}s overall timeout")
        except Exception as exc:
            record["failure_stage"] = stage
            record["error"] = f"{type(exc).__name__}: {exc}"
        ended = time.time()
        files = find_outputs(output_root, project, job_id)
        probes = [probe(path) for path in files]
        record.update({"ended_at": ended, "submitted_at": submitted_at,
                       "rendering_started_at": rendering_started_at,
                       "render_completed_at": render_completed_at,
                       "queue_wait_seconds": round(rendering_started_at - submitted_at, 3) if rendering_started_at else None,
                       "render_seconds": round(render_completed_at - rendering_started_at, 3) if rendering_started_at and render_completed_at else None,
                       "elapsed_seconds": round(ended - started, 3), "status": final,
                       "outputs": probes,
                       "target_duration_seconds": round(duration * images, 3),
                       "duration_ok": duration_matches_target(probes, duration * images)})
        return record


def find_outputs(root: Path, project: str, job_id: str):
    if not root.exists():
        return []
    matches = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix.lower() in {".mp4", ".webm", ".mov"}:
            text = str(path)
            if project in text or job_id in text:
                matches.append(path)
    return matches


def probe(path: Path):
    result = {"path": str(path), "bytes": path.stat().st_size}
    try:
        raw = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                       "format=duration:stream=width,height", "-of", "json", str(path)],
                                      text=True, stderr=subprocess.STDOUT)
        result["ffprobe"] = json.loads(raw)
    except (OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        result["ffprobe_error"] = str(exc)
    return result


def duration_matches_target(probes, target: float) -> bool:
    for item in probes:
        streams = item.get("ffprobe", {}).get("streams", [])
        fmt = item.get("ffprobe", {}).get("format", {})
        raw = fmt.get("duration")
        if raw is None:
            continue
        try:
            if abs(float(raw) - target) <= 0.5:
                return True
        except (TypeError, ValueError):
            pass
    return False


def peak_render_overlap(records) -> int:
    events = []
    for record in records:
        start = record.get("rendering_started_at")
        end = record.get("render_completed_at")
        if start and end and end >= start:
            events.extend(((start, 1), (end, -1)))
    active = peak = 0
    for _timestamp, delta in sorted(events, key=lambda item: (item[0], item[1])):
        active += delta
        peak = max(peak, active)
    return peak


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("single", "parallel-two", "load"))
    parser.add_argument("--jobs", type=int, default=4, help="number of jobs in load mode (1-12)")
    parser.add_argument("--bff", default=os.getenv("FRAMEFLOW_BFF_URL", "http://localhost:8080"))
    parser.add_argument("--images", type=int, default=int(os.getenv("FRAMEFLOW_IMAGES", "8")))
    parser.add_argument("--duration", type=float, default=None,
                        help="seconds per image (default: 60/images; env FRAMEFLOW_DURATION_PER_IMAGE overrides)")
    parser.add_argument("--script-a", default=os.getenv("FRAMEFLOW_SCRIPT_A", "ecommerce-product-demo"))
    parser.add_argument("--script-b", default=os.getenv("FRAMEFLOW_SCRIPT_B", "cinematic-montage"))
    parser.add_argument("--chunk-size", type=int, default=400_000)
    parser.add_argument("--poll-seconds", type=float, default=5)
    parser.add_argument("--request-timeout-seconds", type=float,
                        default=float(os.getenv("FRAMEFLOW_REQUEST_TIMEOUT_SECONDS", "150")),
                        help="timeout for one BFF HTTP request")
    parser.add_argument("--timeout-seconds", type=float,
                        default=float(os.getenv("FRAMEFLOW_TIMEOUT_SECONDS", "1800")),
                        help="overall timeout for one upload+render workflow")
    parser.add_argument("--output-root", type=Path, default=Path(os.getenv("FRAMEFLOW_OUTPUT_ROOT", "projects")))
    parser.add_argument("--require-publish", action="store_true",
                        help="also fail unless the upstream publish/share stage succeeds")
    parser.add_argument("--remote-output", action="store_true",
                        help="outputs are on the render host; accept MCP video_path instead of local ffprobe")
    args = parser.parse_args()
    if not 5 <= args.images <= 10:
        parser.error("--images must be between 5 and 10")
    if args.duration is None:
        env_duration = os.getenv("FRAMEFLOW_DURATION_PER_IMAGE")
        args.duration = float(env_duration) if env_duration else 60.0 / args.images
    if args.duration <= 0:
        parser.error("--duration must be positive")
    if not args.bff:
        parser.error("BFF URL is required")
    if not 1 <= args.jobs <= 12:
        parser.error("--jobs must be between 1 and 12")
    if args.request_timeout_seconds <= 0 or args.timeout_seconds <= 0:
        parser.error("timeouts must be positive")

    def worker(script, label):
        return BFF(args.bff, args.request_timeout_seconds).run_one(
            script, args.images, args.duration, label, args.chunk_size,
            args.poll_seconds, args.output_root, args.timeout_seconds,
        )

    if args.mode == "single":
        records = [worker(args.script_a, "single")]
    elif args.mode == "parallel-two":
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, args.script_a, "parallel-a"), pool.submit(worker, args.script_b, "parallel-b")]
            records = [future.result() for future in futures]
    else:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = [
                pool.submit(worker, args.script_a if index % 2 == 0 else args.script_b, f"load-{index + 1:02d}")
                for index in range(args.jobs)
            ]
            records = [future.result() for future in futures]
    overlap = None
    if len(records) == 2:
        overlap = max(0.0, min(r["ended_at"] for r in records) - max(r["started_at"] for r in records))
    for record in records:
        terminal_status = str((record.get("status") or {}).get("status", "")).lower()
        remote_video = bool((record.get("status") or {}).get("video_path"))
        record["render_ok"] = remote_video if args.remote_output else bool(record.get("outputs")) and bool(record.get("duration_ok"))
        record["publish_ok"] = terminal_status in {"published", "done", "success", "completed", "finished"}
    report = {"mode": args.mode, "bff": args.bff, "images": args.images,
              "duration_per_image": args.duration, "require_publish": args.require_publish,
              "remote_output": args.remote_output, "jobs": len(records),
              "peak_render_overlap": peak_render_overlap(records),
              "records": records, "overlap_seconds": overlap}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if any(not r.get("render_ok") for r in records):
        return 2
    if args.require_publish and any(not r.get("publish_ok") for r in records):
        print("WARNING: render passed but publish/share did not complete", file=sys.stderr)
        return 3
    if len(records) == 2 and not overlap:
        print("WARNING: render intervals did not overlap; this is not evidence of parallel workers", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
