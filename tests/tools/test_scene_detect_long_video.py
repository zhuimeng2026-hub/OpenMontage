"""Tests for SceneDetect's long-video segmented detection path.

Long sources (>5 min) are processed in bounded 180-second chunks so one
slow decode cannot consume the entire request timeout. The tests below
pin the timeout heuristic, the offset/deduplication merge, the degraded
fallback when a single segment times out, and the analyzer's contract
that a degraded detection is still surfaced to the caller (not silently
discarded).
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.analysis.scene_detect import SceneDetect
from tools.analysis.video_analyzer import VideoAnalyzer
from tools.base_tool import ToolResult


class SceneDetectLongVideoTests(unittest.TestCase):
    def test_timeout_scales_with_duration_and_resolution_and_is_capped(self):
        short_sd = SceneDetect._detection_timeout(duration=30, width=1280, height=720)
        long_4k = SceneDetect._detection_timeout(duration=600, width=3840, height=2160)
        huge_4k = SceneDetect._detection_timeout(duration=10000, width=3840, height=2160)
        self.assertGreater(long_4k, short_sd)
        self.assertEqual(huge_4k, SceneDetect._MAX_DETECTION_TIMEOUT_SECONDS)

    def test_scene_min_length_compares_with_last_kept_boundary(self):
        # 0.5 is within min_scene_len=1.0 of 0.0 → must be dropped. 1.0 is
        # exactly at the boundary so it is kept. End-cap is appended.
        scenes = SceneDetect._build_scenes(
            [0.0, 0.5, 1.0], total_dur=2.0, min_scene_len=1.0
        )
        self.assertEqual([s["start_seconds"] for s in scenes], [0.0, 1.0])
        self.assertEqual([s["end_seconds"] for s in scenes], [1.0, 2.0])

    def test_execute_resets_status_when_detector_instance_is_reused(self):
        # A detector may be reused across calls; a previous degraded status
        # must NOT bleed into a clean run.
        detector = SceneDetect()
        detector._has_pyscenedetect = Mock(return_value=True)  # type: ignore[attr-defined]
        detector._detect_pyscenedetect = Mock(  # type: ignore[attr-defined]
            return_value=[
                {"index": 0, "start_seconds": 0.0, "end_seconds": 2.0,
                 "duration_seconds": 2.0},
            ]
        )

        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            detector._detection_status = "degraded"
            detector._detection_diagnostics = ["old run"]
            result = detector.execute({"input_path": str(input_path)})
            self.assertTrue(result.success)
            self.assertEqual(result.data["status"], "completed")
            self.assertEqual(result.data["diagnostics"], [])

    def test_segmented_detection_applies_offsets_and_deduplicates_boundary(self):
        detector = SceneDetect()
        detector._probe_media_info = Mock(return_value=(360.0, 1920, 1080))  # type: ignore[attr-defined]

        # Per-segment mock: emit exactly one cut at chunk-local 175s (so
        # when offset by seek_start=0 for chunk 0 we get ts=175; when
        # offset by seek_start=179 for chunk 1 we get ts=354).
        def run_segment(cmd, timeout):
            return Mock(
                stderr="[ffmpeg] frame pts_time:175.000\n",
                stdout="",
            )

        detector.run_command = run_segment  # type: ignore[attr-defined]
        scenes = detector._detect_ffmpeg(
            {"input_path": "long.mp4", "threshold": 0.3, "min_scene_length_seconds": 1.0}
        )
        # We expect cuts at 0 (implicit), 175, 354, and the end cap at 360.
        starts = [s["start_seconds"] for s in scenes]
        self.assertEqual(starts, [0.0, 175.0, 354.0])
        # Every scene must have a positive duration.
        self.assertTrue(all(s["end_seconds"] - s["start_seconds"] > 0 for s in scenes))

    def test_partial_segment_failure_is_degraded_but_keeps_scenes(self):
        detector = SceneDetect()
        detector._probe_media_info = Mock(return_value=(360.0, 1280, 720))  # type: ignore[attr-defined]
        detector._has_pyscenedetect = Mock(return_value=False)  # type: ignore[attr-defined]

        def run_segment(cmd, timeout):
            # First segment OK, second segment raises a timeout.
            if "180" in str(cmd):
                raise TimeoutError("segment timeout")
            return Mock(stderr="[ffmpeg] frame pts_time:90.000\n", stdout="")

        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            detector.run_command = run_segment  # type: ignore[attr-defined]
            result = detector.execute(
                {"input_path": str(input_path), "min_scene_length_seconds": 1.0}
            )
            self.assertGreater(len(result.data["scenes"]), 1)
            self.assertEqual(result.data["status"], "degraded")
            self.assertTrue(any("segment timeout" in d for d in detector._detection_diagnostics))

            # The artifact on disk must also carry the degraded status.
            artifact = Path(str(input_path).replace(".mp4", ".scenes.json"))
            self.assertIn('"status": "degraded"', artifact.read_text(encoding="utf-8"))

    def test_timeout_does_not_return_a_successful_single_scene(self):
        detector = SceneDetect()
        detector._has_pyscenedetect = Mock(return_value=False)  # type: ignore[attr-defined]
        detector._detect_ffmpeg = Mock(side_effect=TimeoutError("ffmpeg timed out"))  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as td:
            input_path = Path(td) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            output_path = Path(td) / "scenes.json"
            result = detector.execute(
                {"input_path": str(input_path), "output_path": str(output_path)}
            )
            self.assertFalse(result.success)
            self.assertEqual(result.data["status"], "failed")
            self.assertIn("timed out", (result.error or "").lower())
            self.assertFalse(output_path.exists())

    def test_video_analyzer_retains_and_reports_degraded_scenes(self):
        # Patch VideoAnalyzer's underlying SceneDetect.execute to return a
        # degraded ToolResult. The analyzer must surface the degraded status
        # in steps_completed ("scene_detect_degraded") and append a diagnostic
        # to steps_failed rather than silently dropping the run.
        degraded_result = ToolResult(
            success=True,
            data={
                "status": "degraded",
                "diagnostics": ["segment 180-360 timed out"],
                "scenes": [
                    {"index": 0, "start_seconds": 0.0, "end_seconds": 1.0,
                     "duration_seconds": 1.0},
                    {"index": 1, "start_seconds": 1.0, "end_seconds": 2.0,
                     "duration_seconds": 1.0},
                ],
            },
        )
        with tempfile.TemporaryDirectory() as td:
            from lib import paths
            # Redirect PROJECTS_DIR to the tempdir so the workspace
            # resolution stays inside the throwaway tree. Then we need
            # to make output_dir land under that workspace — easiest is
            # to mirror the workspace layout into the tempdir.
            from lib.principal_registry import Principal
            principal = Principal(kind="user", principal_id="test_alice")
            workspace_root = Path(td) / "users" / principal.namespace_key / "references"
            workspace_root.mkdir(parents=True)
            output_dir = workspace_root / "out"
            output_dir.mkdir()

            input_path = Path(td) / "input.mp4"
            input_path.write_bytes(b"placeholder")

            with patch.object(paths, "PROJECTS_DIR", Path(td)), \
                 patch.object(paths, "REPO_ROOT", Path(td)), \
                 patch.object(VideoAnalyzer, "_get_duration", return_value=2.0), \
                 patch.object(SceneDetect, "execute", return_value=degraded_result):
                analyzer = VideoAnalyzer()
                result = analyzer.execute({
                    "source": str(input_path),
                    "userid": "test_alice",
                    "analysis_depth": "standard",
                    "max_keyframes": 5,
                })
            self.assertTrue(result.success)
            meta = result.data["_analysis_meta"]
            # Degraded path goes through steps_completed as
            # "scene_detect_degraded" (per video_analyzer.py:412) and is
            # recorded in steps_failed with the diagnostic message.
            self.assertIn("scene_detect_degraded", meta["steps_completed"])
            self.assertTrue(
                any("degraded" in s and "180-360 timed out" in s
                    for s in meta["steps_failed"])
            )
            self.assertEqual(meta["scene_count"], 2)


if __name__ == "__main__":
    unittest.main()
