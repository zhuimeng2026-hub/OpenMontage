"""Kokoro-82M local text-to-speech provider tool."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


HF_REPO = "hexgrad/Kokoro-82M"
DEFAULT_VOICE = "zf_xiaobei"  # Mandarin female; this project narrates in Chinese
SAMPLE_RATE = 24000

# Kokoro voice names are `<lang><gender>_<name>`, so the language code the
# pipeline needs is simply the first character of the voice name.
LANG_NAMES = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Brazilian Portuguese",
    "z": "Mandarin Chinese",
}


def _hf_cache_root() -> Path:
    """HuggingFace hub cache dir, honouring the standard env overrides."""
    if hub := os.environ.get("HF_HUB_CACHE"):
        return Path(hub).expanduser()
    if home := os.environ.get("HF_HOME"):
        return Path(home).expanduser() / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _weights_cached() -> bool:
    """True once the Kokoro weights have been fetched at least once.

    The tool advertises `offline: True` / `network_required=False`, which is
    only honest after the one-time bootstrap download. Reporting AVAILABLE
    before that would repeat the `piper_tts` false positive: status green,
    then a mid-pipeline failure on a host that cannot reach huggingface.co.
    """
    repo_dir = _hf_cache_root() / f"models--{HF_REPO.replace('/', '--')}"
    return any(repo_dir.glob("snapshots/*/voices/*.pt"))


class KokoroTTS(BaseTool):
    name = "kokoro_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "kokoro"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:kokoro", "python:soundfile"]
    install_instructions = (
        "Install Kokoro TTS:\n"
        "  pip install kokoro soundfile\n"
        "  pip install 'misaki[zh]'    # required for Mandarin (lang_code 'z')\n"
        "Then bootstrap the weights once (~330 MB, all voices included):\n"
        f"  python -c \"from kokoro import KPipeline; KPipeline(lang_code='z')\"\n"
        "The bootstrap needs huggingface.co; export HTTPS_PROXY first if this\n"
        "host reaches it only through a proxy. Generation is offline afterwards."
    )
    fallback = "edge_tts"
    fallback_tools = ["edge_tts", "piper_tts"]
    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "offline_generation",
        "multilingual_generation",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": True,
        "native_audio": True,
    }
    best_for = [
        "highest-quality local narration without an API key",
        "privacy-sensitive workflows that must stay on-prem",
        "Chinese and English narration at 24 kHz",
    ]
    not_good_for = [
        "voice clone matching",
        "languages outside the nine Kokoro supports",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "voice": {
                "type": "string",
                "default": DEFAULT_VOICE,
                "description": "Kokoro voice, e.g. zf_xiaobei (zh) or af_heart (en)",
            },
            "lang_code": {
                "type": "string",
                "description": "Override the language code; defaults to the voice's first character",
            },
            "speed": {"type": "number", "default": 1.0},
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=2048, vram_mb=0, disk_mb=400, network_required=False
    )
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=[])
    idempotency_key_fields = ["text", "voice", "lang_code", "speed"]
    side_effects = ["writes audio file to output_path"]
    user_visible_verification = ["Listen to generated audio for natural prosody"]

    # Building a KPipeline loads the 82M model, which costs seconds. Production
    # runs synthesize one line per scene, so keep one pipeline per language for
    # the life of the process.
    _pipelines: dict[str, Any] = {}

    def get_status(self) -> ToolStatus:
        try:
            import kokoro  # noqa: F401
            import soundfile  # noqa: F401
        except ImportError:
            return ToolStatus.UNAVAILABLE

        return ToolStatus.AVAILABLE if _weights_cached() else ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(success=False, error="Kokoro TTS not available. " + self.install_instructions)

        start = time.time()
        try:
            result = self._generate(inputs)
        except Exception as exc:
            return ToolResult(success=False, error=f"Kokoro TTS generation failed: {exc}")

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _get_pipeline(self, lang_code: str):
        if lang_code not in self._pipelines:
            from kokoro import KPipeline

            try:
                self._pipelines[lang_code] = KPipeline(lang_code=lang_code)
            except ModuleNotFoundError as exc:
                # misaki ships its per-language G2P backends as extras, so a
                # working English install still fails on Mandarin.
                raise RuntimeError(
                    f"Kokoro language {lang_code!r} "
                    f"({LANG_NAMES.get(lang_code, 'unknown')}) needs an extra "
                    f"misaki backend: pip install 'misaki[{lang_code}]' "
                    f"(missing {exc.name!r})"
                ) from exc
        return self._pipelines[lang_code]

    def _generate(self, inputs: dict[str, Any]) -> ToolResult:
        import numpy as np
        import soundfile as sf

        text = inputs["text"]
        voice = inputs.get("voice", DEFAULT_VOICE)
        lang_code = inputs.get("lang_code") or voice[0]
        speed = inputs.get("speed", 1.0)
        output_path = Path(inputs.get("output_path", "tts_output.wav"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        pipeline = self._get_pipeline(lang_code)

        # KPipeline splits long text and yields one chunk per segment. Taking
        # only the first chunk silently truncates multi-sentence narration, so
        # collect them all.
        chunks = []
        for _graphemes, _phonemes, audio in pipeline(text, voice=voice, speed=speed):
            chunks.append(audio.numpy() if hasattr(audio, "numpy") else np.asarray(audio))

        if not chunks:
            return ToolResult(success=False, error=f"Kokoro produced no audio for {len(text)} chars of text")

        sf.write(str(output_path), np.concatenate(chunks), SAMPLE_RATE)

        if not output_path.exists():
            return ToolResult(success=False, error=f"Kokoro output file missing: {output_path}")

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "voice": voice,
                "lang_code": lang_code,
                "language": LANG_NAMES.get(lang_code, "unknown"),
                "speed": speed,
                "text_length": len(text),
                "chunks": len(chunks),
                "output": str(output_path),
                "format": "wav",
                "sample_rate": SAMPLE_RATE,
            },
            artifacts=[str(output_path)],
            model=f"{HF_REPO}/{voice}",
        )
