"""Tests for VideoAnalyzer's per-user workspace resolution + userid propagation.

Mirrors ``test_video_downloader_workspace.py``: real
``ProjectWorkspace.for_principal`` runs, redirected via
``patch.object(paths, 'PROJECTS_DIR', td)`` into a throwaway tempdir. The
four child tools (VideoDownloader, Transcriber, FrameSampler, SceneDetect)
are mocked so we can assert that userid/project_id/relative-output-dir
propagate correctly through the cascade.

Run before changing ``video_analyzer.execute()`` — the cascade wiring
breaks loudly if userid propagation regresses.
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
from tools.base_tool import ToolResult  # noqa: E402
from lib import paths as _paths  # noqa: E402


@contextmanager
def _temp_projects_root():
    """Redirect lib.paths.PROJECTS_DIR + REPO_ROOT to a tempdir and patch
    yt_dlp + all child tools so video_analyzer runs without network."""
    from lib.principal_registry import Principal

    td_ctx = tempfile.TemporaryDirectory()
    td = Path(td_ctx.name)
    principal = Principal(kind="user", principal_id="alice_2026")
    # Pre-create the workspace dir so the test can verify file paths land here.
    workspace_root = td / "users" / principal.namespace_key / "references"
    workspace_root.mkdir(parents=True, exist_ok=True)

    with patch.object(_paths, "PROJECTS_DIR", td), \
         patch.object(_paths, "REPO_ROOT", td), \
         patch("yt_dlp.YoutubeDL") as ydl, \
         patch("tools.analysis.transcriber.Transcriber") as tr_cls, \
         patch("tools.analysis.frame_sampler.FrameSampler") as fs_cls, \
         patch("tools.analysis.scene_detect.SceneDetect") as sd_cls:
        ydl_inst = MagicMock()
        ydl.return_value.__enter__.return_value = ydl_inst
        ydl_inst.extract_info.return_value = {
            "duration": 30, "title": "t", "uploader": "u",
        }
        # Each child tool's .execute() returns a stub ToolResult.
        for mock_cls in (tr_cls, fs_cls, sd_cls):
            mock_cls.return_value.execute.return_value = ToolResult(
                success=True, data={"scenes": [], "frames": [], "segments": []}
            )
        yield td, workspace_root, principal, {
            "video_downloader": ydl,
            "transcriber": tr_cls,
            "frame_sampler": fs_cls,
            "scene_detect": sd_cls,
        }
    td_ctx.cleanup()


class VideoAnalyzerWorkspaceTests(unittest.TestCase):
    # --- workspace resolution ----------------------------------------------

    def test_no_userid_and_no_mcp_session_returns_principal_not_found(self):
        # No userid input + no MCP context → for_current_principal raises.
        with _temp_projects_root():
            r = VideoAnalyzer().execute({
                "source": "https://example.com/v",
            })
        self.assertFalse(r.success)
        self.assertIn("principal", (r.error or "").lower())

    def test_explicit_userid_uses_for_principal_fallback(self):
        with _temp_projects_root():
            r = VideoAnalyzer().execute({
                "source": "https://example.com/v",
                "userid": "alice_2026",
                "analysis_depth": "metadata_only",  # skip deep cascade
            })
        self.assertTrue(r.success, msg=f"unexpected error: {r.error}")

    def test_default_project_id_is_references(self):
        with _temp_projects_root() as (td, ws_root, _p, mocks):
            # We mock VideoDownloader's execute via yt_dlp patch — but
            # VideoDownloader is a separate class instantiated by
            # video_analyzer. Patch the class instead.
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True, data={"metadata": {}, "video_path": None, "audio_path": None}
                )
                r = VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                    "analysis_depth": "metadata_only",
                })
                # VideoDownloader.execute() is called with one positional arg (the inputs dict).
                inputs = vd_cls.return_value.execute.call_args.args[0]
                self.assertEqual(inputs["project_id"], "references")
        self.assertTrue(r.success)

    def test_custom_project_id_respected(self):
        with _temp_projects_root() as (_td, _ws, _p, _mocks):
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True, data={"metadata": {}, "video_path": None, "audio_path": None}
                )
                r = VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                    "project_id": "myproject",
                    "analysis_depth": "metadata_only",
                })
                inputs = vd_cls.return_value.execute.call_args.args[0]
                self.assertEqual(inputs["project_id"], "myproject")
        self.assertTrue(r.success)

    # --- output_dir defaulting ---------------------------------------------

    def test_default_output_dir_is_under_workspace(self):
        with _temp_projects_root() as (_td, ws_root, _p, _mocks):
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True, data={"metadata": {}, "video_path": None, "audio_path": None}
                )
                r = VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                })
                inputs = vd_cls.return_value.execute.call_args.args[0]
                output_dir = inputs["output_dir"]
                self.assertTrue(output_dir.startswith("analysis_"),
                                msg=f"unexpected output_dir: {output_dir!r}")
                self.assertFalse(Path(output_dir).is_absolute(),
                                 msg=f"output_dir must be relative: {output_dir!r}")
        self.assertTrue(r.success)

    def test_explicit_output_dir_escaping_workspace_is_rejected(self):
        with _temp_projects_root():
            r = VideoAnalyzer().execute({
                "source": "https://example.com/v",
                "userid": "alice_2026",
                "output_dir": "../etc",
            })
        self.assertFalse(r.success)
        self.assertIn("escapes", (r.error or "").lower())

    def test_explicit_output_dir_inside_workspace_is_accepted(self):
        with _temp_projects_root() as (_td, ws_root, _p, _mocks):
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True, data={"metadata": {}, "video_path": None, "audio_path": None}
                )
                r = VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                    "output_dir": "custom_subdir",
                })
                inputs = vd_cls.return_value.execute.call_args.args[0]
                self.assertEqual(inputs["output_dir"], "custom_subdir")
        self.assertTrue(r.success)

    # --- userid propagation to child tools ---------------------------------

    def test_video_downloader_receives_userid_and_project_id(self):
        with _temp_projects_root() as (_td, _ws, _p, _mocks):
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True, data={"metadata": {}, "video_path": None, "audio_path": None}
                )
                VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                    "project_id": "myproject",
                    "analysis_depth": "metadata_only",
                })
                inputs = vd_cls.return_value.execute.call_args.args[0]
                self.assertEqual(inputs["userid"], "alice_2026")
                self.assertEqual(inputs["project_id"], "myproject")
                # output_dir must be RELATIVE — absolute would be rejected
                # by the child's workspace.resolve() with "relative path
                # must not be absolute".
                self.assertFalse(Path(inputs["output_dir"]).is_absolute())

    def test_transcriber_receives_userid_and_project_id(self):
        # Drive the transcriber path: need a successful download + scene
        # detect first, then we hit the Whisper fallback at line ~340.
        with _temp_projects_root() as (_td, ws_root, _p, mocks):
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True,
                    data={
                        "metadata": {"duration": 30},
                        "video_path": str(ws_root / "fake.mp4"),
                        "audio_path": str(ws_root / "fake.wav"),
                    },
                )
                VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                    "project_id": "myproject",
                })
                tr_inputs = mocks["transcriber"].return_value.execute.call_args.args[0]
                self.assertEqual(tr_inputs["userid"], "alice_2026")
                self.assertEqual(tr_inputs["project_id"], "myproject")
                self.assertFalse(Path(tr_inputs["output_dir"]).is_absolute())

    def test_frame_sampler_receives_userid_and_project_id(self):
        with _temp_projects_root() as (_td, ws_root, _p, mocks):
            with patch("tools.analysis.video_downloader.VideoDownloader") as vd_cls:
                vd_cls.return_value.execute.return_value = ToolResult(
                    success=True,
                    data={
                        "metadata": {"duration": 30},
                        "video_path": str(ws_root / "fake.mp4"),
                        "audio_path": str(ws_root / "fake.wav"),
                    },
                )
                # SceneDetect must return scenes so FrameSampler is invoked.
                mocks["scene_detect"].return_value.execute.return_value = ToolResult(
                    success=True,
                    data={"scenes": [{"index": 0, "start_seconds": 0.0,
                                      "end_seconds": 5.0}],
                          "status": "completed", "diagnostics": []},
                )
                VideoAnalyzer().execute({
                    "source": "https://example.com/v",
                    "userid": "alice_2026",
                    "project_id": "myproject",
                })
                fs_inputs = mocks["frame_sampler"].return_value.execute.call_args.args[0]
                self.assertEqual(fs_inputs["userid"], "alice_2026")
                self.assertEqual(fs_inputs["project_id"], "myproject")
                # Keyframe output_dir is relative — e.g. "analysis_<ts>/keyframes".
                self.assertTrue(fs_inputs["output_dir"].endswith("keyframes"))
                self.assertFalse(Path(fs_inputs["output_dir"]).is_absolute())


if __name__ == "__main__":
    unittest.main()
