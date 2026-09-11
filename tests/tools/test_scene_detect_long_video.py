import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.base_tool import ToolResult
from tools.analysis.scene_detect import SceneDetect
from tools.analysis.video_analyzer import VideoAnalyzer
from lib import paths as _paths  # noqa: E402


# ---------------------------------------------------------------------------
# Workspace fixture — mirrors the pattern in
# tests/regression/test_video_analyzer_keyframe_failure.py so VideoAnalyzer's
# principal/workspace-resolution path can run. Without this the test fails
# with 'no authenticated principal bound to this call' (workspace refactor
# 2026-09 added a mandatory userid + ProjectWorkspace.for_principal() hop).
# ---------------------------------------------------------------------------
@contextmanager
def _temp_workspace():
    from lib.principal_registry import Principal

    td_ctx = tempfile.TemporaryDirectory()
    td = Path(td_ctx.name)
    principal = Principal(kind="user", principal_id="alice_2026")
    workspace_root = td / "users" / principal.namespace_key / "references"
    workspace_root.mkdir(parents=True, exist_ok=True)
    source_path = workspace_root / "input.mp4"
    source_path.write_bytes(b"\x00" * 16)

    with patch.object(_paths, "PROJECTS_DIR", td), \
         patch.object(_paths, "REPO_ROOT", td):
        yield td, workspace_root, source_path
    td_ctx.cleanup()


def _stub_result(*, success: bool = True, data: dict | None = None, error: str | None = None):
    r = MagicMock()
    r.success = success
    r.data = data or {}
    r.error = error
    return r


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
        detector._has_transnetv2 = Mock(return_value=False)
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
            detector._has_transnetv2 = Mock(return_value=False)
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
        detector._has_transnetv2 = Mock(return_value=False)
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
        """When SceneDetect returns a `degraded` ToolResult (some
        segments failed but scenes still came back), VideoAnalyzer must
        surface that in the audit trail — `scene_detect_degraded` in
        steps_completed, the segment diagnostics in steps_failed —
        while keeping the brief's `structure_analysis` populated.

        Pre-fix failure mode: the test passed `_get_duration` + SceneDetect
        mocks but no `userid`, so VideoAnalyzer's principal resolution
        short-circuited with `success=False` before any pipeline step
        ran. With the workspace refactor, every VideoAnalyzer test now
        needs a temp workspace + `userid` + child-tool stubs (FrameSampler
        + AudioEnergy + Transcriber all touch disk via ffmpeg and would
        otherwise crash on the placeholder source bytes).
        """
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
        with _temp_workspace() as (_td, _ws, source):
            analyzer = VideoAnalyzer()
            analyzer._get_duration = lambda *a, **k: 2.0
            with (
                patch.object(SceneDetect, "execute", return_value=degraded),
                patch("tools.analysis.frame_sampler.FrameSampler") as fs_cls,
                patch("tools.analysis.audio_energy.AudioEnergy") as ae_cls,
                patch("tools.analysis.transcriber.Transcriber") as tr_cls,
            ):
                fs_cls.return_value.execute.return_value = _stub_result(
                    success=True, data={"frames": []},
                )
                ae_cls.return_value.execute.return_value = _stub_result(
                    success=True, data={"recommended_offset_seconds": 0.0},
                )
                tr_cls.return_value.execute.return_value = _stub_result(
                    success=True, data={"segments": [], "language": "en"},
                )
                # Short-circuit motion classification so we don't try to
                # open the placeholder source with cv2.
                analyzer._classify_scene_motion = lambda scenes: (
                    [{"motion_type": "unknown", "flow_variance": -1}] * len(scenes)
                )
                result = analyzer.execute({
                    "source": str(source),
                    "userid": "alice_2026",
                    "project_id": "references",
                    "analysis_depth": "standard",
                    "output_dir": "analysis_test",
                    "max_keyframes": 1,
                })

        self.assertTrue(result.success, msg=result.error)
        self.assertEqual(result.data["structure_analysis"]["total_scenes"], 2)
        meta = result.data["_analysis_meta"]
        self.assertIn("scene_detect_degraded", meta["steps_completed"])
        self.assertTrue(any(
            "segment 180-360 timed out" in item for item in meta["steps_failed"]
        ))


if __name__ == "__main__":
    unittest.main()
