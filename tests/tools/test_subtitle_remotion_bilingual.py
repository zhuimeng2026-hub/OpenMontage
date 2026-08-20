"""Tests for subtitle_gen(format='remotion_bilingual_captions').

Validates the JSON contract that the BilingualCaptionOverlay Remotion
composition consumes. The shape must match
remotion-composer/src/components/BilingualCaptionOverlay.tsx exactly:

    {
      "format": "remotion_bilingual_captions",
      "primaryWords":   [{word: str, startMs: int, endMs: int}, ...],
      "secondaryWords": [{word: str, startMs: int, endMs: int}, ...],
    }

These tests verify the JSON in isolation — no Remotion rendering
involved, so they run in <0.1s.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from tools.subtitle.subtitle_gen import SubtitleGen  # noqa: E402


def _word_segments() -> list[dict]:
    """Two English segments with word-level timestamps."""
    return [
        {
            "id": 0, "start": 0.0, "end": 1.5, "text": "Hello world",
            "words": [
                {"word": "Hello", "start": 0.0, "end": 0.7},
                {"word": "world", "start": 0.7, "end": 1.5},
            ],
        },
        {
            "id": 1, "start": 1.5, "end": 3.0, "text": "Goodbye",
            "words": [
                {"word": "Goodbye", "start": 1.5, "end": 3.0},
            ],
        },
    ]


def _sentence_only_segments() -> list[dict]:
    """Sentence-level (no words[]) — typical FunASR paraformer-zh output."""
    return [
        {"id": 0, "start": 0.0, "end": 1.5, "text": "你好 世界"},
        {"id": 1, "start": 1.5, "end": 3.0, "text": "再见"},
    ]


def _render(payload_inputs: dict) -> tuple[dict, "ToolResult"]:
    """Render the format into a temp file and return (parsed JSON, ToolResult)
    while the file is still readable."""
    ctx = tempfile.TemporaryDirectory()
    tmpdir = ctx.__enter__()
    try:
        out = Path(tmpdir) / "out.remotion_bilingual.json"
        inputs = dict(payload_inputs)
        inputs["output_path"] = str(out)
        r = SubtitleGen().execute(inputs)
        parsed = json.loads(out.read_text(encoding="utf-8"))
    finally:
        ctx.__exit__(None, None, None)
    return parsed, r


# ---------------------------------------------------------------------------
# Output shape
# ---------------------------------------------------------------------------

def test_output_format_field_is_remotion_bilingual_captions():
    parsed, r = _render({
        "segments": _word_segments(),
        "target_segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    assert r.success, r.error
    assert parsed["format"] == "remotion_bilingual_captions"


def test_output_has_primary_and_secondary_arrays():
    parsed, r = _render({
        "segments": _word_segments(),
        "target_segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    assert isinstance(parsed["primaryWords"], list)
    assert isinstance(parsed["secondaryWords"], list)


def test_each_word_has_required_keys_with_correct_types():
    parsed, r = _render({
        "segments": _word_segments(),
        "target_segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    for w in parsed["primaryWords"] + parsed["secondaryWords"]:
        assert set(w.keys()) == {"word", "startMs", "endMs"}
        assert isinstance(w["word"], str)
        assert isinstance(w["startMs"], int)
        assert isinstance(w["endMs"], int)


# ---------------------------------------------------------------------------
# Time conversion (seconds → ms)
# ---------------------------------------------------------------------------

def test_seconds_to_ms_conversion_is_exact():
    """0.7s → 700ms, 1.5s → 1500ms, 3.0s → 3000ms (rounded to int)."""
    parsed, r = _render({
        "segments": _word_segments(),
        "target_segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    by_word = {w["word"]: w for w in parsed["primaryWords"]}
    assert by_word["Hello"]["startMs"] == 0
    assert by_word["Hello"]["endMs"] == 700
    assert by_word["world"]["startMs"] == 700
    assert by_word["world"]["endMs"] == 1500
    assert by_word["Goodbye"]["startMs"] == 1500
    assert by_word["Goodbye"]["endMs"] == 3000


def test_time_rounding_is_integer_no_floats():
    """Remotion's useCurrentFrame works on ints — float ms values would
    cause off-by-one frame drift over a 30s video."""
    parsed, r = _render({
        "segments": _word_segments(),
        "target_segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    for w in parsed["primaryWords"] + parsed["secondaryWords"]:
        assert isinstance(w["startMs"], int)
        assert isinstance(w["endMs"], int)
        assert w["endMs"] >= w["startMs"]


# ---------------------------------------------------------------------------
# Word-level preservation (1:1 with source segments)
# ---------------------------------------------------------------------------

def test_primary_word_count_matches_input_segments():
    primary = _word_segments()
    parsed, r = _render({
        "segments": primary,
        "target_segments": primary,
        "format": "remotion_bilingual_captions",
    })
    expected = sum(len(s.get("words", [])) for s in primary)
    assert len(parsed["primaryWords"]) == expected


def test_secondary_word_count_matches_target_segments():
    primary = _word_segments()
    secondary = [
        {**s, "text": s["text"], "words": s["words"]} for s in primary
    ]  # same shape; bilingual test would have different text per word
    parsed, r = _render({
        "segments": primary,
        "target_segments": secondary,
        "format": "remotion_bilingual_captions",
    })
    expected = sum(len(s.get("words", [])) for s in secondary)
    assert len(parsed["secondaryWords"]) == expected


def test_primary_words_preserve_timestamps_verbatim():
    primary = _word_segments()
    parsed, r = _render({
        "segments": primary,
        "target_segments": primary,
        "format": "remotion_bilingual_captions",
    })
    flat = [w for seg in primary for w in seg["words"]]
    for src, out in zip(flat, parsed["primaryWords"]):
        assert out["startMs"] == round(src["start"] * 1000)
        assert out["endMs"] == round(src["end"] * 1000)


# ---------------------------------------------------------------------------
# Sentence-only fallback
# ---------------------------------------------------------------------------

def test_sentence_only_segments_emit_one_wordcaption_per_segment():
    parsed, r = _render({
        "segments": _sentence_only_segments(),
        "target_segments": _sentence_only_segments(),
        "format": "remotion_bilingual_captions",
    })
    # 2 segments → 2 word captions per language
    assert len(parsed["primaryWords"]) == 2
    assert len(parsed["secondaryWords"]) == 2
    assert parsed["primaryWords"][0]["word"] == "你好 世界"
    assert parsed["primaryWords"][1]["word"] == "再见"


def test_sentence_only_timestamps_span_segment():
    parsed, r = _render({
        "segments": _sentence_only_segments(),
        "target_segments": _sentence_only_segments(),
        "format": "remotion_bilingual_captions",
    })
    assert parsed["primaryWords"][0]["startMs"] == 0
    assert parsed["primaryWords"][0]["endMs"] == 1500
    assert parsed["primaryWords"][1]["startMs"] == 1500
    assert parsed["primaryWords"][1]["endMs"] == 3000


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_requires_target_segments():
    sg = SubtitleGen()
    r = sg.execute({
        "segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    assert not r.success
    assert "requires" in (r.error or "").lower()


def test_rejects_length_mismatch():
    sg = SubtitleGen()
    r = sg.execute({
        "segments": _word_segments(),  # 2 segments
        "target_segments": _word_segments()[:1],  # 1 segment
        "format": "remotion_bilingual_captions",
    })
    assert not r.success
    assert "target_segments length" in (r.error or "")


# ---------------------------------------------------------------------------
# Schema accepts the new value
# ---------------------------------------------------------------------------

def test_input_schema_enum_includes_remotion_bilingual_captions():
    info = SubtitleGen().get_info()
    enum = info["input_schema"]["properties"]["format"]["enum"]
    assert "remotion_bilingual_captions" in enum


def test_input_schema_default_is_unchanged():
    """Adding a new format must NOT change the default — downstream callers
    rely on `format` being optional."""
    info = SubtitleGen().get_info()
    assert info["input_schema"]["properties"]["format"]["default"] == "srt"


# ---------------------------------------------------------------------------
# Component contract: shape matches BilingualCaptionOverlay's WordCaption
# ---------------------------------------------------------------------------

def test_shape_matches_remotion_wordcaption_interface():
    """Sanity check that the JSON we emit is structurally identical to what
    BilingualCaptionOverlay.tsx declares as `WordCaption`:

        export interface WordCaption {
          word: string;
          startMs: number;
          endMs: number;
        }

    If either field is renamed, both sides must change together — this
    test fails first as the canary.
    """
    parsed, r = _render({
        "segments": _word_segments(),
        "target_segments": _word_segments(),
        "format": "remotion_bilingual_captions",
    })
    assert parsed["primaryWords"], "no words emitted"
    sample = parsed["primaryWords"][0]
    # EXACT field set — additions require updating both files
    assert set(sample.keys()) == {"word", "startMs", "endMs"}