"""Subtitle generation tool.

Converts word-level timestamps from the transcriber into SRT, VTT,
or caption JSON formats. Pure Python — no external dependencies beyond
the standard library.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolTier,
)


class SubtitleGen(BaseTool):
    name = "subtitle_gen"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "subtitle"
    provider = "openmontage"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = []  # pure Python
    install_instructions = "No external dependencies required."
    agent_skills = ["remotion-best-practices"]

    capabilities = ["generate_srt", "generate_vtt", "generate_caption_json"]

    input_schema = {
        "type": "object",
        "required": ["segments"],
        "properties": {
            "segments": {
                "type": "array",
                "description": "Transcript segments from transcriber (with words and timestamps)",
            },
            "target_segments": {
                "type": "array",
                "description": (
                    "Optional translated segments (e.g. from the `translator` "
                    "tool). Must share the same segment count and aligned "
                    "start/end timestamps as `segments`. Used by `dual_srt` "
                    "and `dual_ass` formats to render primary + secondary "
                    "language side-by-side. Primary = segments (top), "
                    "secondary = target_segments (bottom)."
                ),
            },
            "format": {
                "type": "string",
                "enum": ["srt", "vtt", "json", "dual_srt", "dual_ass", "remotion_bilingual_captions"],
                "default": "srt",
                "description": (
                    "`srt`/`vtt`/`json` for single-language output. "
                    "`dual_srt` renders both languages on consecutive lines in "
                    "the same cue (English top, target bottom). "
                    "`dual_ass` emits an ASS file with two styles (Primary, "
                    "Secondary) so FFmpeg `subtitles=` can burn-in the pair. "
                    "`remotion_bilingual_captions` outputs JSON shaped for the "
                    "BilingualCaptionOverlay Remotion composition "
                    "(primaryWords[]/secondaryWords[] with startMs/endMs ints)."
                ),
            },
            "output_path": {"type": "string"},
            "max_chars_per_line": {"type": "integer", "default": 42},
            "max_words_per_cue": {"type": "integer", "default": 8},
            "highlight_style": {
                "type": "string",
                "enum": ["none", "word_by_word", "karaoke"],
                "default": "none",
            },
            "corrections": {
                "type": "object",
                "description": (
                    "Dictionary of word corrections for common ASR misrecognitions. "
                    "Keys are the wrong word (case-insensitive), values are the "
                    "correct replacement. Applied before generating subtitles. "
                    "Example: {\"cloud\": \"Claude\", \"co-pilot\": \"Copilot\"}."
                ),
            },
            "secondary_font": {
                "type": "string",
                "default": "Noto Sans CJK SC",
                "description": (
                    "Font name for the secondary (translated) line in `dual_ass` "
                    "output. Defaults to Noto Sans CJK SC for Chinese subtitles."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=128, vram_mb=0, disk_mb=10)
    idempotency_key_fields = ["segments", "format", "max_words_per_cue"]
    side_effects = ["writes subtitle file to output_path"]
    user_visible_verification = [
        "Play video with generated subtitles and verify timing",
    ]

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        segments = inputs["segments"]
        fmt = inputs.get("format", "srt")
        max_words = inputs.get("max_words_per_cue", 8)
        max_chars = inputs.get("max_chars_per_line", 42)
        highlight_style = inputs.get("highlight_style", "none")
        output_path = inputs.get("output_path")
        corrections = inputs.get("corrections")
        target_segments = inputs.get("target_segments")
        secondary_font = inputs.get("secondary_font", "Noto Sans CJK SC")

        start = time.time()

        # Apply word corrections if provided
        if corrections:
            segments = self._apply_corrections(segments, corrections)

        # Build cues from word-level timestamps
        cues = self._build_cues(segments, max_words, max_chars)

        # For dual_*: align target_segments to the same cues by index. We do
        # NOT call `_build_cues` on the secondary side because that would
        # re-chunk the translated text on word/char boundaries and break the
        # 1:1 alignment with the primary cues.
        secondary_segments: list[dict] | None = None
        if fmt in ("dual_srt", "dual_ass", "remotion_bilingual_captions"):
            if not target_segments:
                return ToolResult(
                    success=False,
                    error=f"format={fmt} requires `target_segments`.",
                )
            if len(target_segments) != len(segments):
                return ToolResult(
                    success=False,
                    error=(
                        f"target_segments length ({len(target_segments)}) must "
                        f"match segments length ({len(segments)}). The "
                        "`translator` tool preserves segment count 1:1 — "
                        "double-check the translator was called on the same "
                        "segments you passed in."
                    ),
                )
            secondary_segments = target_segments

        if fmt == "srt":
            content = self._render_srt(cues, highlight_style)
            ext = ".srt"
        elif fmt == "vtt":
            content = self._render_vtt(cues, highlight_style)
            ext = ".vtt"
        elif fmt == "json":
            content = json.dumps({"cues": cues, "highlight_style": highlight_style}, indent=2)
            ext = ".caption.json"
        elif fmt == "dual_srt":
            assert secondary_segments is not None
            content = self._render_dual_srt(segments, secondary_segments)
            ext = ".srt"
        elif fmt == "dual_ass":
            assert secondary_segments is not None
            content = self._render_dual_ass(segments, secondary_segments, secondary_font)
            ext = ".ass"
        elif fmt == "remotion_bilingual_captions":
            assert secondary_segments is not None
            payload = {
                "format": "remotion_bilingual_captions",
                "primaryWords": SubtitleGen._segments_to_word_captions(segments),
                "secondaryWords": SubtitleGen._segments_to_word_captions(
                    secondary_segments
                ),
            }
            content = json.dumps(payload, indent=2, ensure_ascii=False)
            ext = ".remotion_bilingual.json"
        else:
            return ToolResult(success=False, error=f"Unknown format: {fmt}")

        if output_path is None:
            output_path = f"subtitles{ext}"
        # If the caller passed a path without the dual_*-implied extension,
        # don't silently rewrite it — respect what they asked for.
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(content, encoding="utf-8")

        elapsed = time.time() - start

        return ToolResult(
            success=True,
            data={
                "format": fmt,
                "cue_count": len(cues),
                "output": str(out),
            },
            artifacts=[str(out)],
            duration_seconds=round(elapsed, 2),
        )

    @staticmethod
    def _apply_corrections(
        segments: list[dict], corrections: dict[str, str]
    ) -> list[dict]:
        """Apply word-level corrections to transcript segments.

        Handles case-insensitive matching and preserves punctuation.
        """
        import copy

        corr = {k.lower(): v for k, v in corrections.items()}
        result = copy.deepcopy(segments)

        for seg in result:
            words = seg.get("words", [])
            for w in words:
                raw = w.get("word", "").strip()
                # Strip punctuation for lookup, preserve it
                stripped = raw.lower().rstrip(".,!?;:'\"")
                if stripped in corr:
                    trailing = raw[len(stripped):]
                    w["word"] = corr[stripped] + trailing
            # Also fix segment-level text
            if "text" in seg and words:
                seg["text"] = " ".join(w["word"] for w in words)
            elif "text" in seg:
                for wrong, right in corr.items():
                    import re as _re
                    seg["text"] = _re.sub(
                        r"\b" + _re.escape(wrong) + r"\b",
                        right,
                        seg["text"],
                        flags=_re.IGNORECASE,
                    )

        return result

    def _build_cues(
        self, segments: list[dict], max_words: int, max_chars: int
    ) -> list[dict]:
        """Group words into display cues respecting max_words and max_chars."""
        # Collect all words with timestamps
        all_words = []
        for seg in segments:
            words = seg.get("words", [])
            if words:
                all_words.extend(words)
            elif "text" in seg:
                # Fallback: segment-level only (no word timestamps)
                all_words.append({
                    "word": seg["text"],
                    "start": seg["start"],
                    "end": seg["end"],
                })

        if not all_words:
            return []

        cues = []
        buf: list[dict] = []
        buf_text = ""

        for w in all_words:
            word_text = w["word"].strip()
            candidate = f"{buf_text} {word_text}".strip() if buf_text else word_text

            if buf and (len(buf) >= max_words or len(candidate) > max_chars):
                cues.append({
                    "index": len(cues) + 1,
                    "start": buf[0]["start"],
                    "end": buf[-1]["end"],
                    "text": buf_text,
                    "words": [
                        {"word": b["word"].strip(), "start": b["start"], "end": b["end"]}
                        for b in buf
                    ],
                })
                buf = []
                buf_text = ""

            buf.append(w)
            buf_text = f"{buf_text} {word_text}".strip() if buf_text else word_text

        # Flush remaining
        if buf:
            cues.append({
                "index": len(cues) + 1,
                "start": buf[0]["start"],
                "end": buf[-1]["end"],
                "text": buf_text,
                "words": [
                    {"word": b["word"].strip(), "start": b["start"], "end": b["end"]}
                    for b in buf
                ],
            })

        return cues

    def _render_srt(self, cues: list[dict], highlight_style: str = "none") -> str:
        lines = []
        if highlight_style == "word_by_word":
            # Emit one cue per word for word-by-word reveal
            idx = 1
            for cue in cues:
                for word_info in cue.get("words", []):
                    lines.append(str(idx))
                    lines.append(
                        f"{self._ts_srt(word_info['start'])} --> {self._ts_srt(word_info['end'])}"
                    )
                    lines.append(word_info["word"])
                    lines.append("")
                    idx += 1
        elif highlight_style == "karaoke":
            # Show full cue text but bold the active word using SRT HTML tags
            for cue in cues:
                words = cue.get("words", [])
                if not words:
                    lines.append(str(cue["index"]))
                    lines.append(f"{self._ts_srt(cue['start'])} --> {self._ts_srt(cue['end'])}")
                    lines.append(cue["text"])
                    lines.append("")
                    continue
                for wi, word_info in enumerate(words):
                    lines.append(str(cue["index"] * 100 + wi))
                    lines.append(
                        f"{self._ts_srt(word_info['start'])} --> {self._ts_srt(word_info['end'])}"
                    )
                    parts = []
                    for wj, w in enumerate(words):
                        if wj == wi:
                            parts.append(f"<b>{w['word']}</b>")
                        else:
                            parts.append(w["word"])
                    lines.append(" ".join(parts))
                    lines.append("")
        else:
            for cue in cues:
                lines.append(str(cue["index"]))
                lines.append(f"{self._ts_srt(cue['start'])} --> {self._ts_srt(cue['end'])}")
                lines.append(cue["text"])
                lines.append("")
        return "\n".join(lines)

    def _render_vtt(self, cues: list[dict], highlight_style: str = "none") -> str:
        lines = ["WEBVTT", ""]
        if highlight_style == "word_by_word":
            for cue in cues:
                for word_info in cue.get("words", []):
                    lines.append(
                        f"{self._ts_vtt(word_info['start'])} --> {self._ts_vtt(word_info['end'])}"
                    )
                    lines.append(word_info["word"])
                    lines.append("")
        elif highlight_style == "karaoke":
            for cue in cues:
                words = cue.get("words", [])
                if not words:
                    lines.append(f"{self._ts_vtt(cue['start'])} --> {self._ts_vtt(cue['end'])}")
                    lines.append(cue["text"])
                    lines.append("")
                    continue
                for wi, word_info in enumerate(words):
                    lines.append(
                        f"{self._ts_vtt(word_info['start'])} --> {self._ts_vtt(word_info['end'])}"
                    )
                    parts = []
                    for wj, w in enumerate(words):
                        if wj == wi:
                            parts.append(f"<b>{w['word']}</b>")
                        else:
                            parts.append(w["word"])
                    lines.append(" ".join(parts))
                    lines.append("")
        else:
            for cue in cues:
                lines.append(f"{self._ts_vtt(cue['start'])} --> {self._ts_vtt(cue['end'])}")
                lines.append(cue["text"])
                lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _align_dual(primary_segments: list[dict], secondary_segments: list[dict]) -> list[tuple[dict, dict]]:
        """Pair primary/secondary segments 1:1 for bilingual render.

        The bilingual path skips `_build_cues` (which would re-chunk word
        boundaries) because each segment in `segments` already corresponds
        to a speaker turn; if we re-chunked, the translated counterpart
        could land in the wrong cue. Aligning by index + timestamps keeps
        audio alignment intact.
        """
        out: list[tuple[dict, dict]] = []
        for pri, sec in zip(primary_segments, secondary_segments):
            # Trust the translated segment's timestamps (translator preserves
            # start/end); fall back to source if translator dropped them.
            cue = {
                "start": pri.get("start", sec.get("start", 0.0)),
                "end": pri.get("end", sec.get("end", 0.0)),
                "text": pri.get("text", ""),
            }
            sec_cue = {
                "start": sec.get("start", pri.get("start", 0.0)),
                "end": sec.get("end", pri.get("end", 0.0)),
                "text": sec.get("text", ""),
            }
            out.append((cue, sec_cue))
        return out

    @staticmethod
    def _render_dual_srt(
        primary_segments: list[dict],
        secondary_segments: list[dict],
    ) -> str:
        """Render bilingual SRT — primary line above, secondary line below.

        Both languages are emitted in the SAME cue with a newline between
        them (legal SRT). Players without multi-line support will show the
        primary line; players with multi-line (VLC, mpv, most web players)
        render both. For a single bilingual layout burn-in via FFmpeg ASS,
        use `dual_ass` instead.
        """
        pairs = SubtitleGen._align_dual(primary_segments, secondary_segments)
        lines: list[str] = []
        for i, (cue, sec) in enumerate(pairs, start=1):
            lines.append(str(i))
            lines.append(
                f"{SubtitleGen._ts_srt(cue['start'])} --> {SubtitleGen._ts_srt(cue['end'])}"
            )
            lines.append(cue["text"])
            lines.append(sec["text"])
            lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _render_dual_ass(
        primary_segments: list[dict],
        secondary_segments: list[dict],
        secondary_font: str,
    ) -> str:
        """Render bilingual ASS — Primary style on top, Secondary below.

        Two `[V4+ Styles]` entries are emitted:
          Primary   — English (or source) — default font, larger size
          Secondary — translated language — `secondary_font` for CJK, smaller
        The Primary style uses `Alignment=2` (bottom center). Secondary uses
        a smaller MarginV to push it further down. This produces the
        canonical "English top, 中文 bottom" layout.
        """
        header = (
            "[Script Info]\n"
            "ScriptType: v4.00+\n"
            "Collisions: Normal\n"
            "PlayResX: 1920\n"
            "PlayResY: 1080\n"
            "\n"
            "[V4+ Styles]\n"
            "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
            "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
            "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
            "Alignment, MarginL, MarginR, MarginV, Encoding\n"
            "Style: Primary,Arial,52,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
            "1,0,0,0,100,100,0,0,1,2,1,2,40,40,80,1\n"
            f"Style: Secondary,{secondary_font},44,&H00FFFFFF,&H000000FF,"
            "&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,2,1,2,40,40,30,1\n"
            "\n"
            "[Events]\n"
            "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
            "MarginV, Effect, Text\n"
        )

        def fmt(t: float) -> str:
            h, m, s, ms = SubtitleGen._hmsms(t)
            cs = ms // 10
            return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

        pairs = SubtitleGen._align_dual(primary_segments, secondary_segments)
        events: list[str] = []
        for cue, sec in pairs:
            text = cue["text"] + r" {\rSecondary}" + sec["text"]
            events.append(
                f"Dialogue: 0,{fmt(cue['start'])},{fmt(cue['end'])},Primary,,0,0,0,,{text}"
            )
        return header + "\n".join(events) + "\n"

    @staticmethod
    def _segments_to_word_captions(segments: list[dict]) -> list[dict]:
        """Flatten transcript segments to Remotion `WordCaption[]`.

        Output shape matches `BilingualCaptionOverlay`'s `WordCaption`
        interface in remotion-composer/src/components/BilingualCaptionOverlay.tsx:
            {word: str, startMs: int, endMs: int}

        Seconds → milliseconds conversion (round to int) so the timeline
        is stable across int math (Remotion's `useCurrentFrame` and
        `interpolate` both operate on ints at 30fps).

        Sentence-only fallback: when a segment has no `words[]` (e.g.,
        FunASR paraformer-zh without the word-timestamp model), emit a
        single WordCaption spanning the whole segment. The component
        groups these into multi-char cues by `wordsPerPage`, so per-char
        karaoke isn't possible with sentence-level input — pick the
        `speech_seaco_paraformer_large_asrnat` model for that.
        """
        out: list[dict] = []
        for seg in segments:
            words = seg.get("words") or []
            if words:
                for w in words:
                    word = (w.get("word") or "").strip()
                    if not word:
                        continue
                    start_s = w.get("start", seg.get("start", 0))
                    end_s = w.get("end", seg.get("end", 0))
                    out.append({
                        "word": word,
                        "startMs": int(round(float(start_s) * 1000)),
                        "endMs": int(round(float(end_s) * 1000)),
                    })
            elif seg.get("text"):
                out.append({
                    "word": seg["text"].strip(),
                    "startMs": int(round(float(seg.get("start", 0)) * 1000)),
                    "endMs": int(round(float(seg.get("end", 0)) * 1000)),
                })
        return out

    @staticmethod
    def _hmsms(seconds: float) -> tuple[int, int, int, int]:
        """Decompose seconds into (h, m, s, ms), rounding to whole ms first.

        Rounding to total milliseconds before splitting the fields lets the
        carry propagate: 0.9995s+ must become the next second (…,000), not a
        malformed 4-digit …,1000 with the seconds field left unincremented.
        """
        total_ms = int(round(max(0.0, seconds) * 1000))
        h, rem = divmod(total_ms, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1_000)
        return h, m, s, ms

    @classmethod
    def _ts_srt(cls, seconds: float) -> str:
        """Format seconds as SRT timestamp: HH:MM:SS,mmm"""
        h, m, s, ms = cls._hmsms(seconds)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    @classmethod
    def _ts_vtt(cls, seconds: float) -> str:
        """Format seconds as VTT timestamp: HH:MM:SS.mmm"""
        h, m, s, ms = cls._hmsms(seconds)
        return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
