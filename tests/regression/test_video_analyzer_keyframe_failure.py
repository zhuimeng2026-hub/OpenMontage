"""Regression test: video_analyzer must surface FrameSampler failures.

Bug: video_analyzer.execute() at STEP 4 calls FrameSampler.execute() to
extract keyframes. When FrameSampler returns a ToolResult with
``success=False`` (e.g. permission / scope / path error inside FrameSampler),
the `if fs_result.success:` branch is skipped and the surrounding
``try / except Exception`` does not fire either — FrameSampler does not
raise, it returns. Net effect: ``steps_completed`` lacks the ``keyframes``
entry, ``steps_failed`` is empty, and ``brief["keyframes"]`` is ``[]``,
with no signal in the artifact that the sub-step failed.

This was the Weibo-reference failure mode (2026-09-05): the brief reported
``success=True`` with zero keyframes, downstream LLM-synthesis over frames
was skipped, and the loop never closed.

The fix adds an explicit ``else:`` to both STEP 4 branches (scene-guided
and no-scenes fallback) that appends ``fs_result.error`` to
``steps_failed``. These tests cover both branches.

Run with::

    .venv/bin/python -m pytest tests/regression/test_video_analyzer_keyframe_failure.py -v
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.analysis.video_analyzer import VideoAnalyzer  # noqa: E402
from lib import paths as _paths  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@contextmanager
def _temp_workspace():
    """Redirect lib.paths into a tempdir with a real local source file."""
    from lib.principal_registry import Principal

    td_ctx = tempfile.TemporaryDirectory()
    td = Path(td_ctx.name)
    principal = Principal(kind="user", principal_id="alice_2026")
    workspace_root = td / "users" / principal.namespace_key / "references"
    workspace_root.mkdir(parents=True, exist_ok=True)
    source_path = workspace_root / "source.mp4"
    source_path.write_bytes(b"\x00" * 16)

    with patch.object(_paths, "PROJECTS_DIR", td), \
         patch.object(_paths, "REPO_ROOT", td):
        yield td, workspace_root, source_path
    td_ctx.cleanup()


def _patched_analyzer(video_duration: float = 30.0) -> VideoAnalyzer:
    """Return a VideoAnalyzer with ffprobe stubbed (no real ffmpeg needed)."""
    inst = VideoAnalyzer()
    inst._get_duration = lambda *a, **k: video_duration
    return inst


def _fake_result(*, success: bool, data: dict | None = None, error: str | None = None):
    """Build a ToolResult-shaped object for the child tools to return."""
    r = MagicMock()
    r.success = success
    r.data = data or {}
    r.error = error
    return r


# Minimal scene that motion_classification can iterate over without crashing.
# We only need a real list of dicts — STEP 3b's optical-flow code runs on
# real video frames, so we sidestep it by setting scenes=[] for tests where
# we don't want to drive STEP 3b. For the scene-guided keyframe test, we DO
# need scenes (otherwise the no-scenes fallback runs), but we can patch
# _classify_scene_motion on the instance to short-circuit.
_SCENE = {
    "index": 0,
    "scene_index": 0,
    "start_seconds": 0.0,
    "end_seconds": 5.0,
}


def _run(
    analyzer: VideoAnalyzer,
    source: str,
    *,
    scenes: list[dict] | None,
    fs_result,
    depth: str = "standard",
) -> object:
    """Run the analyzer with controlled child-tool results.

    `scenes=None` means scene detection fails/empty (drives no-scenes
    fallback). `scenes=[...]` drives the scene-guided path. The patched
    `fs_result` is what FrameSampler.execute() returns — the test
    parameterises success vs. failure.
    """
    with patch("tools.analysis.scene_detect.SceneDetect") as sd_cls, \
         patch("tools.analysis.frame_sampler.FrameSampler") as fs_cls, \
         patch("tools.analysis.audio_energy.AudioEnergy") as ae_cls:
        sd_cls.return_value.execute.return_value = _fake_result(
            success=True,
            data={"scenes": scenes or [], "status": "completed", "diagnostics": []},
        )
        fs_cls.return_value.execute.return_value = fs_result
        ae_cls.return_value.execute.return_value = _fake_result(
            success=True,
            data={"recommended_offset_seconds": 0.0},
        )
        # Stub out motion classification so we don't try to open the
        # fake source.mp4 with cv2 (no real frames in a 16-byte file).
        with patch.object(
            analyzer, "_classify_scene_motion",
            return_value=[{"motion_type": "unknown", "flow_variance": -1}] * len(scenes or []),
        ):
            return analyzer.execute({
                "source": source,
                "userid": "alice_2026",
                "project_id": "references",
                "analysis_depth": depth,
                "output_dir": "analysis_test",
            })


# ---------------------------------------------------------------------------
# Tests — scene-guided path
# ---------------------------------------------------------------------------


class KeyframeFailureTests(unittest.TestCase):
    # --- Scene-guided path -----------------------------------------------

    def test_scene_guided_keyframe_failure_surfaced_in_steps_failed(self):
        """The bug: success=False on FrameSampler → brief has no signal.

        Fix: an `else:` branch appends `fs_result.error` to steps_failed
        so the audit trail records the failure.
        """
        with _temp_workspace() as (_td, _ws, source):
            analyzer = _patched_analyzer()
            r = _run(
                analyzer, str(source),
                scenes=[_SCENE],
                fs_result=_fake_result(
                    success=False,
                    error="output_dir 'analysis_test/keyframes' resolves outside workspace",
                ),
            )
            self.assertTrue(r.success, msg=r.error)
            meta = r.data["_analysis_meta"]
            self.assertEqual(
                r.data["keyframes"], [],
                msg="keyframes[] must be empty when FrameSampler failed",
            )
            self.assertNotIn("keyframes", meta["steps_completed"])
            failed = meta["steps_failed"]
            self.assertTrue(
                any("keyframes" in s and "outside workspace" in s for s in failed),
                msg=f"expected keyframes failure in steps_failed; got {failed!r}",
            )

    def test_scene_guided_keyframe_success_populates_brief(self):
        """Sanity check: when FrameSampler returns success=True with frames,
        the brief records the keyframes and steps_completed."""
        with _temp_workspace() as (_td, _ws, source):
            analyzer = _patched_analyzer()
            r = _run(
                analyzer, str(source),
                scenes=[_SCENE],
                fs_result=_fake_result(
                    success=True,
                    data={"frames": [
                        {"timestamp_seconds": 1.0, "path": "keyframes/f0.jpg"},
                        {"timestamp_seconds": 3.0, "path": "keyframes/f1.jpg"},
                    ]},
                ),
            )
            self.assertTrue(r.success, msg=r.error)
            self.assertIn("keyframes", r.data["_analysis_meta"]["steps_completed"])
            self.assertEqual(
                len(r.data["keyframes"]), 2,
                msg="expected 2 keyframes; got "
                    f"{len(r.data['keyframes'])}",
            )
            self.assertEqual(r.data["_analysis_meta"]["keyframe_count"], 2)
            self.assertFalse(
                any("keyframes" in s for s in r.data["_analysis_meta"]["steps_failed"]),
                msg="no keyframe failure expected on happy path",
            )

    def test_scene_guided_keyframe_failure_uses_default_message_when_error_blank(self):
        """If FrameSampler returns success=False with no error string, the
        steps_failed entry must still be populated (with a default message)
        so the artifact doesn't silently swallow the failure."""
        with _temp_workspace() as (_td, _ws, source):
            analyzer = _patched_analyzer()
            r = _run(
                analyzer, str(source),
                scenes=[_SCENE],
                fs_result=_fake_result(success=False, error=None),
            )
            self.assertTrue(r.success, msg=r.error)
            failed = r.data["_analysis_meta"]["steps_failed"]
            self.assertTrue(
                any("keyframes" in s and "success=False" in s for s in failed),
                msg=f"expected default 'success=False' message; got {failed!r}",
            )

    # --- No-scenes fallback path -----------------------------------------

    def test_no_scenes_keyframe_failure_surfaced(self):
        """The bug also lives in the no-scenes fallback (keyframes_uniform).

        Both branches must record failures — see bug doc §"Root cause"."""
        with _temp_workspace() as (_td, _ws, source):
            analyzer = _patched_analyzer()
            r = _run(
                analyzer, str(source),
                scenes=None,  # no scene detection → no-scenes fallback
                fs_result=_fake_result(
                    success=False,
                    error="ffmpeg returned non-zero exit 1",
                ),
            )
            self.assertTrue(r.success, msg=r.error)
            meta = r.data["_analysis_meta"]
            self.assertEqual(r.data["keyframes"], [])
            self.assertNotIn("keyframes_uniform", meta["steps_completed"])
            failed = meta["steps_failed"]
            self.assertTrue(
                any("keyframes_uniform" in s and "ffmpeg" in s for s in failed),
                msg=f"expected keyframes_uniform failure in steps_failed; got {failed!r}",
            )

    def test_no_scenes_keyframe_success_populates_brief(self):
        """Sanity check: no-scenes path with success=True records
        keyframes_uniform in steps_completed."""
        with _temp_workspace() as (_td, _ws, source):
            analyzer = _patched_analyzer()
            r = _run(
                analyzer, str(source),
                scenes=None,
                fs_result=_fake_result(
                    success=True,
                    data={"frames": [
                        {"timestamp_seconds": 5.0, "path": "keyframes/u0.jpg"},
                    ]},
                ),
            )
            self.assertTrue(r.success, msg=r.error)
            self.assertIn(
                "keyframes_uniform",
                r.data["_analysis_meta"]["steps_completed"],
            )
            self.assertEqual(len(r.data["keyframes"]), 1)
            self.assertEqual(
                r.data["keyframes"][0]["scene_index"], 0,
                msg="no-scenes fallback should pin scene_index=0",
            )


if __name__ == "__main__":
    unittest.main()
