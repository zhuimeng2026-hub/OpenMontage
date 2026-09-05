"""Regression test: video_analyzer must reuse pre-existing transcripts.

Bug: video_analyzer.execute() resolved transcripts only via YouTube caption
fetch or a fresh Whisper pass, ignoring any transcript already on disk in
the workspace. Local-file sources never picked up the transcript the agent
had pre-flighted (e.g. ``<workspace>/_audio_transcript.json``), wasting a
full faster-whisper pass and reporting ``has_transcript: false`` even when
a valid transcript was sitting next to the source video.

These tests cover the two reuse routes added by the fix:

- Explicit ``transcript_path`` from the caller (wins outright).
- Filesystem scan for known transcript filenames in ``output_dir`` and
  ``workspace.root`` (``<source_stem>_transcript.json``,
  ``_audio_transcript.json``, ``transcript.json``, ``transcript.srt``).

Run with::

    python -m pytest tests/regression/test_video_analyzer_transcript_reuse.py -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.analysis.video_analyzer import (  # noqa: E402
    VideoAnalyzer,
    _srt_timecode,
)
from lib import paths as _paths  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@contextmanager
def _temp_workspace(source_bytes: bytes = b"\x00" * 16):
    """Redirect lib.paths into a tempdir and pre-create a real local source file.

    Returns ``(workspace_root, source_path, output_dir)`` so tests can place
    cached transcript files at known locations. The analyzer's metadata
    ffprobe call is mocked so we don't depend on ffmpeg.
    """
    from lib.principal_registry import Principal

    td_ctx = tempfile.TemporaryDirectory()
    td = Path(td_ctx.name)
    principal = Principal(kind="user", principal_id="alice_2026")
    workspace_root = td / "users" / principal.namespace_key / "references"
    workspace_root.mkdir(parents=True, exist_ok=True)
    source_path = workspace_root / "source.mp4"
    source_path.write_bytes(source_bytes)

    with patch.object(_paths, "PROJECTS_DIR", td), \
         patch.object(_paths, "REPO_ROOT", td):
        yield td, workspace_root, source_path
    td_ctx.cleanup()


def _patched_analyzer(video_duration: float = 30.0) -> VideoAnalyzer:
    """Return a VideoAnalyzer with ffprobe stubbed (no real ffmpeg needed).

    Monkeys ``_get_duration`` on the instance rather than using
    ``patch.object`` so the fixture is just a plain factory — keeps the
    test bodies short and avoids context-manager indent churn.
    """
    inst = VideoAnalyzer()
    inst._get_duration = lambda *a, **k: video_duration
    return inst


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_local(analyzer: VideoAnalyzer, source: str, **extra) -> object:
    """Run the analyzer on a local file at ``transcript_only`` depth.

    ``transcript_only`` short-circuits after STEP 2, so child tools
    (Transcriber, SceneDetect, FrameSampler, AudioEnergy) are never
    invoked. We still patch them defensively in case future depth changes
    pull them in.
    """
    with patch("tools.analysis.transcriber.Transcriber") as tr_cls, \
         patch("tools.analysis.frame_sampler.FrameSampler") as fs_cls, \
         patch("tools.analysis.scene_detect.SceneDetect") as sd_cls, \
         patch("tools.analysis.audio_energy.AudioEnergy") as ae_cls:
        for mock_cls in (tr_cls, fs_cls, sd_cls, ae_cls):
            mock_cls.return_value.execute.return_value = type("R", (), {
                "success": True,
                "data": {
                    "scenes": [],
                    "frames": [],
                    "segments": [],
                    "recommended_offset_seconds": 0.0,
                },
                "error": None,
            })()
        return analyzer.execute({
            "source": source,
            "userid": "alice_2026",
            "project_id": "references",
            "analysis_depth": "transcript_only",
            **extra,
        })


def _write_segments_json(path: Path, *, language: str = "zh") -> None:
    """Write a faster-whisper-shaped transcript JSON at ``path``."""
    payload = {
        "segments": [
            {"start": 0.0, "end": 1.5, "text": "hello world"},
            {"start": 1.5, "end": 3.0, "text": "second segment"},
            {"start": 3.0, "end": 4.5, "text": "third segment"},
            {"start": 4.5, "end": 6.0, "text": "fourth segment"},
        ],
        "language": language,
        "text": "hello world second segment third segment fourth segment",
        "duration_seconds": 6.0,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _write_srt(path: Path) -> None:
    """Write a minimal valid SRT file at ``path``."""
    path.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nFirst cue line\n\n"
        "2\n00:00:02,000 --> 00:00:04,500\nSecond cue line\n\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TranscriptReuseTests(unittest.TestCase):
    # --- Explicit transcript_path wins --------------------------------------

    def test_explicit_transcript_path_wins(self):
        with _temp_workspace() as (_td, ws_root, source):
            cached = ws_root / "explicit.json"
            _write_segments_json(cached)
            analyzer = _patched_analyzer()
            r = _run_local(
                analyzer,
                str(source),
                transcript_path=str(cached),
            )
            self.assertTrue(r.success, msg=r.error)
            self.assertIn("transcript_external", r.data["_analysis_meta"]["steps_completed"])
            self.assertTrue(r.data["_analysis_meta"]["has_transcript"])
            narr = r.data["narration_transcript"]
            self.assertEqual(narr["language"], "zh")
            self.assertEqual(narr["word_count"], 8)
            self.assertEqual(len(narr["segments"]), 4)
            self.assertEqual(narr["segments"][0]["text"], "hello world")

    def test_explicit_path_overrides_workspace_cache(self):
        # Both an explicit path and a workspace-root cache exist; the
        # explicit path wins (priority 1).
        with _temp_workspace() as (_td, ws_root, source):
            workspace_cache = ws_root / "_audio_transcript.json"
            _write_segments_json(workspace_cache, language="en")
            workspace_cache_text = workspace_cache.read_text()

            explicit = ws_root / "explicit.json"
            _write_segments_json(explicit, language="zh")
            analyzer = _patched_analyzer()
            r = _run_local(
                analyzer,
                str(source),
                transcript_path=str(explicit),
            )
            self.assertTrue(r.success, msg=r.error)
            narr = r.data["narration_transcript"]
            self.assertEqual(
                narr["language"], "zh",
                msg="explicit path was ignored; "
                    f"got {narr['language']!r} but expected 'zh'",
            )
            # And the workspace cache was untouched.
            self.assertEqual(workspace_cache.read_text(), workspace_cache_text)

    # --- Output-dir filesystem scan ----------------------------------------

    def test_cached_transcript_in_output_dir_reused(self):
        # The transcriber writes <input_stem>_transcript.json. If the caller
        # produced one into output_dir, we should pick it up.
        with _temp_workspace() as (_td, ws_root, source):
            # The analyzer creates output_dir = workspace_root / analysis_<ts>
            # at execute() time. Pre-create one and seed it.
            output_dir = ws_root / "analysis_test"
            output_dir.mkdir()
            cached = output_dir / "source_transcript.json"
            _write_segments_json(cached)

            analyzer = _patched_analyzer()
            r = _run_local(
                analyzer,
                str(source),
                output_dir="analysis_test",
            )
            self.assertTrue(r.success, msg=r.error)
            self.assertIn("transcript_external", r.data["_analysis_meta"]["steps_completed"])
            self.assertTrue(r.data["_analysis_meta"]["has_transcript"])
            self.assertEqual(
                r.data["narration_transcript"]["language"], "zh"
            )

    def test_cached_transcript_in_workspace_root_reused(self):
        # The Weibo-reference bug case: transcript at workspace root, source
        # is a local file, output_dir does not contain a copy.
        with _temp_workspace() as (_td, ws_root, source):
            cached = ws_root / "_audio_transcript.json"
            _write_segments_json(cached)

            analyzer = _patched_analyzer()
            r = _run_local(
                analyzer,
                str(source),
                output_dir="analysis_test",
            )
            self.assertTrue(r.success, msg=r.error)
            self.assertIn("transcript_external", r.data["_analysis_meta"]["steps_completed"])
            self.assertTrue(r.data["_analysis_meta"]["has_transcript"])

    def test_transcript_json_in_workspace_root_reused(self):
        with _temp_workspace() as (_td, ws_root, source):
            cached = ws_root / "transcript.json"
            _write_segments_json(cached, language="en")
            analyzer = _patched_analyzer()
            r = _run_local(analyzer, str(source), output_dir="analysis_test")
            self.assertTrue(r.success, msg=r.error)
            self.assertTrue(r.data["_analysis_meta"]["has_transcript"])
            self.assertEqual(
                r.data["narration_transcript"]["language"], "en"
            )

    def test_srt_loaded(self):
        with _temp_workspace() as (_td, ws_root, source):
            srt = ws_root / "transcript.srt"
            _write_srt(srt)
            analyzer = _patched_analyzer()
            r = _run_local(analyzer, str(source), output_dir="analysis_test")
            self.assertTrue(r.success, msg=r.error)
            self.assertTrue(r.data["_analysis_meta"]["has_transcript"])
            segs = r.data["narration_transcript"]["segments"]
            self.assertEqual(len(segs), 2)
            self.assertEqual(segs[0]["text"], "First cue line")
            self.assertAlmostEqual(segs[0]["start"], 0.0)
            self.assertAlmostEqual(segs[0]["end"], 2.0)
            self.assertAlmostEqual(segs[1]["start"], 2.0)
            self.assertAlmostEqual(segs[1]["end"], 4.5)

    # --- Failure modes — must not blow up the analysis --------------------

    def test_malformed_cache_logged_in_steps_failed(self):
        # A garbage JSON at the expected path must not crash the analysis.
        # It should appear in steps_failed so the audit trail surfaces it,
        # and the brief should still be produced.
        with _temp_workspace() as (_td, ws_root, source):
            cached = ws_root / "_audio_transcript.json"
            cached.write_text("{this is not valid json", encoding="utf-8")
            analyzer = _patched_analyzer()
            r = _run_local(analyzer, str(source), output_dir="analysis_test")
            self.assertTrue(r.success, msg=r.error)
            self.assertFalse(r.data["_analysis_meta"]["has_transcript"])
            failed = r.data["_analysis_meta"]["steps_failed"]
            self.assertTrue(
                any("transcript_external" in s for s in failed),
                msg=f"expected transcript_external error in steps_failed; got {failed!r}",
            )

    def test_cache_missing_required_fields_is_ignored(self):
        # Segments list exists but lacks start/end/text on the first entries
        # → treated as a bad cache, falls through (no transcript in brief).
        with _temp_workspace() as (_td, ws_root, source):
            cached = ws_root / "transcript.json"
            cached.write_text(
                json.dumps({
                    "segments": [
                        {"foo": "bar"},  # missing start/end/text
                        {"foo": "baz"},
                    ],
                    "language": "en",
                }),
                encoding="utf-8",
            )
            analyzer = _patched_analyzer()
            r = _run_local(analyzer, str(source), output_dir="analysis_test")
            self.assertTrue(r.success, msg=r.error)
            self.assertFalse(r.data["_analysis_meta"]["has_transcript"])
            self.assertNotIn("narration_transcript", r.data)

    # --- No cache → brief has no transcript, but analysis still completes --

    def test_no_transcript_falls_through(self):
        with _temp_workspace() as (_td, ws_root, source):
            analyzer = _patched_analyzer()
            r = _run_local(analyzer, str(source), output_dir="analysis_test")
            self.assertTrue(r.success, msg=r.error)
            self.assertFalse(r.data["_analysis_meta"]["has_transcript"])
            self.assertNotIn("narration_transcript", r.data)
            self.assertNotIn(
                "transcript_external",
                r.data["_analysis_meta"]["steps_completed"],
            )

    # --- _srt_timecode helper ----------------------------------------------

    def test_srt_timecode_parses_comma_and_dot(self):
        self.assertAlmostEqual(_srt_timecode("00:00:01,500"), 1.5)
        self.assertAlmostEqual(_srt_timecode("00:01:00,000"), 60.0)
        self.assertAlmostEqual(_srt_timecode("01:00:00.250"), 3600.25)


if __name__ == "__main__":
    unittest.main()
