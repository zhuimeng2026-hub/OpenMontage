"""NLLB-200 — Meta's No Language Left Behind offline translation provider.

Uses HuggingFace `transformers` to load NLLB-200 distilled checkpoints and
translate locally. No API key, no network calls after model download. Pairs
with `transcriber` + `subtitle_gen` to deliver a fully offline Chinese ↔
English bilingual subtitle pipeline.

Key invariant for subtitle workflows: word-level timestamps from the source
transcript are PRESERVED — only `text` is rewritten in the target language.
This lets the translated segments drop straight into `subtitle_gen` for
bilingual SRT/ASS without any re-alignment.

Models (default = distilled-600M, the best size/quality tradeoff for CPU):
    facebook/nllb-200-distilled-600M   (~2.4GB on disk, ~1.5GB RAM, ~1.5GB VRAM)
    facebook/nllb-200-distilled-1.3B   (~5.0GB on disk, ~3.0GB RAM, ~3.0GB VRAM)
    facebook/nllb-200-3.3B             (~13GB  on disk, ~7.0GB RAM, ~7.0GB VRAM)

Install:

    pip install transformers torch sentencepiece
    # The model auto-downloads on first run. To pre-cache offline:
    python -c "from transformers import AutoTokenizer, AutoModelForSeq2SeqLM; \\
        AutoTokenizer.from_pretrained('facebook/nllb-200-distilled-600M'); \\
        AutoModelForSeq2SeqLM.from_pretrained('facebook/nllb-200-distilled-600M')"
"""

from __future__ import annotations

import copy
import os
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


# ISO 639-1 → FLORES-200 mapping for the languages we expose in the
# schema. NLLB supports 200 languages total — extending the enum below
# is the only thing needed to expose more.
_ISO_TO_FLORES: dict[str, str] = {
    "en": "eng_Latn",
    "zh": "zho_Hans",       # Simplified Chinese (default)
    "zh-tw": "zho_Hant",    # Traditional Chinese
    "ja": "jpn_Jpan",
    "ko": "kor_Hang",
    "fr": "fra_Latn",
    "de": "deu_Latn",
    "es": "spa_Latn",
    "ru": "rus_Cyrl",
    "ar": "arb_Arab",
    "hi": "hin_Deva",
    "pt": "por_Latn",
    "it": "ita_Latn",
}

# Reverse map for friendly error messages and CLI display.
_FLORES_TO_ISO: dict[str, str] = {v: k for k, v in _ISO_TO_FLORES.items()}


def _to_flores(code: str) -> str:
    """Accept either an ISO code ('zh') or a FLORES code ('zho_Hans')."""
    code = code.strip()
    if "_" in code:
        return code  # already FLORES
    return _ISO_TO_FLORES.get(code.lower(), code)


class NLLBTranslator(BaseTool):
    name = "nllb_translator"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "translation"
    provider = "nllb"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["python:transformers", "python:torch", "python:sentencepiece"]
    install_instructions = (
        "pip install transformers torch sentencepiece\n"
        "# First run downloads the model (~2.4GB for distilled-600M) into "
        "$HF_HOME or ~/.cache/huggingface/. To pre-cache for offline use:\n"
        "python -c \"from transformers import AutoTokenizer, "
        "AutoModelForSeq2SeqLM; "
        "AutoTokenizer.from_pretrained('facebook/nllb-200-distilled-600M'); "
        "AutoModelForSeq2SeqLM.from_pretrained('facebook/nllb-200-distilled-600M')\""
    )
    agent_skills = ["remotion-best-practices"]

    capabilities = [
        "translate_segments",
        "translate_text",
        "preserve_word_timestamps",
        "multilingual_200",
    ]

    # Languages we expose in the schema enum. NLLB supports many more;
    # extend this list (and _ISO_TO_FLORES) to expose additional pairs.
    _SUPPORTED_LANGS = sorted(_ISO_TO_FLORES.keys())

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
                "enum": _SUPPORTED_LANGS + ["auto"],
                "default": "en",
                "description": (
                    "Source language as ISO 639-1 ('en', 'zh', 'zh-tw') or "
                    "FLORES-200 ('eng_Latn', 'zho_Hans'). 'auto' requires "
                    "lang detection — not implemented in v0.1, will return "
                    "an error."
                ),
            },
            "target_lang": {
                "type": "string",
                "enum": _SUPPORTED_LANGS,
                "default": "zh",
            },
            "model_size": {
                "type": "string",
                "enum": [
                    "facebook/nllb-200-distilled-600M",
                    "facebook/nllb-200-distilled-1.3B",
                    "facebook/nllb-200-3.3B",
                ],
                "default": "facebook/nllb-200-distilled-600M",
                "description": (
                    "Distilled-600M is the recommended default. 1.3B is "
                    "better quality but ~2x slower. 3.3B requires ~7GB RAM/VRAM."
                ),
            },
            "max_length": {
                "type": "integer",
                "default": 512,
                "description": "Max tokens for the decoder. Default 512 covers subtitle segments comfortably.",
            },
            "glossary": {
                "type": "object",
                "description": (
                    "Optional protected-term map. Source-language terms are "
                    "not translated; placeholders like {brand_0} are inserted "
                    "during translation and restored afterward. Example: "
                    "{\"Claude\": \"Claude\", \"GPT-4\": \"GPT-4\"}. "
                    "NLLB has no native term-protection — brand names that "
                    "the decoder rewrites ('Claude' → '克劳德') need a cloud "
                    "provider or post-edit pass for guaranteed preservation."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        # 600M: ~1.5GB RAM on CPU, more if GPU. Conservative defaults so
        # the registry doesn't over-commit on shared hosts.
        cpu_cores=2, ram_mb=3072, vram_mb=2048, disk_mb=2500, network_required=False
    )
    idempotency_key_fields = ["segments", "source_lang", "target_lang", "model_size"]
    side_effects = []
    user_visible_verification = [
        "Compare source and translated text side-by-side",
        "Verify word timestamps still align with the source audio",
        "Spot-check protected-term preservation if `glossary` was used",
    ]

    # transformers is heavy to import; do it lazily and once per process.
    _model = None
    _tokenizer = None
    _loaded_for: Optional[str] = None  # model_size we currently have in memory
    _device: Optional[str] = None
    _lock = threading.Lock()

    def get_status(self) -> ToolStatus:
        try:
            import transformers  # noqa: F401
            import torch  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE

        # Try to load the default model + tokenizer to confirm they're
        # cached on disk. We don't actually keep them in memory here —
        # that's `_ensure_loaded`'s job — only check the cache presence.
        try:
            from transformers import AutoTokenizer, AutoModelForSeq2SeqLM

            default_model = "facebook/nllb-200-distilled-600M"
            try:
                # `from_pretrained` will hit the network if the model is
                # not cached, which is exactly what we want to surface as
                # UNAVAILABLE for offline-first deployments.
                AutoTokenizer.from_pretrained(
                    default_model, local_files_only=True
                )
                AutoModelForSeq2SeqLM.from_pretrained(
                    default_model, local_files_only=True
                )
            except (OSError, ValueError):
                # Cache miss. Model will download on first execute().
                return ToolStatus.DEGRADED
            return ToolStatus.AVAILABLE
        except Exception:
            return ToolStatus.UNAVAILABLE

    def _ensure_loaded(self, model_size: str) -> None:
        if NLLBTranslator._loaded_for == model_size:
            return
        with NLLBTranslator._lock:
            if NLLBTranslator._loaded_for == model_size:
                return

            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(model_size)
            model = AutoModelForSeq2SeqLM.from_pretrained(model_size)

            # CPU/GPU dispatch: keep it simple. If torch sees CUDA, use it
            # with float16; otherwise CPU with float32 (int8 quantization
            # would shave RAM but breaks the determinism claim).
            device = "cuda" if torch.cuda.is_available() else "cpu"
            dtype = torch.float16 if device == "cuda" else torch.float32
            model = model.to(device=device, dtype=dtype)
            model.eval()  # inference-only — no dropout

            NLLBTranslator._tokenizer = tokenizer
            NLLBTranslator._model = model
            NLLBTranslator._device = device
            NLLBTranslator._loaded_for = model_size

    def _translate_one(
        self,
        text: str,
        src_flores: str,
        tgt_flores: str,
        model_size: str,
        max_length: int,
        glossary: Optional[dict],
    ) -> str:
        """Translate a single string, with glossary placeholder protection."""
        if not text or not text.strip():
            return text

        self._ensure_loaded(model_size)
        assert NLLBTranslator._model is not None
        assert NLLBTranslator._tokenizer is not None

        # Glossary: replace each term with a non-linguistic placeholder
        # the decoder is unlikely to touch (PUA chars), then restore
        # afterward. Same trick argos_translator.py uses. NLLB will
        # occasionally still rewrite PUA tokens (it normalizes them to
        # <unk> or strips them); we accept best-effort here and document
        # the limitation in user_visible_verification.
        placeholders: dict[str, str] = {}
        masked = text
        if glossary:
            for i, term in enumerate(glossary.keys()):
                placeholder = f"{ i }"  # PUA chars survive tokenizer
                masked = masked.replace(term, placeholder)
                placeholders[placeholder] = glossary[term]

        import torch

        tokenizer = NLLBTranslator._tokenizer
        tokenizer.src_lang = src_flores
        enc = tokenizer(
            masked,
            return_tensors="pt",
            truncation=True,
            max_length=max_length,
        ).to(NLLBTranslator._device)

        # Force the target language token at the start of the decoder.
        # `forced_bos_token_id` is the supported way in NLLB / M2M100.
        forced_bos_token_id = tokenizer.convert_tokens_to_ids(tgt_flores)

        with torch.no_grad():
            out = NLLBTranslator._model.generate(
                **enc,
                forced_bos_token_id=forced_bos_token_id,
                max_length=max_length,
                num_beams=1,       # greedy → deterministic
                do_sample=False,
            )

        translated = tokenizer.decode(out[0], skip_special_tokens=True)

        # Restore glossary placeholders. If the decoder dropped or
        # rewrote a placeholder, the source term stays out — better to
        # lose a brand mention than to ship a translation with PUA chars.
        for placeholder, original in placeholders.items():
            translated = translated.replace(placeholder, original)
            # Also strip the literal "Placeholder" string the decoder
            # sometimes produces when it doesn't know the PUA char.
            translated = translated.replace(f"Placeholder", original)

        return translated.strip()

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        src = inputs.get("source_lang", "en")
        tgt = inputs.get("target_lang", "zh")
        model_size = inputs.get(
            "model_size", "facebook/nllb-200-distilled-600M"
        )
        max_length = inputs.get("max_length", 512)
        glossary = inputs.get("glossary")

        if src == "auto":
            return ToolResult(
                success=False,
                error=(
                    "source_lang='auto' requires language detection, which is "
                    "not implemented in nllb_translator v0.1. Run the `transcriber` "
                    "tool first and pass its detected language explicitly, or "
                    "pre-detect with `langdetect`."
                ),
            )

        # Validate language codes early — clearer error than waiting
        # for the tokenizer to reject an unknown FLORES code mid-load.
        try:
            src_flores = _to_flores(src)
            tgt_flores = _to_flores(tgt)
        except KeyError as exc:
            return ToolResult(
                success=False,
                error=f"Unknown language code: {exc.args[0]}",
            )

        if src_flores == tgt_flores:
            return ToolResult(
                success=False,
                error=(
                    f"source_lang and target_lang resolve to the same code "
                    f"({src_flores}). Pick a different target."
                ),
            )

        if "text" in inputs and "segments" not in inputs:
            try:
                translated = self._translate_one(
                    inputs["text"], src_flores, tgt_flores, model_size,
                    max_length, glossary,
                )
            except Exception as exc:  # noqa: BLE001
                return ToolResult(
                    success=False, error=f"NLLB translate failed: {exc}"
                )
            return ToolResult(
                success=True,
                data={
                    "text": translated,
                    "source_lang": src,
                    "target_lang": tgt,
                    "source_flores": src_flores,
                    "target_flores": tgt_flores,
                    "model_size": model_size,
                    "device": NLLBTranslator._device,
                },
                duration_seconds=round(time.time() - start, 2),
            )

        segments = inputs.get("segments")
        if not segments:
            return ToolResult(
                success=False, error="Provide 'segments' or 'text'."
            )

        out_segments = copy.deepcopy(segments)
        try:
            for seg in out_segments:
                if "text" in seg and seg["text"]:
                    seg["text"] = self._translate_one(
                        seg["text"], src_flores, tgt_flores, model_size,
                        max_length, glossary,
                    )
                # Preserve the original word stream so subtitle_gen can
                # fall back to it if a downstream consumer needs source-
                # language word timestamps for the dual ASS layout.
                words = seg.get("words")
                if words:
                    seg["_source_words_text"] = " ".join(
                        w.get("word", "") for w in words
                    )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"NLLB translate failed: {exc}"
            )

        return ToolResult(
            success=True,
            data={
                "segments": out_segments,
                "source_lang": src,
                "target_lang": tgt,
                "source_flores": src_flores,
                "target_flores": tgt_flores,
                "model_size": model_size,
                "device": NLLBTranslator._device,
                "segment_count": len(out_segments),
            },
            duration_seconds=round(time.time() - start, 2),
        )