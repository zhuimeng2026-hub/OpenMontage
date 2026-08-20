"""Voicebox internal voice-cloning tool."""

from __future__ import annotations

import time
from typing import Any

from lib.voicebox_client import VoiceboxClient, VoiceboxClientError
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


class VoiceboxVoiceClone(BaseTool):
    name = "voicebox_voice_clone"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "voice_cloning"
    provider = "voicebox"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.ASYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API
    dependencies: list[str] = []
    install_instructions = "Set VOICEBOX_BASE_URL and VOICEBOX_TOKEN for the internal Voicebox service."
    agent_skills = ["text-to-speech"]

    capabilities = ["voice_cloning", "voice_selection", "consent_tracking"]
    supports = {
        "voice_cloning": True,
        "multilingual": True,
        "offline": False,
        "native_audio": False,
        "async_tasks": True,
    }
    best_for = ["internal Voicebox voice cloning", "user-consented private voice samples"]
    not_good_for = ["public direct Voicebox access", "cloning without explicit consent"]
    input_schema = {
        "type": "object",
        "required": ["sample_path", "consent"],
        "properties": {
            "sample_path": {"type": "string", "description": "Local path or registered asset id."},
            "voice_id": {"type": "string", "description": "Optional requested/private voice id."},
            "consent": {"type": "boolean", "const": True},
            "consent_at": {"type": "string"},
            "user_id": {"type": "string"},
            "sample_sha256": {"type": "string"},
        },
    }
    output_schema = {
        "type": "object",
        "required": ["voice_id", "provider_voice_id"],
        "properties": {
            "voice_id": {"type": ["string", "null"]},
            "provider_voice_id": {"type": ["string", "null"]},
            "task_id": {"type": ["string", "null"]},
            "status": {"type": ["string", "null"]},
        },
    }
    resource_profile = ResourceProfile(cpu_cores=1, ram_mb=256, disk_mb=50, network_required=True)
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["timeout", "temporarily unavailable"])
    idempotency_key_fields = ["sample_path", "voice_id", "sample_sha256", "user_id"]
    side_effects = ["submits a voice-cloning task to internal Voicebox"]
    user_visible_verification = ["Verify consent and listen to a generated sample before production use"]

    def get_status(self) -> ToolStatus:
        return ToolStatus.AVAILABLE if VoiceboxClient().configured else ToolStatus.UNAVAILABLE

    def health(self) -> dict[str, Any]:
        """Expose an explicit health operation for diagnostics and preflight."""

        return VoiceboxClient().health()

    def health_check(self) -> dict[str, Any]:
        return self.health()

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()
        sample_path = inputs.get("sample_path") or inputs.get("sample")
        consent = inputs.get("consent")
        if not sample_path:
            return ToolResult(success=False, error="Voicebox voice clone requires sample_path")
        if consent is not True:
            return ToolResult(success=False, error="Explicit consent=true is required for voice cloning")

        try:
            payload = VoiceboxClient().clone_voice(
                sample_path=str(sample_path),
                voice_id=inputs.get("voice_id") or inputs.get("requested_voice_id"),
                consent=True,
                consent_at=inputs.get("consent_at"),
                user_id=inputs.get("user_id"),
                sample_sha256=inputs.get("sample_sha256"),
            )
            data = self._normalize(payload)
            if not data.get("voice_id") and not data.get("task_id"):
                return ToolResult(success=False, error="Voicebox clone response missing voice_id")
            return ToolResult(
                success=True,
                data=data,
                cost_usd=0.0,
                duration_seconds=round(time.time() - start, 2),
                model=data.get("model"),
            )
        except VoiceboxClientError as exc:
            return ToolResult(success=False, error=f"Voicebox voice clone failed: {exc}", duration_seconds=round(time.time() - start, 2))
        except Exception:
            # Do not echo arbitrary adapter/server exceptions; they may carry
            # request headers or sensitive provider payloads.
            return ToolResult(success=False, error="Voicebox voice clone failed: internal service error", duration_seconds=round(time.time() - start, 2))

    @staticmethod
    def _normalize(payload: dict[str, Any]) -> dict[str, Any]:
        nested = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        merged = {**payload, **nested}
        provider_voice_id = merged.get("provider_voice_id") or merged.get("voice_id") or merged.get("id")
        voice_id = merged.get("voice_id") or provider_voice_id
        data = dict(merged)
        task_id = merged.get("task_id") or merged.get("job_id")
        data.update({
            "provider": "voicebox",
            "voice_id": voice_id,
            "provider_voice_id": provider_voice_id,
            "task_id": task_id,
            "job_id": task_id,
        })
        return data
