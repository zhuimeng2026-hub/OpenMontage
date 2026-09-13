"""Tests for subtitle_gen dual_ass bilingual rendering.

Validates that the dual-language ASS path emits a syntactically valid file with
both Primary and Secondary styles, uses the `{\\rSecondary}` style-switch tag
to put the secondary line below the primary, and emits one Dialogue event per
segment pair.

These tests do NOT exercise the upstream NLLB translator — they feed hand-
crafted translated segments straight into `subtitle_gen` so they run with
only the standard library + the project's pure-Python renderer.
"""

from __future__ import annotations

import re
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.subtitle.subtitle_gen import SubtitleGen  # noqa: E402


def _sample_segments() -> list[dict]:
    """Bilingual segments — same shape transcriber + nllb_translator produce."""
    return [
        {
            "id": 0,
            "start": 0.0,
            "end": 2.5,
            "text": "新鲜烘焙，源自云南。",
            "words": [
                {"word": "新鲜", "start": 0.0, "end": 0.6},
                {"word": "烘焙", "start": 0.6, "end": 1.2},
                {"word": "，", "start": 1.2, "end": 1.3},
                {"word": "源自", "start": 1.3, "end": 1.9},
                {"word": "云南", "start": 1.9, "end": 2.4},
                {"word": "。", "start": 2.4, "end": 2.5},
            ],
        },
        {
            "id": 1,
            "start": 2.6,
            "end": 5.4,
            "text": "每一颗都是手工挑选。",
            "words": [
                {"word": "每一颗", "start": 2.6, "end": 3.4},
                {"word": "都是", "start": 3.4, "end": 3.9},
                {"word": "手工", "start": 3.9, "end": 4.5},
                {"word": "挑选", "start": 4.5, "end": 5.2},
                {"word": "。", "start": 5.2, "end": 5.4},
            ],
        },
    ]


def _target_segments() -> list[dict]:
    """English counterparts. Same segment count, same start/end timestamps."""
    return [
        {
            "id": 0,
            "start": 0.0,
            "end": 2.5,
            "text": "Freshly baked, from Yunnan.",
        },
        {
            "id": 1,
            "start": 2.6,
            "end": 5.4,
            "text": "Every bean is hand-picked.",
        },
    ]


def _render_dual_ass(**overrides) -> tuple[str, "ToolResult"]:
    """Render dual_ass to a temp file and return (content, ToolResult)
    inside the temp directory's lifetime so the caller can read it back."""
    sg = SubtitleGen()
    inputs = {
        "segments": _sample_segments(),
        "target_segments": _target_segments(),
        "format": "dual_ass",
    }
    inputs.update(overrides)
    # Default output_path is set so we can read it back. Using a context
    # manager: caller can rely on the returned `content` being valid even
    # after we exit the `with` block, because we copy the bytes into
    # memory.
    tmp_ctx = tempfile.TemporaryDirectory()
    tmpdir = tmp_ctx.__enter__()
    inputs["output_path"] = str(Path(tmpdir) / "test.ass")
    r = sg.execute(inputs)
    content = Path(inputs["output_path"]).read_text(encoding="utf-8")
    tmp_ctx.__exit__(None, None, None)
    return content, r


def test_dual_ass_emits_required_sections():
    content, r = _render_dual_ass()
    assert r.success, r.error
    assert "[Script Info]" in content
    assert "[V4+ Styles]" in content
    assert "[Events]" in content
    # Required ASS metadata
    assert "ScriptType: v4.00+" in content
    assert "PlayResX: 1920" in content
    assert "PlayResY: 1080" in content


def test_dual_ass_emits_primary_and_secondary_styles():
    content, r = _render_dual_ass()
    assert r.success
    assert re.search(r"^Style: Primary,", content, re.M), \
        "Primary style line missing"
    assert re.search(r"^Style: Secondary,", content, re.M), \
        "Secondary style line missing"


def test_dual_ass_default_secondary_font_is_cjk_sc():
    """Without an explicit override, secondary_font defaults to a CJK font."""
    content, r = _render_dual_ass()
    assert r.success
    m = re.search(r"^Style: Secondary,([^,]+),", content, re.M)
    assert m, "Secondary style line not parseable"
    assert "CJK" in m.group(1) or "Noto" in m.group(1), \
        f"Default secondary font should be CJK-capable, got {m.group(1)!r}"


def test_dual_ass_secondary_font_override_takes_effect():
    content, r = _render_dual_ass(secondary_font="Microsoft YaHei")
    assert r.success
    m = re.search(r"^Style: Secondary,([^,]+),", content, re.M)
    assert m and m.group(1) == "Microsoft YaHei"


def test_dual_ass_emits_one_dialogue_per_segment_pair():
    """2 source segments → exactly 2 Dialogue events."""
    content, r = _render_dual_ass()
    assert r.success
    dialogue_lines = [
        line for line in content.splitlines()
        if line.startswith("Dialogue:")
    ]
    assert len(dialogue_lines) == 2, \
        f"Expected 2 Dialogue events, got {len(dialogue_lines)}"


def test_dual_ass_uses_style_switch_tag_for_secondary_line():
    """Each Dialogue line must contain `{\\rSecondary}` so libass renders
    the second line in the Secondary style (smaller, CJK font)."""
    content, r = _render_dual_ass()
    assert r.success
    dialogue_lines = [
        line for line in content.splitlines()
        if line.startswith("Dialogue:")
    ]
    for line in dialogue_lines:
        assert r"{\rSecondary}" in line, \
            f"Dialogue missing style switch tag: {line!r}"


def test_dual_ass_requires_matching_segment_counts():
    sg = SubtitleGen()
    mismatched = _target_segments()[:1]  # only 1 target for 2 source
    r = sg.execute({
        "segments": _sample_segments(),
        "target_segments": mismatched,
        "format": "dual_ass",
    })
    assert not r.success
    assert "target_segments length" in (r.error or "")


def test_dual_ass_requires_target_segments():
    sg = SubtitleGen()
    r = sg.execute({
        "segments": _sample_segments(),
        "format": "dual_ass",
    })
    assert not r.success
    assert "requires" in (r.error or "")


def test_dual_ass_preserves_source_timestamps_in_dialogue():
    """The translated side MUST NOT shift timestamps — that's the entire
    reason we keep the dual pipeline in the chain."""
    content, r = _render_dual_ass()
    assert r.success
    # Each Dialogue uses ASS h:mm:ss.cc timestamp format. Source seg 0:
    # 0.0s..2.5s → "0:00:00.00" .. "0:00:02.50"
    assert "0:00:00.00,0:00:02.50" in content
    # Source seg 1: 2.6s..5.4s → "0:00:02.60" .. "0:00:05.40"
    assert "0:00:02.60,0:00:05.40" in content


def test_dual_srt_emits_one_cue_per_segment_pair():
    """dual_srt variant — both lines inside the same cue, newline-separated."""
    sg = SubtitleGen()
    tmp_ctx = tempfile.TemporaryDirectory()
    tmpdir = tmp_ctx.__enter__()
    try:
        out = Path(tmpdir) / "test.srt"
        r = sg.execute({
            "segments": _sample_segments(),
            "target_segments": _target_segments(),
            "format": "dual_srt",
            "output_path": str(out),
        })
        content = out.read_text(encoding="utf-8")
    finally:
        tmp_ctx.__exit__(None, None, None)

    assert r.success, r.error
    assert content.count("-->") == 2
    assert "新鲜烘焙" in content
    assert "Freshly baked" in content