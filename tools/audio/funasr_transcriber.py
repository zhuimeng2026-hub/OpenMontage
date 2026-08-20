"""FunASR — Alibaba's open-source Chinese-first ASR provider.

Wraps `funasr` (https://github.com/modelscope/FunASR) for offline Mandarin
speech recognition with a Chinese-specialized model family. Default is
`paraformer-zh`, the Paraformer non-streaming Mandarin model — significantly
better WER on Chinese audio than faster-whisper's multilingual model.

Word timestamp capability depends on the chosen model:
  - paraformer-zh           — sentence-level only (default)
  - paraformer-large        — sentence-level only
  - SenseVoiceSmall         — multi-lingual (zh/en/yue/ja/ko), sentence-level
  - speech_seaco_paraformer_large_asrnat — **word-level timestamps**

When the chosen model does not provide word-level timestamps, this tool
falls back to evenly distributing characters across the sentence's time
range. That is correct enough for subtitle rendering (where the consumer
groups words into multi-character cues by `max_chars_per_line`) but is NOT
suitable for word-by-word karaoke highlights — pick
`speech_seaco_paraformer_large_asrnat` for that.

Output contract matches `transcriber.py` (faster-whisper) so the result
drops straight into `nllb_translator` without for the bilingual pipeline:

```python
{
    "id": int, "start": float, "end": float,
    "text": str,
    "words": [{"word": str, "start": float, "end": float}, ...]
}
```

Install:

    pip install funasr modelscope
    # First run downloads the model from ModelScope (~400MB for paraformer-zh)
    # to ~/.cache/modelscope/. To pre-cache offline:
    python -c "from modelscope import snapshot_download; \\
        snapshot_download('iic/speech_seaco_paraformer_large_asrnat-zh-cn-16k-common-vocab8404-pytorch', \\
        cache_dir='~/.cache/modelscope')"
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
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


# Known FunASR model identifiers (ModelScope namespace `iic/...`).
# Extend as the upstream registry grows. The `has_word_timestamps` flag
# controls whether we trust per-word timing from the model or fall back
# to character-distribution.
_KNOWN_MODELS: dict[str, dict[str, Any]] = {
    "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch": {
        "label": "paraformer-zh",
        "has_word_timestamps": False,
        "languages": ["zh"],
    },
    "iic/speech_seaco_paraformer_large_asrnat-zh-cn-16k-common-vocab8404-pytorch": {
        "label": "seaco-paraformer-large",
        "has_word_timestamps": True,
        "languages": ["zh"],
    },
    "iic/speech_paraformer-large-vad-punc_asr_nat-zh-cn-16k-common-vocab8404-pytorch": {
        "label": "paraformer-large-vad-punc",
        "has_word_timestamps": False,
        "languages": ["zh"],
    },
    "iic/SenseVoiceSmall": {
        "label": "SenseVoiceSmall",
        "has_word_timestamps": False,
        "languages": ["zh", "en", "yue", "ja", "ko"],
    },
}

_DEFAULT_MODEL = "iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch"


class FunASRTranscriber(BaseTool):
    name = "funasr_transcriber"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "funasr"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["python:funasr", "python:modelscope", "python:torch"]
    install_instructions = (
        "pip install funasr modelscope torch\n"
        "# First run downloads the model from ModelScope (~400MB for paraformer-zh)\n"
        "# into ~/.cache/modelscope/. To pre-cache for offline use:\n"
        "python -c \"from modelscope import snapshot_download; "
        "snapshot_download('iic/speech_paraformer-large_asr_nat-zh-cn-16k-common-vocab8404-pytorch')\""
    )
    agent_skills = ["speech-to-text"]

    capabilities = [
        "transcribe",
        "language_detection",
        "chinese_optimized",
        "vad_punctuation",  # built-in VAD + punctuation
    ]

    input_schema = {
        "type": "object",
        "required": ["input_path"],
        "properties": {
            "input_path": {
                "type": "string",
                "description": "Path to audio or video file (16kHz preferred; FunASR auto-resamples)",
            },
            "model_size": {
                "type": "string",
                "enum": list(_KNOWN_MODELS.keys()),
                "default": _DEFAULT_MODEL,
                "description": (
                    "ModelScope model id. Default paraformer-zh is the "
                    "best speed/quality tradeoff for Mandarin. Use "
                    "speech_seaco_paraformer_large_asrnat for word-level "
                    "timestamps."
                ),
            },
            "language": {
                "type": "string",
                "enum": ["zh", "zh-cn", "en", "yue", "ja", "ko", "auto"],
                "default": "zh",
                "description": (
                    "ISO 639-1 or BCP-47 code. 'zh' defaults to Simplified "
                    "Mandarin (the model's training distribution). 'auto' "
                    "delegates to FunASR's auto-detect."
                ),
            },
            "use_vad": {
                "type": "boolean",
                "default": True,
                "description": "Voice Activity Detection pre-pass — skips silence, more accurate sentence boundaries.",
            },
            "use_punctuation": {
                "type": "boolean",
                "default": True,
                "description": "Add Chinese punctuation (，。！？) — improves downstream translation grouping.",
            },
            "output_dir": {
                "type": "string",
                "description": "Directory for the transcript JSON; defaults to the input file's parent.",
            },
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "segments": {"type": "array"},
            "language": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2, ram_mb=2048, vram_mb=0, disk_mb=800, network_required=False
    )
    idempotency_key_fields = ["input_path", "model_size", "language"]
    side_effects = ["writes transcript JSON to output_dir"]
    fallback = None
    user_visible_verification = [
        "Spot-check transcribed text against source audio",
        "Verify sentence boundaries land on natural pauses (VAD active)",
        "If word timestamps needed, ensure model_size is speech_seaco_paraformer_large_asrnat",
    ]

    # funasr/modelscope are heavy to import; do it lazily and once.
    _model = None
    _loaded_for: Optional[str] = None
    _lock = threading.Lock()

    def get_status(self) -> ToolStatus:
        try:
            import funasr  # noqa: F401
            import modelscope  # noqa: F401
            import torch  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE

        # Check the default model cache. funasr uses ~/.cache/modelscope.
        try:
            from modelscope import snapshot_download

            try:
                # `local_files_only=True` raises if the cache is missing —
                # exactly what we want for offline-first deployments.
                snapshot_download(
                    _DEFAULT_MODEL,
                    local_files_only=True,
                    cache_dir=os.environ.get("MODELSCOPE_CACHE"),
                )
                return ToolStatus.AVAILABLE
            except (OSError, ValueError, RuntimeError):
                return ToolStatus.DEGRADED  # will download on first execute
        except Exception:
            return ToolStatus.UNAVAILABLE

    def _ensure_loaded(self, model_size: str) -> None:
        if FunASRTranscriber._loaded_for == model_size:
            return
        with FunASRTranscriber._lock:
            if FunASRTranscriber._loaded_for == model_size:
                return

            from funasr import AutoModel

            model_kwargs: dict[str, Any] = {
                "model": model_size,
                "disable_update": True,
            }
            # VAD + punctuation are independent sub-models in FunASR —
            # only attach them when the user wants them, to keep first-
            # load latency down.
            if getattr(self, "_use_vad", True):
                model_kwargs["vad_model"] = "iic/speech_fsmn_vad_zh-cn-16k-common-pytorch"
            if getattr(self, "_use_punc", True):
                model_kwargs["punc_model"] = "iic/punc_ct-transformer_zh-cn-common-vocab272727-pytorch"

            FunASRTranscriber._model = AutoModel(**model_kwargs)
            FunASRTranscriber._loaded_for = model_size

    @staticmethod
    def _distribute_words(sentence: str, start: float, end: float) -> list[dict]:
        """Evenly distribute characters across [start, end] as word timestamps.

        Used as a fallback when the chosen model does not emit per-word
        timestamps. CJK characters are tokenized individually (so per-char
        karaoke-style highlighting works downstream); ASCII runs are
        grouped because spaces separate English words. Whitespace and
        punctuation are dropped.

        Timing is linear by character count — first word starts at `start`,
        last word's end snaps to `end`. Good enough for `subtitle_gen`'s
        cue grouping; NOT accurate enough for per-word karaoke on the
        source audio (use `speech_seaco_paraformer_large_asrnat` for that).
        """
        words: list[dict] = []
        if not sentence or end <= start:
            return words

        # Pre-segment: tokenize into "words" where:
        #   - each CJK ideograph is its own token (per-char timestamps for
        #     Chinese karaoke / subtitle line-break grouping)
        #   - ASCII letters/digits/apostrophes/hyphens form a single run
        #     (English words stay whole)
        #   - whitespace and punctuation are dropped
        import re as __re

        tokens = [
            m.group(0)
            for m in __re.finditer(r"[㐀-鿿]|[A-Za-z0-9'\-]+", sentence)
        ]
        if not tokens:
            return words

        total_chars = sum(len(t) for t in tokens)
        if total_chars == 0:
            return words

        # Linear distribution by character count. First word starts at
        # `start`; last word ends at `end`.
        duration = end - start
        cursor = start
        for i, tok in enumerate(tokens):
            char_dur = duration * (len(tok) / total_chars)
            word_start = cursor
            word_end = cursor + char_dur
            words.append({
                "word": tok,
                "start": round(word_start, 3),
                "end": round(word_end, 3),
            })
            cursor = word_end

        # Snap the last word's end to the actual sentence end so the
        # fallback timestamps don't visibly drift.
        if words:
            words[-1]["end"] = round(end, 3)
        return words

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        # Validate required inputs explicitly so a missing `input_path`
        # surfaces a clean `ToolResult(success=False, error=...)` rather
        # than a `KeyError`. Other tools in this codebase follow the same
        # pattern (e.g., `nllb_translator` rejects missing `segments`).
        if "input_path" not in inputs:
            return ToolResult(
                success=False,
                error="input_path is required",
            )
        input_path = Path(inputs["input_path"])
        model_size = inputs.get("model_size", _DEFAULT_MODEL)
        language = inputs.get("language", "zh")
        output_dir = Path(inputs.get("output_dir", input_path.parent))

        if not input_path.exists():
            return ToolResult(
                success=False, error=f"Input file not found: {input_path}"
            )

        # Stash VAD/punc preferences on self so _ensure_loaded can read
        # them. The AutoModel call happens there.
        self._use_vad = bool(inputs.get("use_vad", True))
        self._use_punc = bool(inputs.get("use_punctuation", True))

        output_dir.mkdir(parents=True, exist_ok=True)

        try:
            self._ensure_loaded(model_size)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False,
                error=f"FunASR model load failed: {exc}. Run the install_hint in install_instructions to download the model.",
            )

        assert FunASRTranscriber._model is not None
        has_word_ts = _KNOWN_MODELS.get(model_size, {}).get("has_word_timestamps", False)

        start = time.time()
        try:
            # FunASR's AutoModel.generate returns a list of result dicts;
            # each contains `text` and (when the model supports it)
            # `timestamp` (word-level) or `sentence_info` (sentence-level
            # with start/end ms).
            results = FunASRTranscriber._model.generate(
                input=str(input_path),
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                success=False, error=f"FunASR inference failed: {exc}"
            )

        segments: list[dict] = []
        for sent_idx, res in enumerate(results):
            text = (res.get("text") or "").strip()
            if not text:
                continue

            # Sentence-level timestamps come back as [[start_ms, end_ms], ...]
            # aligned with the model's sentence_info; if not present, treat
            # the whole audio as one segment.
            sent_ts = res.get("timestamp") or []
            word_ts = res.get("word_timestamp") or []

            if sent_ts and len(sent_ts) >= 1:
                for s_idx, ts in enumerate(sent_ts):
                    s_start = ts[0] / 1000.0 if ts[0] > 1 else float(ts[0])
                    s_end = ts[1] / 1000.0 if ts[1] > 1 else float(ts[1])
                    # FunASR returns word-level timestamps as a flat list
                    # across the whole audio; for sentence s_idx we slice
                    # the slice between this sentence's start and end.
                    if has_word_ts and word_ts:
                        sliced = [
                            w for w in word_ts
                            if s_start <= (w[0] / 1000.0 if w[0] > 1 else w[0]) <= s_end
                        ]
                        words = [
                            {
                                "word": w[2] if len(w) > 2 else "",
                                "start": round(w[0] / 1000.0 if w[0] > 1 else float(w[0]), 3),
                                "end": round(w[1] / 1000.0 if w[1] > 1 else float(w[1]), 3),
                            }
                            for w in sliced
                            if len(w) >= 3
                        ]
                    else:
                        # Fallback: even-distribute characters across the
                        # sentence. Subtitle cue rendering groups these
                        # back into lines, so the imprecision is invisible.
                        words = self._distribute_words(text, s_start, s_end)
                    segments.append({
                        "id": len(segments),
                        "start": round(s_start, 3),
                        "end": round(s_end, 3),
                        "text": text,
                        "words": words,
                    })
            else:
                # Whole-audio single-segment fallback
                segments.append({
                    "id": 0,
                    "start": 0.0,
                    "end": 0.0,  # unknown; consumers should rely on `duration_seconds`
                    "text": text,
                    "words": [],
                })

        # Compute total duration from the last segment's end; if empty,
        # fall back to the model's reported duration if available.
        if segments and segments[-1]["end"] > 0:
            duration = segments[-1]["end"]
        elif results and isinstance(results[0].get("duration"), (int, float)):
            duration = float(results[0]["duration"])
        else:
            duration = 0.0

        result_data = {
            "segments": segments,
            "language": language,
            "duration_seconds": round(duration, 3),
            "model_size": model_size,
            "has_word_timestamps": has_word_ts,
        }

        output_path = output_dir / f"{input_path.stem}_funasr_transcript.json"
        output_path.write_text(json.dumps(result_data, indent=2), encoding="utf-8")

        return ToolResult(
            success=True,
            data=result_data,
            artifacts=[str(output_path)],
            duration_seconds=round(time.time() - start, 2),
        )