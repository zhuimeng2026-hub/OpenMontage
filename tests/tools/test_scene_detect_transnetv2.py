"""Regression tests for the TransNetV2 backend of SceneDetect.

These tests pin the new top-tier routing path added 2026-09-10 (when
`transnetv2_pytorch` became the highest-accuracy available backend on
this host). They cover:

* method routing: ``method="transnetv2"`` / ``"auto"`` / ``"content"``
* capability-loss contract: only TransNetV2 reports ``downgraded=False``
* explicit-method semantics: ``method="content"`` must NOT silently
  route to TransNetV2 even when it's available
* failure isolation: TransNetV2 runtime errors surface with the backend
  name in the error string

The complementary cases — PySceneDetect mid-tier and FFmpeg fallback —
are covered by ``test_scene_detect_fallback.py``. Together they pin
all three branches of the new ``_select_backend`` decision in
``SceneDetect.execute()``.

See ``docs/transnetv2-vs-pyscenedetect-2026-09-10.md`` § Part 1 for
the failure-mode analysis that motivates the three-tier design.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from tools.analysis.scene_detect import SceneDetect


class SceneDetectTransNetV2Tests(unittest.TestCase):
    """Pin the TransNetV2 (top-tier) backend of SceneDetect."""

    # ------------------------------------------------------------------
    # Backend selection routing
    # ------------------------------------------------------------------

    def test_method_transnetv2_dispatches_to_transnetv2_path(self):
        """``method="transnetv2"`` must route to the TransNetV2 path,
        and the result must report ``method == "transnetv2"`` with
        ``downgraded is False`` and an empty ``capability_loss``."""
        detector = SceneDetect()
        detector._has_transnetv2 = Mock(return_value=True)
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_transnetv2 = Mock(return_value=[
            {"index": 0, "start_seconds": 0.0, "end_seconds": 1.0,
             "duration_seconds": 1.0},
        ])
        # Mocked but must NOT be called; included so assert_not_called works.
        detector._detect_pyscenedetect = Mock(return_value=[])
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            out = Path(temp_dir) / "scenes.json"
            result = detector.execute({
                "input_path": str(input_path),
                "method": "transnetv2",
                "output_path": str(out),
            })

        self.assertTrue(result.success)
        self.assertEqual(result.data["method"], "transnetv2")
        self.assertFalse(
            result.data["downgraded"],
            "TransNetV2 path is the only non-downgraded backend",
        )
        self.assertEqual(result.data["capability_loss"], [])
        detector._detect_transnetv2.assert_called_once()
        detector._detect_pyscenedetect.assert_not_called()

    def test_method_auto_prefers_transnetv2_when_available(self):
        """``method="auto"`` (and unset method) must pick TransNetV2
        over PySceneDetect and FFmpeg fallback when the package is
        importable."""
        detector = SceneDetect()
        detector._has_transnetv2 = Mock(return_value=True)
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_transnetv2 = Mock(return_value=[
            {"index": 0, "start_seconds": 0.0, "end_seconds": 2.0,
             "duration_seconds": 2.0},
        ])
        detector._detect_pyscenedetect = Mock(return_value=[])
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            out = Path(temp_dir) / "scenes.json"
            for m in (None, "auto"):
                detector._detect_transnetv2.reset_mock()
                result = detector.execute({
                    "input_path": str(input_path),
                    "output_path": str(out),
                    **({"method": m} if m is not None else {}),
                })

                self.assertTrue(result.success)
                self.assertEqual(result.data["method"], "transnetv2")
                self.assertFalse(result.data["downgraded"])
                detector._detect_transnetv2.assert_called_once()
                detector._detect_pyscenedetect.assert_not_called()

    def test_explicit_pyscenedetect_method_does_not_use_transnetv2(self):
        """When the caller forces ``method="content"`` (PySceneDetect's
        value), the tool must NOT silently route to TransNetV2 — that
        would be a unilateral substitution, which is forbidden by
        ``CLAUDE.md`` Core Invariant #3 (no silent render-runtime
        swaps — same principle for backend selection)."""
        detector = SceneDetect()
        detector._has_transnetv2 = Mock(return_value=True)
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_pyscenedetect = Mock(return_value=[
            {"index": 0, "start_seconds": 0.0, "end_seconds": 2.0,
             "duration_seconds": 2.0},
        ])
        # Mocked but must NOT be called; included so assert_not_called works.
        detector._detect_transnetv2 = Mock(return_value=[])
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            out = Path(temp_dir) / "scenes.json"
            result = detector.execute({
                "input_path": str(input_path),
                "method": "content",
                "output_path": str(out),
            })

        self.assertTrue(result.success)
        self.assertEqual(result.data["method"], "pyscenedetect")
        self.assertTrue(
            result.data["downgraded"],
            "explicit PySceneDetect request is still mid-tier (downgraded)",
        )
        detector._detect_pyscenedetect.assert_called_once()
        detector._detect_transnetv2.assert_not_called()

    def test_transnetv2_missing_falls_through_to_pyscenedetect(self):
        """When ``method="transnetv2"`` is requested but the package is
        not importable, the tool must fall through to PySceneDetect
        rather than crashing."""
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
                "method": "transnetv2",
                "output_path": str(out),
            })

        self.assertTrue(result.success)
        self.assertEqual(result.data["method"], "pyscenedetect")
        self.assertTrue(result.data["downgraded"])
        detector._detect_pyscenedetect.assert_called_once()

    def test_unknown_method_emits_diagnostic_and_falls_back_to_best(self):
        """An unknown ``method`` value must NOT crash — instead it must
        emit a diagnostic and fall back to the best available backend."""
        detector = SceneDetect()
        detector._has_transnetv2 = Mock(return_value=True)
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_transnetv2 = Mock(return_value=[
            {"index": 0, "start_seconds": 0.0, "end_seconds": 1.0,
             "duration_seconds": 1.0},
        ])
        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            out = Path(temp_dir) / "scenes.json"
            result = detector.execute({
                "input_path": str(input_path),
                "method": "garbage-not-a-method",
                "output_path": str(out),
            })

        self.assertTrue(result.success)
        self.assertEqual(result.data["method"], "transnetv2")
        # The diagnostic surfaces in both the artifact and the result data.
        self.assertTrue(
            any("unknown method" in d for d in result.data["diagnostics"]),
            f"diagnostic should mention 'unknown method', got {result.data['diagnostics']!r}",
        )

    # ------------------------------------------------------------------
    # Failure surface: errors must name the backend so debugging is easy.
    # ------------------------------------------------------------------

    def test_transnetv2_runtime_error_includes_backend_name(self):
        """If TransNetV2 raises during inference, the failure must
        surface with the backend name in the error string — silent
        failures with no context are how support tickets get unreadable."""
        detector = SceneDetect()
        detector._has_transnetv2 = Mock(return_value=True)
        detector._has_pyscenedetect = Mock(return_value=True)
        detector._detect_transnetv2 = Mock(side_effect=RuntimeError("ffmpeg missing"))

        with tempfile.TemporaryDirectory() as temp_dir:
            input_path = Path(temp_dir) / "input.mp4"
            input_path.write_bytes(b"placeholder")
            out = Path(temp_dir) / "scenes.json"
            result = detector.execute({
                "input_path": str(input_path),
                "method": "transnetv2",
                "output_path": str(out),
            })

        self.assertFalse(result.success)
        self.assertEqual(result.data["status"], "failed")
        self.assertEqual(result.data["method"], "transnetv2")
        self.assertIn("transnetv2", (result.error or "").lower(),
                      f"error should name the failing backend, got {result.error!r}")
        self.assertIn("ffmpeg missing", (result.error or ""),
                      f"error should include the underlying message, got {result.error!r}")

    # ------------------------------------------------------------------
    # Soft-dep declaration pinning (parallel of fallback test)
    # ------------------------------------------------------------------

    def test_optional_dependencies_declares_both_backends(self):
        """The class must declare both soft upgrades so an upgrade
        helper (or future registry change) can detect them. Hard deps
        (``cmd:ffmpeg``) stay in ``dependencies``; soft deps stay in
        ``_OPTIONAL_DEPENDENCIES`` so ``check_dependencies()`` does not
        raise on hosts where either is absent."""
        soft = SceneDetect._OPTIONAL_DEPENDENCIES
        self.assertIn("python:transnetv2_pytorch", soft)
        self.assertIn("python:scenedetect", soft)
        # Soft deps MUST NOT appear in `dependencies` — putting them
        # there would cause check_dependencies() to raise and break the
        # working FFmpeg fallback.
        for entry in soft:
            self.assertNotIn(
                entry, SceneDetect.dependencies,
                f"soft dep {entry!r} must not appear in hard dependencies",
            )
        self.assertIn("cmd:ffmpeg", SceneDetect.dependencies,
                      "ffmpeg must remain a hard dependency")

    def test_capability_loss_for_pyscenedetect_lists_known_regressions(self):
        """Pin the known regressions for the PySceneDetect mid-tier path
        so consumers can match against documented categories."""
        loss = SceneDetect._PYSCENEDETECT_CAPABILITY_LOSS
        joined = " ".join(loss).lower()
        for keyword in ("fade", "transnetv2"):
            self.assertIn(keyword, joined,
                          f"PySceneDetect capability_loss missing known regression: {keyword!r}")

    def test_capability_loss_for_ffmpeg_lists_known_regressions(self):
        """The FFmpeg fallback capability_loss list still includes all
        four original failure modes from the pre-TransNetV2 contract."""
        loss = SceneDetect._FFMPEG_FALLBACK_CAPABILITY_LOSS
        joined = " ".join(loss).lower()
        for keyword in ("fade", "low-contrast", "flash", "fast-motion"):
            self.assertIn(keyword, joined,
                          f"FFmpeg capability_loss missing known regression: {keyword!r}")

    # ------------------------------------------------------------------
    # Host runnability: TransNetV2 must actually be installable + importable
    # ------------------------------------------------------------------

    def test_transnetv2_pytorch_is_importable_on_this_host(self):
        """The 2026-09-10 host runnability check (see
        docs/transnetv2-vs-pyscenedetect-2026-09-10.md § Part 4) records
        this host as having `transnetv2_pytorch` importable. Pin that
        contract: if a future refactor removes the package, this test
        fails loudly and we know we have a runnability regression."""
        # Detection via the same helper the routing uses.
        detector = SceneDetect()
        if not detector._has_transnetv2():
            self.fail(
                "transnetv2_pytorch is not importable on this host. "
                "Re-run: pip install -r requirements.txt"
            )


if __name__ == "__main__":
    unittest.main()