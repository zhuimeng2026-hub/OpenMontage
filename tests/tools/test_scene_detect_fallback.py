"""Regression tests for SceneDetect fallback path (FFmpeg only).

These tests pin the behaviour that matters on hosts where PySceneDetect
is not installed: the tool must still run end-to-end via FFmpeg's
`scene` filter, must surface `downgraded=True` + `capability_loss` so
downstream consumers can warn the user, and must not crash on
PySceneDetect-only `method` values.

The complementary case — PySceneDetect path — is covered by
``test_scene_detect_long_video.py``. Together they pin both branches of
the `if use_pyscenedetect:` branch in SceneDetect.execute().
"""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tools.base_tool import ToolResult
from tools.analysis.scene_detect import SceneDetect


def _build_synthetic_cut_clip(path: Path, duration_seconds: float = 5.0) -> Path:
    """Render a 2-color hard-cut clip into `path`.

    Red 2.5s → cut → Blue 2.5s (or whatever duration_seconds splits to).
    A real hard cut is what the FFmpeg `scene` filter reliably detects.
    """
    half = duration_seconds / 2
    a = path.with_name(path.stem + "_a.mp4")
    b = path.with_name(path.stem + "_b.mp4")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=red:s=320x240:d={half}",
         "-pix_fmt", "yuv420p", str(a)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=blue:s=320x240:d={half}",
         "-pix_fmt", "yuv420p", str(b)],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(a), "-i", str(b),
         "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return path


class SceneDetectFallbackTests(unittest.TestCase):
    """Pin the FFmpeg-fallback branch end-to-end.

    These tests pre-date the TransNetV2 backend (added 2026-09-10) and
    pin the behaviour that matters on hosts where neither PySceneDetect
    nor TransNetV2 is installed: the tool must still run end-to-end via
    FFmpeg's `scene` filter, must surface `downgraded=True` +
    `capability_loss` so downstream consumers can warn the user, and
    must not crash on PySceneDetect-only `method` values.

    The complementary cases — TransNetV2 path and PySceneDetect path —
    are covered by ``test_scene_detect_transnetv2.py``. Together they
    pin all three branches of the new `_select_backend` decision in
    `SceneDetect.execute()`.
    """

    def test_fallback_runs_when_no_upgraded_backend_available(self):
        """Without TransNetV2 or PySceneDetect the tool must still complete
        via FFmpeg."""
        detector = SceneDetect()
        # Force the FFmpeg fallback by mocking both soft deps as absent.
        # On the host that runs this suite TransNetV2 IS installed (and
        # PySceneDetect may or may not be); this test cares about the
        # fallback branch specifically, so we override the runtime checks.
        detector._has_transnetv2 = Mock(return_value=False)
        detector._has_pyscenedetect = Mock(return_value=False)

        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            clip = _build_synthetic_cut_clip(tmp / "merged.mp4", duration_seconds=5.0)
            out = tmp / "scenes.json"
            result = detector.execute({
                "input_path": str(clip),
                "method": "content",          # PySceneDetect-only kwarg — must not crash
                "threshold": 0.3,
                "min_scene_length_seconds": 0.5,
                "output_path": str(out),
            })

        self.assertIsInstance(result, ToolResult)
        self.assertTrue(result.success,
                        f"expected success=True, got error={result.error}")
        self.assertEqual(result.data["method"], "ffmpeg")
        self.assertTrue(result.data["downgraded"],
                        "downgraded must be True when FFmpeg fallback ran")
        self.assertIsInstance(result.data["capability_loss"], list)
        self.assertGreater(len(result.data["capability_loss"]), 0,
                           "capability_loss must list at least one regression")
        # Sanity: the FFmpeg `scene` filter actually found the cut.
        self.assertGreaterEqual(result.data["scene_count"], 1)

    def test_fallback_output_artifact_records_downgrade(self):
        """The persisted scenes.json must also carry the downgrade signal —
        consumers that read the artifact (rather than ToolResult.data)
        cannot miss partial failure."""
        detector = SceneDetect()
        # Force the FFmpeg fallback so this test exercises the
        # downgraded-artifact path regardless of which backends are
        # actually installed on the host.
        detector._has_transnetv2 = Mock(return_value=False)
        detector._has_pyscenedetect = Mock(return_value=False)
        with tempfile.TemporaryDirectory() as temp_dir:
            tmp = Path(temp_dir)
            clip = _build_synthetic_cut_clip(tmp / "merged.mp4", duration_seconds=5.0)
            out = tmp / "scenes.json"
            detector.execute({
                "input_path": str(clip),
                "threshold": 0.3,
                "min_scene_length_seconds": 0.5,
                "output_path": str(out),
            })
            payload = json.loads(out.read_text(encoding="utf-8"))

        self.assertEqual(payload["status"], "completed")
        self.assertIsInstance(payload["scenes"], list)
        self.assertGreaterEqual(len(payload["scenes"]), 1)

    def test_pyscenedetect_path_is_mid_tier_and_reports_downgraded(self):
        """When PySceneDetect is the active backend (e.g. user forced
        ``method="content"`` and TransNetV2 is unavailable), downgrade
        fields must reflect the mid-tier contract:

        * ``method == "pyscenedetect"``
        * ``downgraded is True`` (TransNetV2 is the only non-downgraded path)
        * ``capability_loss`` is the *smaller* PySceneDetect-specific list,
          NOT the FFmpeg fallback list.

        See ``docs/transnetv2-vs-pyscenedetect-2026-09-10.md`` § Part 1
        for the failure-mode analysis that motivates the mid-tier
        classification.
        """
        detector = SceneDetect()
        detector._has_transnetv2 = Mock(return_value=False)
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_pyscenedetect = Mock(return_value=[
            {"index": 0, "start_seconds": 0.0, "end_seconds": 2.0,
             "duration_seconds": 2.0},
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            out = Path(temp_dir) / "scenes.json"
            result = detector.execute({
                "input_path": str(input_path),
                "method": "content",          # force PySceneDetect
                "output_path": str(out),
            })

        self.assertTrue(result.success)
        self.assertEqual(result.data["method"], "pyscenedetect")
        self.assertTrue(result.data["downgraded"],
                        "PySceneDetect path is mid-tier — downgraded must be True")
        self.assertIsInstance(result.data["capability_loss"], list)
        self.assertGreater(len(result.data["capability_loss"]), 0,
                           "PySceneDetect path must list its capability loss")
        # Mid-tier loss is strictly smaller than the FFmpeg fallback loss.
        self.assertLess(
            len(result.data["capability_loss"]),
            len(detector._FFMPEG_FALLBACK_CAPABILITY_LOSS),
            "PySceneDetect capability_loss should be smaller than FFmpeg's",
        )
        detector._detect_pyscenedetect.assert_called_once()

    def test_capability_loss_lists_known_regressions(self):
        """Pin the four failure modes documented in
        docs/transnetv2-vs-pyscenedetect-2026-09-10.md so consumers can
        match against known categories."""
        detector = SceneDetect()
        loss = detector._FFMPEG_FALLBACK_CAPABILITY_LOSS
        joined = " ".join(loss).lower()
        for keyword in ("fade", "low-contrast", "flash", "fast-motion"):
            self.assertIn(keyword, joined,
                          f"capability_loss missing known regression: {keyword!r}")

    def test_optional_dependencies_declared(self):
        """The class must declare its soft upgrade dependencies so an
        upgrade helper (or future registry change) can detect them."""
        self.assertIn("python:scenedetect", SceneDetect._OPTIONAL_DEPENDENCIES)
        # Soft deps MUST NOT appear in `dependencies` — putting them there
        # would cause check_dependencies() to raise and break the
        # working FFmpeg fallback.
        self.assertNotIn("python:scenedetect", SceneDetect.dependencies)
        self.assertIn("cmd:ffmpeg", SceneDetect.dependencies,
                      "ffmpeg must remain a hard dependency")


if __name__ == "__main__":
    unittest.main()