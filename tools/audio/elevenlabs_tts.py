"""ElevenLabs text-to-speech provider tool."""

from __future__ import annotations

import json
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


class ElevenLabsTTS(BaseTool):
    name = "elevenlabs_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "elevenlabs"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.API

    dependencies = []
    install_instructions = (
        "Set the ELEVENLABS_API_KEY environment variable:\n"
        "  export ELEVENLABS_API_KEY=your_key_here\n"
        "Get a key at https://elevenlabs.io"
    )
    fallback = "openai_tts"
    fallback_tools = ["openai_tts", "piper_tts"]
    agent_skills = ["elevenlabs", "text-to-speech"]

    capabilities = [
        "text_to_speech",
        "voice_selection",
        "ssml_support",
        "pronunciation_control",
        "voice_cloning",
        "list_cloned_voices",
    ]
    supports = {
        "voice_cloning": True,
        "multilingual": True,
        "offline": False,
        "native_audio": True,
    }
    best_for = [
        "high-quality narration",
        "voice-sensitive spokesperson videos",
        "multilingual spoken delivery",
        "instant voice cloning from short samples (1+ min recommended)",
    ]
    not_good_for = [
        "fully offline production",
        "privacy-constrained local-only workflows",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["text_to_speech", "clone_voice", "list_cloned_voices"],
                "description": (
                    "`text_to_speech` (default if `text` is provided): synthesize speech. "
                    "`clone_voice`: upload audio samples to ElevenLabs, returns a new "
                    "`voice_id` for use with `text_to_speech`. "
                    "`list_cloned_voices`: enumerate voices owned by this account."
                ),
            },
            "text": {"type": "string", "description": "Text to convert to speech (operation=text_to_speech)"},
            "voice_id": {
                "type": "string",
                "description": "ElevenLabs voice ID (default: Rachel) (operation=text_to_speech)",
            },
            "model_id": {
                "type": "string",
                "default": "eleven_multilingual_v2",
                "description": "TTS model to use",
            },
            "stability": {
                "type": "number",
                "default": 0.5,
                "minimum": 0,
                "maximum": 1,
            },
            "similarity_boost": {
                "type": "number",
                "default": 0.75,
                "minimum": 0,
                "maximum": 1,
            },
            "style": {
                "type": "number",
                "default": 0.0,
                "minimum": 0,
                "maximum": 1,
            },
            "speed": {
                "type": "number",
                "default": 1.0,
                "minimum": 0.7,
                "maximum": 1.2,
            },
            "use_speaker_boost": {
                "type": "boolean",
                "default": True,
            },
            "output_path": {"type": "string"},
            "output_format": {
                "type": "string",
                "default": "mp3_44100_128",
                "enum": ["mp3_44100_128", "mp3_44100_192", "pcm_16000", "pcm_24000"],
            },
            # ---- clone_voice ----
            "name": {
                "type": "string",
                "description": "Display name for the cloned voice (operation=clone_voice).",
            },
            "audio_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths to 1+ audio sample files (wav/mp3/m4a). "
                    "Recommended total duration >= 1 minute for quality cloning "
                    "(operation=clone_voice)."
                ),
            },
            "description": {
                "type": "string",
                "default": "",
                "description": "Optional description / notes for the cloned voice.",
            },
            "labels": {
                "type": "object",
                "description": (
                    "Optional labels JSON for the cloned voice "
                    "(e.g. {\"accent\": \"british\", \"age\": \"middle-aged\"}). "
                    "ElevenLabs uses these for downstream filtering."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=256, vram_mb=0, disk_mb=50, network_required=True
    )
    retry_policy = RetryPolicy(max_retries=2, retryable_errors=["rate_limit", "timeout"])
    idempotency_key_fields = [
        "operation",
        "text",
        "voice_id",
        "model_id",
        "stability",
        "similarity_boost",
        "style",
        "speed",
        "use_speaker_boost",
        "name",
        "audio_paths",
    ]
    side_effects = [
        "writes audio file to output_path (operation=text_to_speech)",
        "creates remote cloned voice on ElevenLabs (operation=clone_voice)",
        "calls ElevenLabs API",
    ]
    user_visible_verification = [
        "Listen to generated audio for natural speech quality (text_to_speech)",
        "Synthesize a test sentence with the new voice_id after cloning",
    ]

    DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"

    def get_status(self) -> ToolStatus:
        if os.environ.get("ELEVENLABS_API_KEY"):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        return round(len(inputs.get("text", "")) * 0.0003, 4)

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        api_key = os.environ.get("ELEVENLABS_API_KEY")
        if not api_key:
            return ToolResult(success=False, error="No ElevenLabs API key. " + self.install_instructions)

        # Default to text_to_speech when callers omit operation (back-compat
        # with the pre-clone API: callers still pass just {"text": "..."}).
        operation = inputs.get("operation") or ("text_to_speech" if inputs.get("text") else "list_cloned_voices")

        start = time.time()
        try:
            if operation == "text_to_speech":
                result = self._generate(inputs, api_key)
            elif operation == "clone_voice":
                result = self._clone_voice(inputs, api_key)
            elif operation == "list_cloned_voices":
                result = self._list_cloned_voices(inputs, api_key)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"ElevenLabs operation '{operation}' failed: {type(exc).__name__}: {exc}",
            )

        result.duration_seconds = round(time.time() - start, 2)
        # TTS cost scales with text length; cloning/listing have a fixed per-call cost
        # that we surface separately. Don't claim zero cost for non-TTS ops.
        if operation == "text_to_speech":
            result.cost_usd = self.estimate_cost(inputs)
        return result

    def _generate(self, inputs: dict[str, Any], api_key: str) -> ToolResult:
        import requests

        text = inputs["text"]
        voice_id = inputs.get("voice_id", self.DEFAULT_VOICE_ID)
        model_id = inputs.get("model_id", "eleven_multilingual_v2")
        output_format = inputs.get("output_format", "mp3_44100_128")
        voice_settings = {
            "stability": inputs.get("stability", 0.5),
            "similarity_boost": inputs.get("similarity_boost", 0.75),
            "style": inputs.get("style", 0.0),
            "speed": inputs.get("speed", 1.0),
            "use_speaker_boost": inputs.get("use_speaker_boost", True),
        }

        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": model_id,
                "voice_settings": voice_settings,
            },
            params={"output_format": output_format},
            timeout=120,
        )
        response.raise_for_status()

        ext = "mp3" if "mp3" in output_format else "wav"
        output_path = Path(inputs.get("output_path", f"tts_output.{ext}"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "model": model_id,
                "voice_id": voice_id,
                "voice_settings": voice_settings,
                "text_length": len(text),
                "output": str(output_path),
                "format": output_format,
            },
            artifacts=[str(output_path)],
            model=model_id,
        )

    # ---- Voice cloning (instant clone via POST /v1/voices/add) ----

    def _clone_voice(self, inputs: dict[str, Any], api_key: str) -> ToolResult:
        """Create a cloned voice on ElevenLabs from 1+ audio sample files.

        Requires ELEVENLABS_API_KEY with Instant Voice Cloning enabled on the
        account. Recommended total sample duration >= 60s; shorter clips still
        succeed but yield lower-quality clones.

        Reference: https://docs.elevenlabs.io/api-reference/voices/add
        """
        import requests

        name = (inputs.get("name") or "").strip()
        audio_paths = inputs.get("audio_paths") or []
        description = inputs.get("description") or ""
        labels = inputs.get("labels") or {}

        if not name:
            return ToolResult(success=False, error="clone_voice requires `name` (display name).")
        if not audio_paths:
            return ToolResult(
                success=False,
                error="clone_voice requires `audio_paths` (list of 1+ absolute paths to wav/mp3/m4a samples).",
            )

        # Open sample files; fail fast if any are missing so we don't half-upload.
        files_payload = []
        try:
            for path_str in audio_paths:
                p = Path(path_str)
                if not p.exists():
                    return ToolResult(
                        success=False,
                        error=f"audio sample not found: {p}",
                    )
                # ElevenLabs expects `files[]` multipart field; requests handles
                # multi-file via appending the same field name.
                files_payload.append(
                    ("files", (p.name, p.read_bytes(), "audio/mpeg"))
                )
        finally:
            # requests will close file parts it consumed; we passed raw bytes, so nothing to close here.
            pass

        data_form: dict[str, str] = {"name": name}
        if description:
            data_form["description"] = description
        if labels:
            data_form["labels"] = json.dumps(labels)

        response = requests.post(
            "https://api.elevenlabs.io/v1/voices/add",
            headers={"xi-api-key": api_key},
            data=data_form,
            files=files_payload,
            timeout=180,
        )

        # Surface the API error message verbatim — ElevenLabs returns structured
        # JSON with `detail` for almost every failure mode (quota, sample too
        # short, invalid format, etc.). Wrapping in ToolResult lets the caller
        # distinguish "audio too short" from "network down".
        if not response.ok:
            err_body: Any = response.text
            try:
                err_body = response.json()
            except ValueError:
                pass
            return ToolResult(
                success=False,
                error=f"ElevenLabs clone_voice failed ({response.status_code}): {err_body}",
            )

        payload = response.json()
        voice_id = payload.get("voice_id") or payload.get("voice", {}).get("voice_id")
        if not voice_id:
            return ToolResult(
                success=False,
                error=f"ElevenLabs returned 200 but no voice_id in body: {payload}",
            )

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "voice_id": voice_id,
                "name": name,
                "description": description,
                "labels": labels,
                "sample_count": len(audio_paths),
                "raw": payload,
            },
            model="elevenlabs_instant_clone",
        )

    def _list_cloned_voices(self, inputs: dict[str, Any], api_key: str) -> ToolResult:
        """List voices owned by this ElevenLabs account (cloned + custom).

        Default scope is `?show_only_owned=true` so callers see their own
        voices without the curated library. Set `include_library=True` in
        inputs to include library voices.
        """
        import requests

        include_library = bool(inputs.get("include_library", False))

        params = {}
        if not include_library:
            params["show_only_owned"] = "true"

        response = requests.get(
            "https://api.elevenlabs.io/v1/voices",
            headers={"xi-api-key": api_key, "Accept": "application/json"},
            params=params,
            timeout=60,
        )
        response.raise_for_status()

        payload = response.json()
        voices = payload.get("voices") or []
        # Tag cloned voices for callers (they may want to filter them later).
        for v in voices:
            v.setdefault("category", v.get("category") or "unknown")
            v["is_cloned"] = v.get("category") == "cloned"

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "voice_count": len(voices),
                "cloned_count": sum(1 for v in voices if v.get("is_cloned")),
                "voices": voices,
                "scope": "owned" if not include_library else "owned+library",
            },
        )
