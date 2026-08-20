"""Voicebox internal text-to-speech tool."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from lib.voicebox_client import VoiceboxClient, VoiceboxClientError, decode_audio_base64
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


class VoiceboxTTS(BaseTool):
    name = "voicebox_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "voicebox"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies: list[str] = []
    install_instructions = "Set VOICEBOX_BASE_URL and VOICEBOX_TOKEN for the internal Voicebox service."
    agent_skills = ["text-to-speech"]

    capabilities = ["text_to_speech", "voice_selection", "timestamp_alignment", "multilingual"]
    supports = {
        "voice_cloning": True,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
        "timestamps": True,
    }
    best_for = ["TTS using private Voicebox voices", "subtitle-ready narration with timing segments"]
    not_good_for = ["public direct Voicebox access"]
    input_schema = {
        "type": "object",
        "required": ["text"],
        "properties": {
            "text": {"type": "string"},
            "voice_id": {"type": "string"},
            "output_path": {"type": "string"},
            "subtitle": {"type": "boolean", "default": True},
            "model": {"type": "string"},
            "speed": {"type": "number"},
            "language": {"type": "string"},
        },
    }
    output_schema = {
        "type": "object",
        "required": ["output", "segments"],
        "properties": {
            "output": {"type": ["string", "null"]},
            "audio_path": {"type": ["string", "null"]},
            "segments": {"type": "array"},
            "task_id": {"type": ["string", "null"]},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=100, network_required=True)
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout", "temporarily unavailable"])
    idempotency_key_fields = ["text", "voice_id", "model", "speed", "language", "subtitle"]
    side_effects = ["writes audio output when requested", "submits a TTS task to internal Voicebox"]
    user_visible_verification = ["Listen for intelligibility and verify returned subtitle timing segments"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if VoiceboxClient().configured else ToolStatus.UNAVAILABLE

    def health(self) -> dict[str, Any]:
        return VoiceboxClient().health()

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        text = inputs.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(success=False, error="Voicebox TTS requires non-empty text")
        output_path = inputs.get("output_path")
        try:
            payload = VoiceboxClient().tts(
                text=text,
                voice_id=inputs.get("voice_id") or inputs.get("voice"),
                output_path=output_path,
                subtitle=inputs.get("subtitle", True),
                model=inputs.get("model"),
                speed=inputs.get("speed"),
                language=inputs.get("language"),
            )
            data, artifacts = self._normalize(payload, output_path=output_path)
            return ToolResult(
                success=True,
                data=data,
                artifacts=artifacts,
                duration_seconds=round(time.time() - start, 2),
                model=data.get("model"),
            )
        except VoiceboxClientError as exc:
            return ToolResult(success=False, error=f"Voicebox TTS failed: {exc}", duration_seconds=round(time.time() - start, 2))
        except Exception:
            return ToolResult(success=False, error="Voicebox TTS failed: internal service error", duration_seconds=round(time.time() - start, 2))

    @staticmethod
    def _normalize(payload: dict[str, Any], *, output_path: str | None) -> tuple[dict[str, Any], list[str]]:
        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        merged = {**payload, **nested}
        requested = Path(output_path) if output_path else None
        artifacts: list[str] = []
        encoded = merged.get("audio_base64") or merged.get("audio_b64")
        if encoded and requested:
            decoded = decode_audio_base64(encoded)
            if decoded is not None:
                requested.parent.mkdir(parents=True, exist_ok=True)
                requested.write_bytes(decoded)
                artifacts.append(str(requested))

        audio_path = merged.get("audio_path") or merged.get("output") or merged.get("path")
        if not audio_path and requested and requested.exists():
            audio_path = str(requested)
        segments = merged.get("segments")
        if not isinstance(segments, list):
            segments = merged.get("subtitle_segments") or merged.get("timestamps") or []
        data = dict(merged)
        data.update({
            "provider": "voicebox",
            "audio_path": audio_path,
            "output": audio_path,
            "segments": segments,
        })
        if merged.get("task_id") or merged.get("job_id"):
            data["task_id"] = merged.get("task_id") or merged.get("job_id")
            data["job_id"] = data["task_id"]
        if output_path and audio_path is None:
            data["output_path"] = output_path
        return data, artifacts
