import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.base_tool import ToolResult
from tools.analysis.scene_detect import SceneDetect
from tools.analysis.video_analyzer import VideoAnalyzer


class SceneDetectLongVideoTests(unittest.TestCase):
    def test_timeout_scales_with_duration_and_resolution_and_is_capped(self):
        short_sd = SceneDetect._detection_timeout(30, 1280, 720)
        long_4k = SceneDetect._detection_timeout(600, 3840, 2160)
        huge_4k = SceneDetect._detection_timeout(10_000, 3840, 2160)

        self.assertGreater(long_4k, short_sd)
        self.assertEqual(huge_4k, SceneDetect._MAX_DETECTION_TIMEOUT_SECONDS)

    def test_scene_min_length_compares_with_last_kept_boundary(self):
        scenes = SceneDetect._build_scenes([0.0, 0.5, 1.0], 2.0, 1.0)
        self.assertEqual([s["start_seconds"] for s in scenes], [0.0, 1.0])

    def test_execute_resets_status_when_detector_instance_is_reused(self):
        detector = SceneDetect()
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_pyscenedetect = Mock(return_value=[
            {"index": 0, "start_seconds": 0.0, "end_seconds": 2.0, "duration_seconds": 2.0}
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            detector._detection_status = "degraded"
            detector._detection_diagnostics = ["old run"]
            result = detector.execute({"input_path": str(input_path)})
        self.assertTrue(result.success)
        self.assertEqual(result.data["status"], "completed")
        self.assertEqual(result.data["diagnostics"], [])

    def test_segmented_detection_applies_offsets_and_deduplicates_boundary(self):
        detector = SceneDetect()
        detector._probe_media_info = Mock(return_value=(360.0, 1920, 1080))

        def run_segment(cmd, *, timeout=None, **_kwargs):
            # The second chunk overlaps the first by one second.  Its local
            # timestamp 1.0 maps to the same global boundary at 180 seconds.
            start = float(cmd[cmd.index("-ss") + 1])
            output = "pts_time:10.000 pts_time:180.000" if start == 0 else "pts_time:1.000"
            return Mock(stderr=output, stdout="")

        detector.run_command = Mock(side_effect=run_segment)
        scenes = detector._detect_ffmpeg({
            "input_path": "long.mp4", "threshold": 0.3,
            "min_scene_length_seconds": 1.0,
        })

        self.assertEqual([s["start_seconds"] for s in scenes], [0.0, 10.0, 180.0])
        self.assertEqual(scenes[-1]["end_seconds"], 360.0)
        self.assertEqual(len({s["start_seconds"] for s in scenes}), len(scenes))
        self.assertTrue(all(s["duration_seconds"] >= 1.0 for s in scenes))

    def test_partial_segment_failure_is_degraded_but_keeps_scenes(self):
        detector = SceneDetect()
        detector._probe_media_info = Mock(return_value=(360.0, 1280, 720))
        calls = 0

        def run_segment(cmd, *, timeout=None, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise TimeoutError("segment timeout")
            return Mock(stderr="pts_time:10.000", stdout="")

        detector.run_command = Mock(side_effect=run_segment)
        scenes = detector._detect_ffmpeg({"input_path": "long.mp4", "min_scene_length_seconds": 1.0})
        self.assertGreater(len(scenes), 1)
        self.assertEqual(detector._detection_status, "degraded")
        self.assertTrue(detector._detection_diagnostics)

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            detector._has_pyscenedetect = Mock(return_value=False)
            def degraded_detection(_inputs):
                detector._detection_status = "degraded"
                detector._detection_diagnostics = ["segment timeout"]
                return scenes
            detector._detect_ffmpeg = Mock(side_effect=degraded_detection)
            result = detector.execute({"input_path": str(input_path)})
            self.assertTrue(result.success)
            self.assertEqual(result.data["status"], "degraded")
            self.assertIsNone(result.error)
            artifact = Path(result.data["output"]).read_text(encoding="utf-8")
            self.assertIn('"status": "degraded"', artifact)
            self.assertIn("segment timeout", artifact)

    def test_timeout_does_not_return_a_successful_single_scene(self):
        detector = SceneDetect()
        detector._has_pyscenedetect = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            output_path = Path(temp_dir) / "scenes.json"
            input_path.write_bytes(b"placeholder")

            detector._detect_ffmpeg = Mock(side_effect=TimeoutError("ffmpeg timed out"))
            result = detector.execute({
                "input_path": str(input_path), "output_path": str(output_path),
            })

        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "failed")
        self.assertIn("timed out", result.error)
        self.assertFalse(output_path.exists())

    def test_video_analyzer_retains_and_reports_degraded_scenes(self):
        degraded = ToolResult(
            success=True,
            data={
                "status": "degraded",
                "diagnostics": ["segment 180-360 timed out"],
                "scenes": [
                    {"index": 0, "start_seconds": 0.0, "end_seconds": 1.0, "duration_seconds": 1.0},
                    {"index": 1, "start_seconds": 1.0, "end_seconds": 2.0, "duration_seconds": 1.0},
                ],
            },
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            output_dir = Path(temp_dir) / "analysis"
            with (
                patch.object(VideoAnalyzer, "_get_duration", return_value=2.0),
                patch.object(SceneDetect, "execute", return_value=degraded),
            ):
                result = VideoAnalyzer().execute({
                    "source": str(input_path),
                    "analysis_depth": "standard",
                    "output_dir": str(output_dir),
                    "max_keyframes": 1,
                })

        self.assertTrue(result.success)
        self.assertEqual(result.data["structure_analysis"]["total_scenes"], 2)
        meta = result.data["_analysis_meta"]
        self.assertIn("scene_detect_degraded", meta["steps_completed"])
        self.assertTrue(any(
            "segment 180-360 timed out" in item for item in meta["steps_failed"]
        ))


if __name__ == "__main__":
    unittest.main()
