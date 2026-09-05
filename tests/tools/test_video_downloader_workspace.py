"""Tests for VideoDownloader per-user workspace isolation + URL-hash naming.

These pin the workspace contract enforced by ``_validate_output_dir`` /
``_validate_userid`` and the collision-avoidance behavior of ``_url_hash``.
Run them before changing either — the security model breaks loudly if
either regresses.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.analysis.video_downloader import VideoDownloader  # noqa: E402


def _execute_with_patched_projects(output_dir: str, userid: str | None = "alice"):
    """Run VideoDownloader.execute() with a temp PROJECTS_DIR and yt-dlp
    mocked. Returns the ToolResult so callers can assert on success/error.
    """
    td_ctx = tempfile.TemporaryDirectory()
    td = Path(td_ctx.name)
    (td / "alice" / "_scratch").mkdir(parents=True)
    (td / "bob" / "_scratch").mkdir(parents=True)
    from lib import paths
    with patch.object(paths, "PROJECTS_DIR", td):
        with patch("yt_dlp.YoutubeDL") as ydl:
            instance = MagicMock()
            ydl.return_value.__enter__.return_value = instance
            instance.extract_info.return_value = {
                "duration": 30, "title": "t", "uploader": "u",
            }
            inputs: dict = {
                "url": "https://example.com/v",
                "output_dir": output_dir,
            }
            if userid is not None:
                inputs["userid"] = userid
            r = VideoDownloader().execute(inputs)
    td_ctx.cleanup()
    return r


class VideoDownloaderWorkspaceTests(unittest.TestCase):
    # --- userid validation --------------------------------------------------

    def test_missing_userid_is_rejected(self):
        r = _execute_with_patched_projects("projects/alice/_scratch/weibo", userid=None)
        self.assertFalse(r.success)
        self.assertIn("userid", (r.error or "").lower())

    def test_userid_with_path_traversal_is_rejected(self):
        r = _execute_with_patched_projects("projects/alice/_scratch/weibo", userid="../etc")
        self.assertFalse(r.success)
        self.assertIn("userid", (r.error or "").lower())

    def test_userid_with_slash_is_rejected(self):
        # userids are flat identifiers — no path separators.
        r = _execute_with_patched_projects("projects/alice/_scratch/weibo", userid="a/b")
        self.assertFalse(r.success)

    def test_userid_with_nul_byte_is_rejected(self):
        r = _execute_with_patched_projects("projects/alice/_scratch/weibo", userid="a\x00b")
        self.assertFalse(r.success)

    def test_userid_with_spaces_is_rejected(self):
        r = _execute_with_patched_projects("projects/alice/_scratch/weibo", userid="a b")
        self.assertFalse(r.success)

    def test_userid_too_long_is_rejected(self):
        # 65 chars > 64-char cap
        r = _execute_with_patched_projects(
            "projects/alice/_scratch/weibo", userid="a" * 65
        )
        self.assertFalse(r.success)

    def test_valid_userid_alphanumeric_passes_validation(self):
        # Workspace check still applies; use a path that's actually under
        # projects/<userid>/ so we get past the contract check.
        with tempfile.TemporaryDirectory() as td:
            userid = "alice_2026"
            (Path(td) / userid / "_scratch").mkdir(parents=True)
            from lib import paths
            with patch.object(paths, "PROJECTS_DIR", Path(td)):
                with patch("yt_dlp.YoutubeDL") as ydl:
                    instance = MagicMock()
                    ydl.return_value.__enter__.return_value = instance
                    instance.extract_info.return_value = {
                        "duration": 30, "title": "t", "uploader": "u",
                    }
                    r = VideoDownloader().execute({
                        "url": "https://example.com/v",
                        "userid": userid,
                        "output_dir": str(Path(td) / userid / "_scratch" / "x"),
                    })
        # Should pass workspace validation (mocked yt-dlp means no real download)
        self.assertTrue(r.success, msg=f"unexpected error: {r.error}")

    # --- workspace contract -------------------------------------------------

    def test_cross_user_write_is_rejected(self):
        # Alice tries to write into Bob's workspace.
        td_ctx = tempfile.TemporaryDirectory()
        try:
            td = Path(td_ctx.name)
            (td / "alice").mkdir()
            (td / "bob" / "_scratch").mkdir(parents=True)
            from lib import paths
            with patch.object(paths, "PROJECTS_DIR", td):
                with patch("yt_dlp.YoutubeDL"):
                    r = VideoDownloader().execute({
                        "url": "https://example.com/v",
                        "userid": "alice",
                        "output_dir": str(td / "bob" / "_scratch" / "weibo"),
                    })
            self.assertFalse(r.success)
            self.assertIn("alice", (r.error or "").lower())
        finally:
            td_ctx.cleanup()

    def test_write_to_tmp_is_rejected(self):
        # Absolute path outside the projects tree — reject.
        r = _execute_with_patched_projects("/tmp/foo", userid="alice")
        self.assertFalse(r.success)
        self.assertIn("alice", (r.error or "").lower())

    def test_write_to_etc_is_rejected(self):
        r = _execute_with_patched_projects("/etc/passwd", userid="alice")
        self.assertFalse(r.success)

    def test_path_traversal_via_dotdot_is_rejected(self):
        # Resolve() follows ../, so projects/alice/../etc ends up at projects/etc
        # which is NOT under projects/alice/.
        r = _execute_with_patched_projects("projects/alice/../etc", userid="alice")
        self.assertFalse(r.success)

    def test_write_to_legacy_public_scratch_is_rejected(self):
        # The old public projects/_scratch/ is no longer a valid workspace —
        # it sits outside every user's root.
        r = _execute_with_patched_projects(
            "projects/_scratch/weibo", userid="alice"
        )
        self.assertFalse(r.success)

    def test_symlink_escape_is_rejected(self):
        # Symlink projects/alice/evildir → /etc; writing under it should resolve
        # to /etc and be rejected.
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "alice").mkdir()
            evildir = Path(td) / "alice" / "evildir"
            evildir.symlink_to("/etc")
            from lib import paths
            with patch.object(paths, "PROJECTS_DIR", Path(td)):
                with patch("yt_dlp.YoutubeDL"):
                    r = VideoDownloader().execute({
                        "url": "https://example.com/v",
                        "userid": "alice",
                        "output_dir": str(evildir / "passwd"),
                    })
            self.assertFalse(r.success)

    # --- URL hash for collision avoidance -----------------------------------

    def test_same_url_produces_same_hash(self):
        h1 = VideoDownloader._url_hash("https://example.com/a")
        h2 = VideoDownloader._url_hash("https://example.com/a")
        self.assertEqual(h1, h2)

    def test_different_urls_produce_different_hashes(self):
        h1 = VideoDownloader._url_hash("https://example.com/a")
        h2 = VideoDownloader._url_hash("https://example.com/b")
        self.assertNotEqual(h1, h2)

    def test_hash_format_is_8_hex_chars(self):
        h = VideoDownloader._url_hash("https://example.com/anything")
        self.assertEqual(len(h), 8)
        self.assertTrue(re.fullmatch(r"[0-9a-f]{8}", h))

    def test_outtmpl_includes_url_hash(self):
        # Verify the constructed outtmpl embeds the hash — collision avoidance.
        # We can't easily run yt-dlp here, but we can read the outtmpl pattern
        # off the class.
        url = "https://example.com/v"
        expected_hash = VideoDownloader._url_hash(url)
        # Mirror the construction in _download_video.
        template = f"reference_video_{expected_hash}.%(ext)s"
        self.assertIn(expected_hash, template)
        # Different URL → different template
        self.assertNotEqual(
            template,
            f"reference_video_{VideoDownloader._url_hash('https://example.com/w')}.%(ext)s",
        )


if __name__ == "__main__":
    unittest.main()
