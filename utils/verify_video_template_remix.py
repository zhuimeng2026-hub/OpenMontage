#!/usr/bin/env python3
"""Local verification for the template-remix pipeline and scene detection.

Run without arguments for a self-contained FFmpeg smoke test::

    python utils/verify_video_template_remix.py

Pass ``--input`` to verify a real local video.  The script intentionally forces
SceneDetect's FFmpeg fallback so the long-video timeout and multi-shot path are
exercised even on machines that also have PySceneDetect installed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a local command and include a useful tail on failure."""
    try:
        return subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"Required executable is unavailable: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        tail = "\n".join(detail[-8:])
        raise RuntimeError(f"Command failed ({exc.returncode}): {' '.join(command)}\n{tail}") from exc


def _verify_pipeline() -> dict[str, Any]:
    from lib.config_model import OpenMontageConfig
    from lib.pipeline_loader import get_default_pipeline_name, load_pipeline

    configured = OpenMontageConfig.load().pipeline.default_type
    resolved = get_default_pipeline_name()
    manifest = load_pipeline(resolved)
    if configured != "video-template-remix" or resolved != "video-template-remix":
        raise AssertionError(
            f"Expected video-template-remix as default, configured={configured!r}, resolved={resolved!r}"
        )
    required_stages = ["idea", "script", "scene_plan", "assets", "edit", "compose", "publish"]
    actual_stages = [stage["name"] for stage in manifest.get("stages", [])]
    if actual_stages != required_stages:
        raise AssertionError(f"Unexpected pipeline stages: {actual_stages!r}")
    return {"default_pipeline": resolved, "stages": actual_stages}


def _make_fixture(path: Path) -> None:
    """Create a six-second video with three visually distinct hard-cut shots."""
    _run([
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", "color=c=red:s=320x240:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=blue:s=320x240:d=2:r=25",
        "-f", "lavfi", "-i", "color=c=green:s=320x240:d=2:r=25",
        "-filter_complex", "[0:v][1:v][2:v]concat=n=3:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path),
    ])


def _verify_scene_detection(input_path: Path, output_path: Path, fixture: bool) -> dict[str, Any]:
    from tools.analysis.scene_detect import SceneDetect

    detector = SceneDetect()
    # Exercise the FFmpeg implementation under test, regardless of optional
    # PySceneDetect availability on the host.
    detector._has_pyscenedetect = lambda: False  # type: ignore[method-assign]
    result = detector.execute({
        "input_path": str(input_path),
        "threshold": 0.1,
        "min_scene_length_seconds": 0.5,
        "output_path": str(output_path),
    })
    if not result.success:
        raise AssertionError(f"Scene detection failed: {result.error}")
    if result.data.get("status") not in {"completed", "degraded"}:
        raise AssertionError(f"Unexpected scene-detection status: {result.data.get('status')!r}")
    scene_count = int(result.data.get("scene_count", 0))
    if fixture and scene_count < 3:
        raise AssertionError(f"Fixture should contain at least 3 scenes, got {scene_count}")
    if not output_path.exists():
        raise AssertionError(f"Scene artifact was not written: {output_path}")
    artifact = json.loads(output_path.read_text(encoding="utf-8"))
    if artifact.get("status") != result.data.get("status"):
        raise AssertionError("Scene artifact status does not match ToolResult")
    return {
        "scene_status": result.data.get("status"),
        "scene_count": scene_count,
        "output": str(output_path),
        "diagnostics": result.data.get("diagnostics", []),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="Optional local video to verify")
    parser.add_argument("--output", type=Path, help="Optional scene JSON output path")
    args = parser.parse_args()

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("FAIL: ffmpeg and ffprobe must both be available on PATH", file=sys.stderr)
        return 2

    try:
        pipeline = _verify_pipeline()
        if args.input:
            input_path = args.input.resolve()
            if not input_path.is_file():
                raise FileNotFoundError(f"Input video not found: {input_path}")
            if args.output:
                output_path = args.output.resolve()
                output_path.parent.mkdir(parents=True, exist_ok=True)
                scene = _verify_scene_detection(input_path, output_path, fixture=False)
            else:
                with tempfile.TemporaryDirectory(prefix="openmontage-verify-") as temp_dir:
                    scene = _verify_scene_detection(input_path, Path(temp_dir) / "scenes.json", fixture=False)
        else:
            with tempfile.TemporaryDirectory(prefix="openmontage-verify-") as temp_dir:
                fixture_path = Path(temp_dir) / "three-cuts.mp4"
                scene_path = args.output.resolve() if args.output else Path(temp_dir) / "scenes.json"
                if args.output:
                    scene_path.parent.mkdir(parents=True, exist_ok=True)
                _make_fixture(fixture_path)
                scene = _verify_scene_detection(fixture_path, scene_path, fixture=True)
        print(json.dumps({"ok": True, **pipeline, **scene}, ensure_ascii=False, indent=2))
        return 0
    except (AssertionError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
