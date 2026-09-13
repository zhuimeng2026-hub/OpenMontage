"""Transcription tool wrapping faster-whisper / WhisperX.

Provides speech-to-text with word-level timestamps and optional speaker
diarization. Falls back gracefully when GPU or diarization dependencies
are not available.

Offline behaviour: faster-whisper's `WhisperModel(...)` resolves model
metadata against huggingface.co on every load, which breaks on hosts that
cannot reach the hub. We mirror the NLLB translator's pattern: resolve the
requested model to a local snapshot directory and pass that absolute path
to `WhisperModel`. faster-whisper's `os.path.isdir` branch
(`transcribe.py:678-681`) then bypasses the hub entirely.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ResumeSupport,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
)


# ---------------------------------------------------------------------------
# Offline model resolution
# ---------------------------------------------------------------------------

DEFAULT_MODEL_SIZE = "base"
DEFAULT_MODEL_REPO = "Systran/faster-whisper-base"

# Size alias → HF repo id. Kept local rather than reaching into faster_whisper
# internals. Values match `faster_whisper/utils.py:_MODELS`; ours never touches
# the hub, faster-whisper's own resolution would.
_MODELS: dict[str, str] = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "medium": "Systran/faster-whisper-medium",
    "large": "Systran/faster-whisper-large-v3",
    "large-v2": "Systran/faster-whisper-large-v2",
    "large-v3": "Systran/faster-whisper-large-v3",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}

# Authoritative list of files that make a faster-whisper snapshot usable.
# Source: faster_whisper/utils.py:91-97 (`allow_patterns`).
_USABLE_FILES = ("model.bin", "config.json", "tokenizer.json")


def _resolve_cache_root(inputs: dict[str, Any] | None = None) -> Path:
    """HuggingFace hub cache directory.

    Precedence: input['model_dir'] > FASTER_WHISPER_MODEL_DIR env >
    HF_HUB_CACHE env > HF_HOME/hub > ~/.cache/huggingface/hub. The
    tool-scoped env takes precedence over the generic HF vars so a caller
    can pin the transcriber's cache without affecting the rest of the
    toolchain (mirrors piper_tts's `PIPER_DATA_DIR`).
    """
    raw = (inputs or {}).get("model_dir") or os.environ.get("FASTER_WHISPER_MODEL_DIR")
    if raw:
        return Path(raw).expanduser()
    if hub := os.environ.get("HF_HUB_CACHE"):
        return Path(hub).expanduser()
    if home := os.environ.get("HF_HOME"):
        return Path(home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _resolve_repo(size_or_repo: str) -> str:
    """Map a faster-whisper size alias to its HF repo id.

    Bare sizes (`base`, `large-v3`, ...) are looked up in `_MODELS`.
    Anything containing a `/` is treated as a raw repo id
    (`Systran/faster-distil-whisper-large-v3`). Unknown bare sizes raise
    ValueError with the supported set spelled out.
    """
    if "/" in size_or_repo:
        return size_or_repo
    try:
        return _MODELS[size_or_repo]
    except KeyError as exc:
        raise ValueError(
            f"Unknown faster-whisper model size {size_or_repo!r}. "
            f"Known sizes: {sorted(_MODELS)}. Or pass a repo id ('owner/name')."
        ) from exc


def _snapshot_path(cache_root: Path, repo: str) -> Optional[Path]:
    """Return the absolute path to a usable cached snapshot of `repo`, or None.

    Mirrors `nllb_translator._resolve_local_snapshot`: prefer the snapshot
    pointed at by `refs/main`, fall back to the newest non-empty snapshot.
    A snapshot counts as usable only when every entry in `_USABLE_FILES`
    resolves (authoritative list per faster_whisper/utils.py:91-97).
    """
    repo_dir = cache_root / f"models--{repo.replace('/', '--')}"
    snapshots_dir = repo_dir / "snapshots"
    if not snapshots_dir.is_dir():
        return None

    refs_file = repo_dir / "refs" / "main"
    if refs_file.exists():
        sha = refs_file.read_text().strip()
        candidate = snapshots_dir / sha
        if candidate.is_dir() and all((candidate / f).is_file() for f in _USABLE_FILES):
            return candidate.resolve()

    candidates = sorted(
        (p for p in snapshots_dir.iterdir() if p.is_dir()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for cand in candidates:
        if all((cand / f).is_file() for f in _USABLE_FILES):
            return cand.resolve()
    return None


def _is_local_path(value: str) -> bool:
    """True if `value` is an existing directory faster-whisper can use directly."""
    return Path(value).expanduser().is_dir()


class Transcriber(BaseTool):
    name = "transcriber"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "analysis"
    provider = "whisperx"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["python:faster_whisper"]
    install_instructions = (
        "pip install faster-whisper  # CPU mode\n"
        "pip install faster-whisper[gpu]  # GPU mode (requires CUDA)\n"
        "pip install whisperx  # For diarization support\n"
        "# Pre-cache the default model (~150MB, one-time, needs huggingface.co):\n"
        f"python -c \"from huggingface_hub import snapshot_download; "
        f"snapshot_download('{DEFAULT_MODEL_REPO}', "
        f"allow_patterns=['*.bin','*.json','tokenizer.*'])\"\n"
        "# Override the cache root with FASTER_WHISPER_MODEL_DIR (tool-scoped)\n"
        "# or the standard HF_HUB_CACHE / HF_HOME env vars. export HTTPS_PROXY\n"
        "# first if this host reaches huggingface.co only through a proxy."
    )
    agent_skills = ["speech-to-text"]

    capabilities = [
        "transcribe",
        "word_timestamps",
        "diarization",
        "language_detection",
    ]

    input_schema = {
        "type": "object",
        "required": ["input_path"],
        "properties": {
            "input_path": {"type": "string", "description": "Path to audio or video file"},
            "model_size": {
                "type": "string",
                "enum": [
                    "tiny", "base", "small", "medium",
                    "large", "large-v2", "large-v3", "turbo",
                ],
                "default": DEFAULT_MODEL_SIZE,
                "description": (
                    "faster-whisper size alias or any 'owner/name' repo id. "
                    "Each option must be pre-cached locally before use — "
                    "transcription never fetches from huggingface.co."
                ),
            },
            "model_dir": {
                "type": "string",
                "description": (
                    "HF cache root to read models from. Overrides the "
                    "FASTER_WHISPER_MODEL_DIR / HF_HUB_CACHE / HF_HOME env vars."
                ),
            },
            "language": {"type": "string", "description": "ISO 639-1 language code, or null for auto-detect"},
            "diarize": {"type": "boolean", "default": False},
            "output_dir": {"type": "string", "description": "Directory for output files"},
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "segments": {"type": "array"},
            "word_timestamps": {"type": "array"},
            "language": {"type": "string"},
            "duration_seconds": {"type": "number"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=2,
        ram_mb=2048,
        vram_mb=0,  # CPU by default; GPU optional
        disk_mb=500,
        network_required=False,
    )

    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["MemoryError"])
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["input_path", "model_size", "language"]
    side_effects = ["writes transcript JSON to output_dir"]
    fallback = None
    user_visible_verification = [
        "Check transcript text against source audio",
        "Verify word timestamps align with speech",
    ]

    def get_status(self) -> ToolStatus:
        try:
            import faster_whisper  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE

        # Package importable, but is the default model actually on disk?
        # The original check stopped here and returned AVAILABLE, which is
        # the piper_tts / kokoro_tts false positive: green status, then
        # mid-pipeline failure on hosts that cannot reach huggingface.co.
        # DEGRADED keeps the tool discoverable while the provider menu's
        # setup-offer path surfaces `install_instructions` to the user.
        cache_root = _resolve_cache_root()
        snapshot = _snapshot_path(cache_root, DEFAULT_MODEL_REPO)
        return ToolStatus.AVAILABLE if snapshot is not None else ToolStatus.DEGRADED

    def _has_diarization(self) -> bool:
        try:
            import whisperx  # noqa: F401
            return True
        except ImportError:
            return False

    def estimate_runtime(self, inputs: dict[str, Any]) -> float:
        """Rough estimate: ~0.5x real-time on CPU for 'base' model."""
        return 60.0  # conservative default

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        input_path = Path(inputs["input_path"])
        model_size = inputs.get("model_size", DEFAULT_MODEL_SIZE)
        language = inputs.get("language")
        diarize = inputs.get("diarize", False)
        output_dir = Path(inputs.get("output_dir", input_path.parent))

        if not input_path.exists():
            return ToolResult(success=False, error=f"Input file not found: {input_path}")

        output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve the requested model to a local snapshot path *before*
        # instantiating faster-whisper. faster-whisper's
        # `WhisperModel(<abs path>)` short-circuits the hub entirely when
        # the argument is a directory, so no HEAD against huggingface.co/api
        # happens even on offline hosts. Unknown sizes and missing caches
        # surface here as actionable errors with the pre-cache command,
        # rather than as stack traces from a mid-load network failure.
        cache_root = _resolve_cache_root(inputs)
        if _is_local_path(model_size):
            model_path: str = str(Path(model_size).expanduser().resolve())
        else:
            try:
                repo = _resolve_repo(model_size)
            except ValueError as exc:
                return ToolResult(success=False, error=str(exc))
            snapshot = _snapshot_path(cache_root, repo)
            if snapshot is None:
                return ToolResult(
                    success=False,
                    error=(
                        f"faster-whisper model {model_size!r} not found in HF "
                        f"cache ({cache_root}). To pre-cache it once on a "
                        f"host with network access (export HTTPS_PROXY first "
                        f"if required):\n"
                        f"  python -c \"from huggingface_hub import "
                        f"snapshot_download; snapshot_download('{repo}', "
                        f"allow_patterns=['*.bin','*.json','tokenizer.*'])\""
                    ),
                )
            model_path = str(snapshot)

        try:
            from faster_whisper import WhisperModel
        except ImportError:
            return ToolResult(
                success=False,
                error="faster-whisper is not installed. Run: pip install faster-whisper",
            )

        start = time.time()

        # Load model (CPU by default, CUDA if available)
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
            compute_type = "float16" if device == "cuda" else "int8"
        except ImportError:
            device = "cpu"
            compute_type = "int8"

        model = WhisperModel(model_path, device=device, compute_type=compute_type)

        # Transcribe
        segments_iter, info = model.transcribe(
            str(input_path),
            language=language,
            word_timestamps=True,
            vad_filter=True,
        )

        segments = []
        word_timestamps = []

        for seg in segments_iter:
            seg_data = {
                "id": seg.id,
                "start": round(seg.start, 3),
                "end": round(seg.end, 3),
                "text": seg.text.strip(),
            }

            if seg.words:
                words = []
                for w in seg.words:
                    word_entry = {
                        "word": w.word,
                        "start": round(w.start, 3),
                        "end": round(w.end, 3),
                        "probability": round(w.probability, 3),
                    }
                    words.append(word_entry)
                    word_timestamps.append(word_entry)
                seg_data["words"] = words

            segments.append(seg_data)

        detected_language = language or info.language
        duration = info.duration

        # Optional diarization pass
        if diarize and self._has_diarization():
            segments = self._apply_diarization(
                str(input_path), segments, detected_language
            )

        elapsed = time.time() - start

        result_data = {
            "segments": segments,
            "word_timestamps": word_timestamps,
            "language": detected_language,
            "duration_seconds": round(duration, 3),
            "model_size": model_size,
            "device": device,
        }

        # Write transcript JSON
        output_path = output_dir / f"{input_path.stem}_transcript.json"
        output_path.write_text(json.dumps(result_data, indent=2), encoding="utf-8")

        return ToolResult(
            success=True,
            data=result_data,
            artifacts=[str(output_path)],
            duration_seconds=round(elapsed, 2),
        )

    def _apply_diarization(
        self,
        audio_path: str,
        segments: list[dict],
        language: str,
    ) -> list[dict]:
        """Apply WhisperX diarization to assign speaker labels."""
        try:
            import whisperx

            # Load audio for alignment
            audio = whisperx.load_audio(audio_path)

            # Align segments with word timestamps
            align_model, align_metadata = whisperx.load_align_model(
                language_code=language, device="cpu"
            )
            aligned = whisperx.align(
                segments, align_model, align_metadata, audio, device="cpu"
            )

            # Diarize
            import os
            hf_token = os.environ.get("HF_TOKEN")
            if not hf_token:
                # Can't diarize without HuggingFace token for pyannote
                return segments

            diarize_model = whisperx.DiarizationPipeline(
                use_auth_token=hf_token, device="cpu"
            )
            diarize_segments = diarize_model(audio)
            result = whisperx.assign_word_speakers(diarize_segments, aligned)

            return result.get("segments", segments)
        except Exception:
            # Diarization is best-effort; return original segments on failure
            return segments
