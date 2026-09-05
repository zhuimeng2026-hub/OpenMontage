"""Tests for SceneDetect._escape_lavfi_movie_path filtergraph escaping.

FFmpeg's lavfi ``movie='...'`` filter applies filtergraph parsing to its
input, which means a user-supplied path that contains ``\\``, ``:``, ``,``,
``[``, ``]``, or ``;`` would otherwise corrupt the filter expression or
allow argument smuggling. SceneDetect escapes the path before passing it
through. Single quotes are rejected outright (the only way to terminate a
filtergraph string, so we fail closed rather than guess at escaping).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.analysis.scene_detect import SceneDetect  # noqa: E402


class SceneDetectEscapingTests(unittest.TestCase):
    """SceneDetectEscapingTests"""

    def test_lavfi_movie_path_escapes_filtergraph_metacharacters(self):
        raw = "/tmp/clip-name,with[bad];chars:01.mov"
        escaped = SceneDetect._escape_lavfi_movie_path(raw)
        # Every filtergraph metacharacter in the basename must be backslash-escaped.
        self.assertIn("\\,", escaped)
        self.assertIn("\\[", escaped)
        self.assertIn("\\]", escaped)
        self.assertIn("\\;", escaped)
        self.assertIn("\\:", escaped)
        # Each escape sequence must precede the original character (no escaping
        # accidentally shifted or dropped a metachar).
        for meta in (",", "[", "]", ";", ":"):
            self.assertIn(f"\\{meta}", escaped)

    def test_lavfi_movie_path_rejects_single_quote_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "single quotes"):
            SceneDetect._escape_lavfi_movie_path("/tmp/clip'name.mov")


if __name__ == "__main__":
    unittest.main()
