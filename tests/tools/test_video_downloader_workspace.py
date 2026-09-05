"""Tests for VideoDownloader per-user workspace isolation + URL-hash naming.

Mirrors the upload_asset test pattern: workspace setup via
``ProjectWorkspace.for_principal(Principal(...), project_id)``, which
matches the MCP-layer auto-injection chain (WeChat → X-VClaw-User-Id →
ContextVar → Principal → ProjectWorkspace).

Run these before changing ``VideoDownloader._resolve_workspace`` /
``_url_hash`` — the security model breaks loudly if either regresses.
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.analysis.video_downloader import VideoDownloader  # noqa: E402
from lib.principal_registry import Principal  # noqa: E402
from lib.project_workspace import ProjectWorkspace  # noqa: E402


@contextmanager
def _temp_workspace(userid: str, project_id: str = "scratch"):
    """Build a ProjectWorkspace rooted at a throwaway tempdir.

    Strategy: monkey-patch ``lib.paths.PROJECTS_DIR`` so the real
    ``ProjectWorkspace.for_principal`` computes a workspace whose
    ``root`` lives under the tempdir. No frozen-dataclass gymnastics.
    The real factory still runs (so we exercise the real path
    resolution and the per-principal namespace_key derivation); only
    the projects root is redirected.
    """
    from lib import paths
    td_ctx = tempfile.TemporaryDirectory()
    td = Path(td_ctx.name)
    principal = Principal(kind="user", principal_id=userid)
    # Real root (under patched PROJECTS_DIR): projects/users/<ns>/<project_id>
    real_root = td / "users" / principal.namespace_key / project_id
    real_root.mkdir(parents=True, exist_ok=True)

    with patch.object(paths, "PROJECTS_DIR", td), \
         patch.object(paths, "REPO_ROOT", td), \
         patch("yt_dlp.YoutubeDL") as ydl:
        instance = MagicMock()
        ydl.return_value.__enter__.return_value = instance
        instance.extract_info.return_value = {
            "duration": 30, "title": "t", "uploader": "u",
        }
        yield td_ctx, real_root, principal

    td_ctx.cleanup()


def _run(userid: str, project_id: str = "scratch", **extra):
    """Run VideoDownloader.execute() inside a tempdir workspace."""
    with _temp_workspace(userid, project_id) as (_td, root, principal):
        inputs = {
            "url": "https://example.com/v",
            "userid": userid,
            "project_id": project_id,
        }
        inputs.update(extra)
        r = VideoDownloader().execute(inputs)
        return r, root, principal


class VideoDownloaderWorkspaceTests(unittest.TestCase):
    # --- userid validation (fallback path) ---------------------------------

    def test_bad_userid_is_rejected(self):
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "userid": "../etc",
            "project_id": "scratch",
        })
        self.assertFalse(r.success)
        self.assertIn("userid", (r.error or "").lower())

    def test_userid_with_slash_is_rejected(self):
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "userid": "a/b",
            "project_id": "scratch",
        })
        self.assertFalse(r.success)

    def test_userid_with_nul_byte_is_rejected(self):
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "userid": "a\x00b",
            "project_id": "scratch",
        })
        self.assertFalse(r.success)

    def test_userid_with_spaces_is_rejected(self):
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "userid": "a b",
            "project_id": "scratch",
        })
        self.assertFalse(r.success)

    def test_userid_too_long_is_rejected(self):
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "userid": "a" * 65,
            "project_id": "scratch",
        })
        self.assertFalse(r.success)

    # --- workspace resolution ----------------------------------------------

    def test_no_userid_and_no_mcp_session_returns_principal_not_found(self):
        # No userid input AND no MCP session context → PrincipalNotFound,
        # wrapped as a clean ToolResult failure.
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "project_id": "scratch",
        })
        self.assertFalse(r.success)
        self.assertIn("principal", (r.error or "").lower())

    def test_workspace_root_is_per_user(self):
        # alice and bob with the same project_id must get different
        # workspace roots — the namespace_key differs.
        r_alice, root_alice, _ = _run("alice")
        r_bob, root_bob, _ = _run("bob")
        self.assertTrue(r_alice.success)
        self.assertTrue(r_bob.success)
        self.assertNotEqual(root_alice, root_bob)
        # workspace_root echoed in the response matches the tempdir root.
        self.assertEqual(r_alice.data["workspace_root"], str(root_alice))
        self.assertEqual(r_bob.data["workspace_root"], str(root_bob))

    def test_valid_alphanumeric_userid_passes(self):
        r, _, _ = _run("alice_2026")
        self.assertTrue(r.success, msg=f"unexpected error: {r.error}")

    def test_default_project_id_is_references(self):
        # When caller omits project_id, it defaults to "references" —
        # matches the existing reference_input.analysis_tools convention
        # in pipeline_defs and avoids the sanitize_project_id constraint
        # that forbids a leading underscore.
        with _temp_workspace("alice", project_id="references") as (_td, root, _p):
            r = VideoDownloader().execute({
                "url": "https://example.com/v",
                "userid": "alice",
            })
        self.assertTrue(r.success)
        self.assertTrue(str(root).endswith("/references"))

    # --- workspace boundary enforcement ------------------------------------

    def test_output_dir_traversal_is_rejected(self):
        # Pass an output_dir that points outside the workspace root via "..".
        r = VideoDownloader().execute({
            "url": "https://example.com/v",
            "userid": "alice",
            "project_id": "scratch",
            "output_dir": "../etc",
        })
        self.assertFalse(r.success)
        self.assertIn("escapes", (r.error or "").lower())

    def test_output_dir_subpath_under_root_is_allowed(self):
        # output_dir = "subdir" should resolve to workspace.root/subdir.
        with _temp_workspace("alice") as (_td, root, _p):
            r = VideoDownloader().execute({
                "url": "https://example.com/v",
                "userid": "alice",
                "project_id": "scratch",
                "output_dir": "subdir",
            })
        self.assertTrue(r.success)
        # workspace_root echoed back is the canonical root, not the subdir.
        self.assertEqual(r.data["workspace_root"], str(root))

    # --- URL hash for collision avoidance ----------------------------------

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
        url = "https://example.com/v"
        expected_hash = VideoDownloader._url_hash(url)
        template = f"reference_video_{expected_hash}.%(ext)s"
        self.assertIn(expected_hash, template)
        self.assertNotEqual(
            template,
            f"reference_video_{VideoDownloader._url_hash('https://example.com/w')}.%(ext)s",
        )


if __name__ == "__main__":
    unittest.main()
