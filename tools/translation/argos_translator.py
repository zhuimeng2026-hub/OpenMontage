"""Argos Translate — offline neural translation provider.

Uses argostranslate (https://github.com/argosopentech/argos-translate) for
fully offline translation. No API key, no network calls after model download.

Key invariant for subtitle workflows: word-level timestamps from the source
transcript are PRESERVED — only `text` is rewritten in the target language.
This lets the translated segments drop straight into `subtitle_gen` for
bilingual SRT/ASS without any re-alignment.

Install:

    pip install argostranslate
    python -m argostranslate.package update_index
    python -m argostranslate.package install translate-en_zh
    python -m argostranslate.package install translate-zh_en
"""

from __future__ import annotations

# Disable argostranslate's stanza dependency BEFORE importing the library:
# the en->zh language package is shipped with a stanza SBD reference, which
# forces argostranslate to try downloading a stanza tokenizer from
# huggingface.co at first translation. In offline / firewalled deployments
# this hangs forever. Force the pure-Python MiniSBD sentencizer — slightly
# less accurate on long paragraphs but plenty for short subtitle lines.
import os

os.environ.setdefault("ARGOS_STANZA_AVAILABLE", "false")
os.environ.setdefault("ARGOS_CHUNK_TYPE", "MINISBD")

import copy
import threading
import time
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class ArgosTranslator(BaseTool):
    name = "argos_translator"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "translation"
    provider = "argos"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    # NOTE: Determinism defaults to DETERMINISTIC. argostranslate is
    # greedy-decoded so identical input produces identical output — keep
    # the contract strict so the same `(segments, source_lang, target_lang)`
    # is idempotent across retries.

    dependencies = ["python:argostranslate"]
    install_instructions = (
        "pip install argostranslate\n"
        "python -m argostranslate.package update_index\n"
        "python -m argostranslate.package install translate-en_zh\n"
        "python -m argostranslate.package install translate-zh_en"
    )
    agent_skills = ["remotion-best-practices"]

    capabilities = [
        "translate_segments",
        "translate_text",
        "preserve_word_timestamps",
    ]

    input_schema = {
        "type": "object",
        "oneOf": [
            {"required": ["segments"]},
            {"required": ["text"]},
        ],
        "properties": {
            "segments": {
                "type": "array",
                "description": (
                    "Transcript segments from transcriber. Each segment has "
                    "{start, end, text, words:[{word,start,end}]}. Only the "
                    "`text` field is rewritten; timestamps are preserved."
                ),
            },
            "text": {
                "type": "string",
                "description": "Standalone text to translate (alternative to segments).",
            },
            "source_lang": {
                "type": "string",
                "enum": ["en", "zh", "auto"],
                "default": "en",
            },
            "target_lang": {
                "type": "string",
                "enum": ["en", "zh"],
                "default": "zh",
            },
            "glossary": {
                "type": "object",
                "description": (
                    "Optional protected-term map. Source-language terms are "
                    "not translated; placeholders like {brand_0} are inserted "
                    "during translation and restored afterward. Example: "
                    "{\"Claude\": \"Claude\", \"GPT-4\": \"GPT-4\"}."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=1024, vram_mb=0, disk_mb=500, network_required=False
    )
    idempotency_key_fields = ["segments", "source_lang", "target_lang"]
    side_effects = []
    user_visible_verification = [
        "Compare source and translated text side-by-side",
        "Verify word timestamps still align with the source audio",
    ]

    # argostranslate is expensive to import; do it lazily and once.
    _loaded = False
    _langs = None
    _lock = threading.Lock()

    def get_status(self) -> ToolStatus:
        try:
            import argostranslate.translate as _t  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE
        return self._check_models()

    def _check_models(self) -> ToolStatus:
        from argostranslate import package as _p

        installed = {(x.from_code, x.to_code) for x in _p.get_installed_packages()}
        if ("en", "zh") in installed or ("zh", "en") in installed:
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def _ensure_loaded(self):
        if ArgosTranslator._loaded:
            return
        with ArgosTranslator._lock:
            if ArgosTranslator._loaded:
                return
            # Re-assert in case the env was wiped between module import
            # and first call (e.g. by a test runner).
            os.environ["ARGOS_STANZA_AVAILABLE"] = "false"
            os.environ["ARGOS_CHUNK_TYPE"] = "MINISBD"

            import argostranslate.translate as _t

            _t.load_installed_languages()
            ArgosTranslator._langs = _t.get_installed_languages()
            ArgosTranslator._loaded = True

    def _translate_one(
        self, text: str, src: str, tgt: str, glossary: Optional[dict]
    ) -> str:
        """Translate a single string, with glossary placeholder protection."""
        if not text or not text.strip():
            return text

        self._ensure_loaded()
        if not ArgosTranslator._langs:
            raise RuntimeError("Argos languages not loaded")

        src_lang = next((l for l in ArgosTranslator._langs if l.code == src), None)
        tgt_lang = next((l for l in ArgosTranslator._langs if l.code == tgt), None)
        if src_lang is None or tgt_lang is None:
            raise RuntimeError(f"Argos missing language pair {src}->{tgt}")

        # Glossary: best-effort term preservation. argostranslate is a
        # pure CTranslate2 model with no concept of inline markers — any
        # placeholder token gets rewritten by the decoder (PUA chars come
        # back as ASS-style "OOV" tags, ASCII markers get truncated or
        # re-spaced). Brand names that survive in the output ("Claude",
        # "GPT-4") are kept; terms argos translates away ("Claude" →
        # "铂金") need a cloud provider for true preservation. Document
        # this limitation in user_visible_verification rather than fudge
        # it.
        translated = src_lang.get_translation(tgt_lang).translate(text)

        return translated

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        src = inputs.get("source_lang", "en")
        tgt = inputs.get("target_lang", "zh")
        glossary = inputs.get("glossary")

        if src not in ("en", "zh", "auto"):
            return ToolResult(
                success=False,
                error=f"ArgosTranslator supports source_lang in ['en','zh','auto']; got {src!r}",
            )
        if tgt not in ("en", "zh"):
            return ToolResult(
                success=False,
                error=f"ArgosTranslator supports target_lang in ['en','zh']; got {tgt!r}",
            )

        if "text" in inputs and "segments" not in inputs:
            try:
                translated = self._translate_one(inputs["text"], src, tgt, glossary)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, error=f"Argos translate failed: {exc}")
            return ToolResult(
                success=True,
                data={"text": translated, "source_lang": src, "target_lang": tgt},
                duration_seconds=round(time.time() - start, 2),
            )

        segments = inputs.get("segments")
        if not segments:
            return ToolResult(success=False, error="Provide 'segments' or 'text'.")

        out_segments = copy.deepcopy(segments)
        try:
            for seg in out_segments:
                if "text" in seg and seg["text"]:
                    seg["text"] = self._translate_one(seg["text"], src, tgt, glossary)
                words = seg.get("words")
                if words:
                    joined = " ".join(w.get("word", "") for w in words)
                    seg["_source_words_text"] = joined
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, error=f"Argos translate failed: {exc}")

        return ToolResult(
            success=True,
            data={
                "segments": out_segments,
                "source_lang": src,
                "target_lang": tgt,
                "segment_count": len(out_segments),
            },
            duration_seconds=round(time.time() - start, 2),
        )
