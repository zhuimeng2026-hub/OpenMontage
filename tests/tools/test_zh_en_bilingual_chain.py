"""End-to-end smoke test for the zh ↔ en bilingual subtitle chain.

Wires the three stages of the `zh-en-bilingual-subtitle` pipeline together:

    transcriber (mocked) → nllb_translator (mocked) → subtitle_gen (real)

The point is to validate the *contract* between stages without requiring the
~2.4 GB NLLB model or an audio file:

  - Stage 1 returns segment dicts with the right shape.
  - Stage 2 mutates only `text` and preserves `start` / `end` / `words` 1:1.
  - Stage 3 consumes both inputs verbatim into a renderable ASS/SRT.

Run with the standard pytest invocation:

    python -m pytest tests/tools/test_zh_en_bilingual_chain.py -v

Heavy-deps-free: only uses the standard library + the project's pure-Python
renderer + the registry (which is in-process).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from tools.subtitle.subtitle_gen import SubtitleGen  # noqa: E402
from tools.tool_registry import registry  # noqa: E402


# ---------------------------------------------------------------------------
# Mocked source audio → segments (stand-in for the `transcriber` tool)
# ---------------------------------------------------------------------------

def _mock_transcriber_segments() -> list[dict]:
    """Pretend faster-whisper produced these from a 6-second Mandarin clip."""
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


def _mock_nllb_translate_segments(segments: list[dict], **kwargs: Any) -> dict:
    """Stand-in for NLLB translation. Returns the same shape as
    `nllb_translator.execute({"segments": ...})`.

    Each Chinese segment gets a hardcoded English counterpart that matches
    its source text semantically — enough to exercise the chain end-to-end.
    """
    glossary = {
        "新鲜烘焙，源自云南。": "Freshly baked, from Yunnan.",
        "每一颗都是手工挑选。": "Every bean is hand-picked.",
    }
    out_segments = []
    for seg in segments:
        translated = glossary.get(seg["text"], f"[{seg['text']}]")
        new_seg = dict(seg)
        new_seg["text"] = translated
        new_seg["_source_words_text"] = " ".join(
            w.get("word", "") for w in seg.get("words", [])
        )
        out_segments.append(new_seg)
    return {
        "success": True,
        "data": {
            "segments": out_segments,
            "source_lang": "zh",
            "target_lang": "en",
            "segment_count": len(out_segments),
        },
    }


# ---------------------------------------------------------------------------
# Chain tests
# ---------------------------------------------------------------------------

def test_chain_segments_contract_preserved_through_translation():
    """If NLLB translator adds/removes segments, subtitle_gen rejects it.
    Verify the chain honours 1:1 alignment by faking both stages."""
    src = _mock_transcriber_segments()
    fake_translation = _mock_nllb_translate_segments(src)

    assert len(fake_translation["data"]["segments"]) == len(src), \
        "Translator must preserve segment count"

    for orig, translated in zip(src, fake_translation["data"]["segments"]):
        # Timestamps preserved verbatim — the whole point of dual subtitle.
        assert translated["start"] == orig["start"]
        assert translated["end"] == orig["end"]
        assert translated["words"] == orig["words"]
        # Text rewritten.
        assert translated["text"] != orig["text"]


def test_chain_dual_ass_smoke(tmp_path):
    """End-to-end: mock both upstream stages, run subtitle_gen for real,
    and verify the rendered file is a valid bilingual ASS with one Dialogue
    per segment pair."""
    src_segments = _mock_transcriber_segments()
    translated = _mock_nllb_translate_segments(src_segments)["data"]["segments"]

    out = tmp_path / "bilingual.ass"
    r = SubtitleGen().execute({
        "segments": src_segments,
        "target_segments": translated,
        "format": "dual_ass",
        "output_path": str(out),
    })
    assert r.success, r.error
    assert out.exists()

    content = out.read_text(encoding="utf-8")
    assert "[Script Info]" in content
    assert "[V4+ Styles]" in content
    assert "[Events]" in content
    # Both languages appear
    assert "新鲜烘焙" in content
    assert "Freshly baked" in content
    # Style switch in every Dialogue
    dialogue_count = sum(
        1 for line in content.splitlines() if line.startswith("Dialogue:")
    )
    assert dialogue_count == 2
    for line in content.splitlines():
        if line.startswith("Dialogue:"):
            assert r"{\rSecondary}" in line


def test_chain_dual_srt_smoke(tmp_path):
    """Same chain but emitting dual_srt instead of dual_ass."""
    src_segments = _mock_transcriber_segments()
    translated = _mock_nllb_translate_segments(src_segments)["data"]["segments"]

    out = tmp_path / "bilingual.srt"
    r = SubtitleGen().execute({
        "segments": src_segments,
        "target_segments": translated,
        "format": "dual_srt",
        "output_path": str(out),
    })
    assert r.success, r.error

    content = out.read_text(encoding="utf-8")
    assert content.count("-->") == 2
    assert "新鲜烘焙" in content
    assert "Freshly baked" in content


def test_chain_translator_returns_failure_propagates_cleanly(tmp_path):
    """If NLLB translator returns fewer segments than the source (e.g.,
    dropped a chunk on OOM), subtitle_gen must reject with a useful error
    instead of silently rendering a misaligned subtitle."""
    src_segments = _mock_transcriber_segments()

    # Case A: empty target_segments — translator returned nothing.
    r_empty = SubtitleGen().execute({
        "segments": src_segments,
        "target_segments": [],
        "format": "dual_ass",
        "output_path": str(tmp_path / "should-not-exist-a.ass"),
    })
    assert not r_empty.success
    assert "requires" in (r_empty.error or "").lower()

    # Case B: length mismatch — translator dropped a segment.
    r_mismatch = SubtitleGen().execute({
        "segments": src_segments,
        "target_segments": [_mock_nllb_translate_segments(src_segments)["data"]["segments"][0]],
        "format": "dual_ass",
        "output_path": str(tmp_path / "should-not-exist-b.ass"),
    })
    assert not r_mismatch.success
    assert "target_segments length" in (r_mismatch.error or "")


def test_chain_via_registry_with_mocked_translator(monkeypatch):
    """Exercise the chain through the *real* tool registry, mocking only
    nllb_translator.execute so we don't need the heavy deps."""
    registry.discover()
    nllb = registry.get("nllb_translator")
    assert nllb is not None

    def fake_execute(self, inputs):
        return _fake_translate_result(inputs)

    monkeypatch.setattr(
        type(nllb), "execute", fake_execute
    )

    # Invoke through the registry the way `execute_tool(name=...)` would.
    segments_in = _mock_transcriber_segments()
    r = nllb.execute({"segments": segments_in, "source_lang": "zh", "target_lang": "en"})
    assert r.success
    out_segments = r.data["segments"]
    assert len(out_segments) == len(segments_in)
    # Now feed into subtitle_gen (real)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "x.ass"
        sg_r = SubtitleGen().execute({
            "segments": segments_in,
            "target_segments": out_segments,
            "format": "dual_ass",
            "output_path": str(out),
        })
        assert sg_r.success, sg_r.error
        content = out.read_text(encoding="utf-8")
    assert "Freshly baked" in content


def _fake_translate_result(inputs: dict) -> "ToolResult":
    """Build a fake ToolResult from a segments input, in the exact shape the
    real NLLB translator would return."""
    from tools.base_tool import ToolResult

    src = inputs["segments"]
    target_lang = inputs.get("target_lang", "zh")
    glossary = {
        "新鲜烘焙，源自云南。": "Freshly baked, from Yunnan.",
        "每一颗都是手工挑选。": "Every bean is hand-picked.",
    }
    out_segments = []
    for seg in src:
        new = dict(seg)
        if target_lang == "en":
            new["text"] = glossary.get(seg["text"], f"[{seg['text']}]")
        else:
            new["text"] = seg["text"]  # identity for the zh→zh case
        new["_source_words_text"] = " ".join(
            w.get("word", "") for w in seg.get("words", [])
        )
        out_segments.append(new)

    return ToolResult(
        success=True,
        data={
            "segments": out_segments,
            "source_lang": inputs.get("source_lang", "zh"),
            "target_lang": target_lang,
            "segment_count": len(out_segments),
        },
    )