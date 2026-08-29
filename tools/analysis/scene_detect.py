"""Scene detection tool wrapping PySceneDetect.

Detects scene boundaries and shot changes in video. Falls back to
FFmpeg-based detection if PySceneDetect is not installed.
"""

from __future__ import annotations

import json
import math
import re
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class SceneDetect(BaseTool):
    name = "scene_detect"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "ffmpeg"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["cmd:ffmpeg"]
    install_instructions = (
        "FFmpeg is required. For better detection install PySceneDetect:\n"
        "pip install scenedetect[opencv]"
    )
    agent_skills = ["ffmpeg"]

    capabilities = [
        "detect_scenes",
        "detect_content_changes",
        "detect_threshold",
    ]

    input_schema = {
        "type": "object",
        "required": ["input_path"],
        "properties": {
            "input_path": {"type": "string"},
            "method": {
                "type": "string",
                "enum": ["content", "threshold", "adaptive"],
                "default": "content",
            },
            "threshold": {
                "type": "number",
                "description": "Detection threshold (method-dependent)",
            },
            "min_scene_length_seconds": {
                "type": "number",
                "minimum": 0.1,
                "default": 1.0,
            },
            "output_path": {"type": "string", "description": "Path for scene list JSON"},
        },
    }

    resource_profile = ResourceProfile(cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=100)
    idempotency_key_fields = ["input_path", "method", "threshold"]
    side_effects = ["writes scene list JSON to output_path"]
    user_visible_verification = [
        "Spot-check detected scene boundaries against the video",
    ]

    # FFmpeg can be surprisingly slow on 4K/H.265 sources.  Keep the timeout
    # proportional to the actual input while retaining a hard safety ceiling.
    _MAX_DETECTION_TIMEOUT_SECONDS = 900
    _LONG_VIDEO_SECONDS = 300
    _SEGMENT_SECONDS = 180

    def _has_pyscenedetect(self) -> bool:
        try:
            import scenedetect  # noqa: F401
            return True
        except ImportError:
            return False

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        start = time.time()
        # A detector instance may be reused by callers.  Never leak the
        # previous invocation's degraded diagnostics into a new run.
        self._detection_status = "completed"
        self._detection_diagnostics = []

        use_pyscenedetect = self._has_pyscenedetect()
        try:
            if use_pyscenedetect:
                scenes = self._detect_pyscenedetect(inputs)
            else:
                scenes = self._detect_ffmpeg(inputs)
        except Exception as exc:
            elapsed = time.time() - start
            return ToolResult(
                success=False,
                data={"status": "failed", "scene_count": 0},
                error=f"Scene detection failed: {exc}",
                duration_seconds=round(elapsed, 2),
            )

        elapsed = time.time() - start

        status = self._detection_status
        diagnostics = list(self._detection_diagnostics)

        # Write scene list together with degradation metadata so consumers that
        # read the artifact (rather than ToolResult) cannot miss partial failure.
        output_path = Path(
            inputs.get("output_path", str(input_path.with_suffix(".scenes.json")))
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(
                {"status": status, "diagnostics": diagnostics, "scenes": scenes},
                indent=2,
            ),
            encoding="utf-8",
        )

        return ToolResult(
            success=status in ("completed", "degraded"),
            data={
                "scene_count": len(scenes),
                "scenes": scenes,
                "method": "pyscenedetect" if use_pyscenedetect else "ffmpeg",
                "output": str(output_path),
                "status": status,
                "diagnostics": diagnostics,
                **(
                    {"warning": "Scene detection completed with partial segment failures"}
                    if status == "degraded"
                    else {}
                ),
            },
            artifacts=[str(output_path)],
            duration_seconds=round(elapsed, 2),
        )

    def _probe_media_info(self, input_path: str) -> tuple[float, int, int]:
        """Return duration, width and height, failing loudly if media is unreadable."""
        cmd = [
            "ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_entries", "format=duration:stream=width,height,codec_type",
            input_path,
        ]
        result = self.run_command(cmd, timeout=30)
        payload = json.loads(result.stdout)
        duration = float(payload.get("format", {}).get("duration", 0) or 0)
        video = next((s for s in payload.get("streams", []) if s.get("codec_type") == "video"), {})
        width = int(video.get("width", 0) or 0)
        height = int(video.get("height", 0) or 0)
        if duration <= 0:
            raise ValueError("ffprobe returned no positive video duration")
        return duration, width, height

    @classmethod
    def _detection_timeout(cls, duration: float, width: int = 0, height: int = 0) -> int:
        """Estimate an FFmpeg timeout from duration and pixel count, with a hard cap."""
        megapixels = max(1.0, (width * height) / 1_000_000) if width and height else 1.0
        # 1x realtime for SD, up to 4x for 4K, plus startup/probe allowance.
        multiplier = min(4.0, max(1.0, math.sqrt(megapixels)))
        return min(cls._MAX_DETECTION_TIMEOUT_SECONDS, max(60, math.ceil(30 + duration * multiplier)))

    @staticmethod
    def _build_scenes(change_points: list[float], total_dur: float, min_scene_len: float) -> list[dict]:
        """Normalize, de-duplicate and materialize scene boundaries."""
        points = sorted({round(max(0.0, min(total_dur, p)), 3) for p in change_points})
        kept: list[float] = []
        for point in points:
            if not kept or point - kept[-1] >= min_scene_len:
                kept.append(point)
        points = kept
        if not points or points[0] != 0.0:
            points.insert(0, 0.0)
        if total_dur - points[-1] < min_scene_len and len(points) > 1:
            points[-1] = round(total_dur, 3)
        elif points[-1] != round(total_dur, 3):
            points.append(round(total_dur, 3))
        return [
            {"index": i, "start_seconds": start, "end_seconds": end,
             "duration_seconds": round(end - start, 3)}
            for i, (start, end) in enumerate(zip(points, points[1:])) if end > start
        ]

    def _detect_pyscenedetect(self, inputs: dict[str, Any]) -> list[dict]:
        """Use PySceneDetect for scene detection."""
        from scenedetect import open_video, SceneManager
        from scenedetect.detectors import ContentDetector, ThresholdDetector, AdaptiveDetector

        input_path = str(inputs["input_path"])
        method = inputs.get("method", "content")
        threshold = inputs.get("threshold")
        min_scene_len = inputs.get("min_scene_length_seconds", 1.0)

        video = open_video(input_path)
        scene_manager = SceneManager()

        if method == "content":
            detector = ContentDetector(
                threshold=threshold or 27.0,
                min_scene_len=int(min_scene_len * video.frame_rate),
            )
        elif method == "threshold":
            detector = ThresholdDetector(
                threshold=threshold or 12.0,
                min_scene_len=int(min_scene_len * video.frame_rate),
            )
        elif method == "adaptive":
            detector = AdaptiveDetector(
                adaptive_threshold=threshold or 3.0,
                min_scene_len=int(min_scene_len * video.frame_rate),
            )
        else:
            detector = ContentDetector()

        scene_manager.add_detector(detector)
        scene_manager.detect_scenes(video)
        scene_list = scene_manager.get_scene_list()

        scenes = []
        for i, (scene_start, scene_end) in enumerate(scene_list):
            scenes.append({
                "index": i,
                "start_seconds": round(scene_start.get_seconds(), 3),
                "end_seconds": round(scene_end.get_seconds(), 3),
                "duration_seconds": round(
                    scene_end.get_seconds() - scene_start.get_seconds(), 3
                ),
            })

        return scenes

    @staticmethod
    def _escape_lavfi_movie_path(path: str) -> str:
        """Escape a path for FFmpeg lavfi movie=... without allowing filter injection."""
        normalized = path.replace("\\", "/")
        if "'" in normalized:
            raise ValueError("FFmpeg lavfi movie paths containing single quotes are unsupported")
        escaped = []
        for char in normalized:
            if char in "\\:,[];":
                escaped.append("\\" + char)
            else:
                escaped.append(char)
        return "".join(escaped)

    def _detect_ffmpeg(self, inputs: dict[str, Any]) -> list[dict]:
        """Fallback: use FFmpeg scene change filter."""
        input_path = str(inputs["input_path"])
        threshold = inputs.get("threshold", 0.3)
        min_scene_len = inputs.get("min_scene_length_seconds", 1.0)
        self._detection_status = "completed"
        self._detection_diagnostics = []
        total_dur, width, height = self._probe_media_info(input_path)
        timeout = self._detection_timeout(total_dur, width, height)

        # Long/high-resolution files are processed in bounded chunks so one
        # slow decode cannot consume the entire request timeout.  Overlap by
        # min_scene_len and de-duplicate boundaries after applying offsets.
        if total_dur > self._LONG_VIDEO_SECONDS:
            return self._detect_ffmpeg_segmented(
                input_path, threshold, min_scene_len, total_dur, width, height
            )
        escaped_input = self._escape_lavfi_movie_path(input_path)

        cmd = [
            "ffprobe",
            "-v", "quiet",
            "-show_entries", "frame=pts_time",
            "-of", "json",
            "-f", "lavfi",
            f"movie='{escaped_input}',select='gt(scene,{threshold})'",
        ]

        try:
            result = self.run_command(cmd, timeout=timeout)
            data = json.loads(result.stdout)
        except Exception as exc:
            # If ffprobe lavfi approach fails, try a simpler method
            self._detection_diagnostics.append(f"lavfi detection failed: {exc}")
            return self._detect_ffmpeg_simple(input_path, threshold, min_scene_len, total_dur, width, height)

        change_points = [0.0]
        for frame in data.get("frames", []):
            ts = float(frame.get("pts_time", 0))
            if ts - change_points[-1] >= min_scene_len:
                change_points.append(ts)

        return self._build_scenes(change_points, total_dur, min_scene_len)

    def _detect_ffmpeg_segmented(
        self, input_path: str, threshold: float, min_scene_len: float,
        total_dur: float, width: int, height: int,
    ) -> list[dict]:
        """Detect long videos chunk-by-chunk and merge timestamps globally."""
        all_points: list[float] = [0.0]
        failures = 0
        segment_start = 0.0
        while segment_start < total_dur:
            segment_end = min(total_dur, segment_start + self._SEGMENT_SECONDS)
            # A small overlap catches a cut exactly at a chunk boundary.
            seek_start = max(0.0, segment_start - min_scene_len)
            segment_length = segment_end - seek_start
            cmd = [
                "ffmpeg", "-ss", str(seek_start), "-t", str(segment_length),
                "-i", input_path, "-vf", f"select='gt(scene,{threshold})',showinfo",
                "-f", "null", "-",
            ]
            try:
                result = self.run_command(
                    cmd, timeout=self._detection_timeout(segment_length, width, height)
                )
                for match in re.finditer(r"pts_time:(\d+\.?\d*)", result.stderr or ""):
                    ts = seek_start + float(match.group(1))
                    if segment_start - min_scene_len <= ts <= segment_end + min_scene_len:
                        all_points.append(ts)
            except Exception as exc:
                failures += 1
                self._detection_diagnostics.append(
                    f"segment {seek_start:.3f}-{segment_end:.3f} failed: {exc}"
                )
            segment_start = segment_end

        scenes = self._build_scenes(all_points, total_dur, min_scene_len)
        if failures:
            self._detection_status = "degraded" if scenes and len(scenes) > 1 else "failed"
            if self._detection_status == "failed":
                raise RuntimeError("all scene-detection segments failed")
        return scenes

    def _detect_ffmpeg_simple(
        self, input_path: str, threshold: float, min_scene_len: float,
        total_dur: float | None = None, width: int = 0, height: int = 0,
    ) -> list[dict]:
        """Simplest fallback: split into uniform segments."""
        if total_dur is None:
            total_dur, width, height = self._probe_media_info(input_path)

        # Use select filter to find scene changes via stdout
        cmd = [
            "ffmpeg",
            "-i", input_path,
            "-vf", f"select='gt(scene,{threshold})',showinfo",
            "-f", "null", "-",
        ]
        try:
            result = self.run_command(
                cmd, timeout=self._detection_timeout(total_dur, width, height)
            )
            output = result.stderr
        except Exception as exc:
            self._detection_status = "failed"
            self._detection_diagnostics.append(f"simple detection failed: {exc}")
            raise RuntimeError("FFmpeg scene detection timed out or failed") from exc

        change_points = [0.0]
        for match in re.finditer(r"pts_time:(\d+\.?\d*)", output):
            ts = float(match.group(1))
            if ts - change_points[-1] >= min_scene_len:
                change_points.append(ts)
        return self._build_scenes(change_points, total_dur, min_scene_len)
