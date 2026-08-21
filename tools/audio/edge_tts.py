"""Microsoft Edge TTS provider tool — free, high-quality multilingual synthesis."""

from __future__ import annotations

import asyncio
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


class EdgeTTS(BaseTool):
    name = "edge_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "edge_tts"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = ["pip:edge-tts"]
    install_instructions = "pip install edge-tts"
    fallback = "piper_tts"
    fallback_tools = ["piper_tts", "google_tts", "openai_tts"]
    agent_skills = ["text-to-speech"]

    capabilities = [
        "text_to_speech",
        "voice_selection",
        "multilingual",
        "ssml_support",
    ]
    supports = {
        "voice_cloning": False,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
        "ssml": True,
    }
    best_for = [
        "free high-quality Chinese TTS (zh-CN-XiaoxiaoNeural, zh-CN-YunxiNeural, etc.)",
        "multilingual narration at zero cost",
        "natural-sounding Chinese voiceover",
    ]
    not_good_for = [
        "fully offline production",
        "voice cloning",
    ]

    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string", "description": "Text to convert to speech"},
            "voice": {
                "type": "string",
                "default": "zh-CN-XiaoxiaoNeural",
                "description": (
                    "Voice name. Default is XiaoxiaoNeural because the upstream "
                    "default 'zh-CN-YunxiNeural' is rate-limited / IP-blocked on "
                    "many hosts (returns NoAudioReceived). Other Chinese voices: "
                    "zh-CN-YunjianNeural (male, passionate), zh-CN-YunyangNeural "
                    "(male, news), zh-CN-XiaoyiNeural (female, lively). "
                    "English: en-US-AndrewNeural, en-US-AvaNeural."
                ),
            },
            "rate": {
                "type": "string",
                "default": "+0%",
                "description": "Speaking rate adjustment, e.g. '+20%', '-10%'",
            },
            "volume": {
                "type": "string",
                "default": "+0%",
                "description": "Volume adjustment, e.g. '+50%', '-20%'",
            },
            "pitch": {
                "type": "string",
                "default": "+0Hz",
                "description": "Pitch adjustment, e.g. '+5Hz', '-3Hz'",
            },
            "output_path": {"type": "string"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["timeout"])
    idempotency_key_fields = ["text", "voice", "rate", "volume", "pitch"]
    side_effects = ["writes audio file to output_path", "calls Microsoft Edge TTS service"]
    user_visible_verification = ["Listen to generated audio for natural speech quality"]

    def get_status(self) -> ToolStatus:
        try:
            import edge_tts  # noqa: F401
            return ToolStatus.AVAILABLE
        except ImportError:
            return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return 0.0

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(success=False, error="edge-tts not installed. " + self.install_instructions)

        start = time.time()
        try:
            result = self._generate(inputs)
        except Exception as exc:
            return ToolResult(success=False, error=f"Edge TTS failed: {exc}")

        result.duration_seconds = round(time.time() - start, 2)
        return result

    def _generate(self, inputs: dict[str, Any]) -> ToolResult:
        import edge_tts

        text = inputs["text"]
        voice = inputs.get("voice", "zh-CN-YunxiNeural")
        rate = inputs.get("rate", "+0%")
        volume = inputs.get("volume", "+0%")
        pitch = inputs.get("pitch", "+0Hz")
        output_path = Path(inputs.get("output_path", "tts_output.mp3"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        async def _synth():
            communicate = edge_tts.Communicate(text, voice, rate=rate, volume=volume, pitch=pitch)
            await communicate.save(str(output_path))

        asyncio.run(_synth())

        if not output_path.exists():
            return ToolResult(success=False, error=f"Edge TTS output file missing: {output_path}")

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "voice": voice,
                "text_length": len(text),
                "output": str(output_path),
                "format": "mp3",
                "rate": rate,
                "volume": volume,
                "pitch": pitch,
            },
            artifacts=[str(output_path)],
            model=f"edge-tts/{voice}",
        )
