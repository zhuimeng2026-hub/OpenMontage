"""Voicebox text-to-speech provider tool.

Wraps the local Voicebox REST API (port 17493 by default) so voicebox appears as
a first-class TTS provider in OpenMontage's tool registry. Voice cloning and
synthesis happen entirely on the host running Voicebox — no API keys, no
cloud spend, voice data never leaves the machine.

Voicebox's "voice clone" is a VoiceProfile with one or more reference samples.
The REST flow is:

    POST /profiles                       -- create empty profile
    POST /profiles/{id}/samples          -- attach reference audio (multipart)
    POST /generate   (profile_id=...)    -- kick off synthesis, returns gen id
    GET  /generate/{id}/status           -- SSE: completed / failed
    GET  /audio/{generation_id}          -- download the produced audio

We poll the SSE status endpoint with a bounded read so we don't block forever
if the worker dies, and we copy the audio into projects/<id>/assets/audio/ so
the Backlot board can find it.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import requests

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


# Voicebox serves REST + MCP on the same port (default 17493). The OpenMontage
# MCP server at :8900 reverse-proxies /voicebox/mcp/* → here, but this tool
# talks REST directly so the pipeline can pick voicebox without going through
# MCP at all. Override with VOICEBOX_REST_URL for remote voicebox hosts.
DEFAULT_VOICEBOX_REST_URL = "http://127.0.0.1:17493"

# Engine names accepted by Voicebox's GenerationRequest.engine. Mirrors the
# regex in /opt/voicebox/backend/models.py:GenerationRequest so callers don't
# pass a string voicebox will 400 on.
SUPPORTED_ENGINES = (
    "qwen",
    "qwen_custom_voice",
    "luxtts",
    "chatterbox",
    "chatterbox_turbo",
    "tada",
    "kokoro",
)

# Only these engines support the "cloned voice" workflow (a profile with
# reference samples). Preset voices (kokoro, qwen_custom_voice) don't take
# samples. Mirrors CLONING_ENGINES in voicebox's services/profiles.py.
CLONING_ENGINES = {"qwen", "luxtts", "chatterbox", "chatterbox_turbo", "tada"}

# How long we'll wait for a generation to finish before giving up. Voicebox
# is local; even long-form narration with effects should finish well inside
# this on modern GPUs. On CPU-only hosts this can blow past it for very long
# scripts — bump DEFAULT_GENERATION_TIMEOUT_S or pass `timeout_seconds` if so.
DEFAULT_GENERATION_TIMEOUT_S = 600
# Poll cadence for the SSE status endpoint. 1s matches voicebox's own SSE
# emit interval, so we don't add latency; if voicebox is busy, the worker
# still wakes us up every second.
STATUS_POLL_INTERVAL_S = 1.0

# Client identity surfaced to voicebox's X-Voicebox-Client-Id middleware.
# Loopback callers don't need a secret, but the header is mandatory so voicebox
# can attribute the request and apply per-client policies (audio_path gating,
# default voice bindings).
VOICEBOX_CLIENT_ID = "openmontage-tts"

# Audio MIME types we accept as clone samples. Matches voicebox's
# _allowed_audio_exts in routes/profiles.py.
_ALLOWED_SAMPLE_EXTS = {".wav", ".mp3", ".m4a", ".ogg", ".flac", ".aac", ".webm", ".opus"}


class VoiceboxTTS(BaseTool):
    """Local TTS + voice cloning via the Voicebox REST API."""

    name = "voicebox_tts"
    version = "0.1.0"
    tier = ToolTier.VOICE
    capability = "tts"
    provider = "voicebox"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.STOCHASTIC
    runtime = ToolRuntime.LOCAL

    # No env-var API key — voicebox is local and uses X-Voicebox-Client-Id.
    # VOICEBOX_REST_URL is optional (defaults to localhost:17493).
    dependencies = []
    install_instructions = (
        "Run the Voicebox backend on the local host (default http://127.0.0.1:17493).\n"
        "  - macOS / Windows: install the Voicebox desktop app from voicebox.sh\n"
        "  - Docker: docker compose up in the Voicebox repo\n"
        "Optional: set VOICEBOX_REST_URL to point at a remote Voicebox host."
    )

    fallback = "elevenlabs_tts"
    fallback_tools = ["elevenlabs_tts", "piper_tts"]
    agent_skills = ["voicebox"]

    capabilities = [
        "text_to_speech",
        "voice_selection",
        "voice_cloning",
        "list_cloned_voices",
    ]
    supports = {
        # Voicebox is the only TTS provider in this repo with truly offline,
        # local-first voice cloning (Qwen3-TTS / Chatterbox / LuxTTS run on
        # the host). ElevenLabs is cloud; Piper doesn't clone.
        "voice_cloning": True,
        "multilingual": True,
        "offline": True,
        "native_audio": True,
        "privacy_local": True,
    }
    best_for = [
        "privacy-sensitive narration where voice data must stay on-prem",
        "voice cloning without a paid cloud subscription",
        "long-form multilingual narration (23 languages via Chatterbox)",
        "emotion-tagged speech via Chatterbox Turbo paralinguistics ([laugh], [sigh])",
    ]
    not_good_for = [
        "running when Voicebox isn't installed locally — check `make preflight`",
        "instant one-shot cloning shorter than a few seconds (Qwen3-TTS needs "
        "enough reference audio for a stable clone; use a longer clip or "
        "fall back to ElevenLabs)",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["text_to_speech", "clone_voice", "list_cloned_voices"],
                "description": (
                    "`text_to_speech` (default if `text` is provided): synthesize "
                    "speech from a cloned or preset voice profile.\n"
                    "`clone_voice`: create a VoiceProfile and attach 1+ reference "
                    "audio samples; returns `profile_id` for use with "
                    "`text_to_speech`.\n"
                    "`list_cloned_voices`: enumerate voice profiles on the local "
                    "Voicebox instance (filtered to `voice_type=cloned`)."
                ),
            },

            # ---- text_to_speech ----
            "text": {
                "type": "string",
                "description": "Text to speak (operation=text_to_speech).",
            },
            "profile_id": {
                "type": "string",
                "description": (
                    "Voicebox voice profile id (operation=text_to_speech). "
                    "Returned by clone_voice or list_cloned_voices."
                ),
            },
            "language": {
                "type": "string",
                "default": "en",
                "enum": [
                    "zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it",
                    "he", "ar", "da", "el", "fi", "hi", "ms", "nl", "no", "pl",
                    "sv", "sw", "tr",
                ],
                "description": (
                    "BCP-47-ish language code. Voicebox validates against a "
                    "whitelist per engine (operation=text_to_speech)."
                ),
            },
            "engine": {
                "type": "string",
                "enum": list(SUPPORTED_ENGINES),
                "description": (
                    "TTS engine override. Defaults to the profile's "
                    "`default_engine` when omitted. Use `qwen`, `chatterbox`, "
                    "`chatterbox_turbo`, `luxtts`, or `tada` for cloned voices; "
                    "use `kokoro` or `qwen_custom_voice` for preset voices."
                ),
            },
            "model_size": {
                "type": "string",
                "description": (
                    "Engine-specific model size (e.g. Qwen3-TTS '0.6B' or '1.7B'). "
                    "Only honored by engines that take a size selector."
                ),
            },
            "instruct": {
                "type": "string",
                "description": (
                    "Natural-language delivery instruction (Qwen3-TTS / "
                    "Qwen CustomVoice only, e.g. 'speak slowly with a smile')."
                ),
            },
            "seed": {
                "type": "integer",
                "description": "Optional seed for reproducibility (engine-dependent).",
            },
            "personality": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true AND the profile has a personality prompt, voicebox "
                    "rewrites the text in-character via its bundled local LLM "
                    "before TTS. Otherwise the text is spoken verbatim."
                ),
            },
            "output_path": {
                "type": "string",
                "description": (
                    "Absolute path to write the synthesized audio. Defaults to "
                    "the active project's assets/audio/ directory; the Backlot "
                    "board only surfaces files written under projects/<id>/."
                ),
            },
            "timeout_seconds": {
                "type": "integer",
                "default": DEFAULT_GENERATION_TIMEOUT_S,
                "description": (
                    "How long to wait for voicebox's generation worker to "
                    "finish before failing (operation=text_to_speech)."
                ),
            },

            # ---- clone_voice ----
            "name": {
                "type": "string",
                "description": (
                    "Display name for the new voice profile (operation=clone_voice). "
                    "Must be unique on the local Voicebox instance."
                ),
            },
            "audio_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Absolute paths to 1+ reference audio files (wav/mp3/m4a/ogg/"
                    "flac/aac/webm/opus). Recommended total duration >= 30s for "
                    "a usable Qwen3-TTS clone; shorter clips still succeed but "
                    "yield lower-quality clones (operation=clone_voice)."
                ),
            },
            "reference_texts": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional 1:1 transcripts for `audio_paths`. If omitted, "
                    "`reference_text` is applied to every sample; if neither is "
                    "provided, voicebox stores samples with empty transcripts "
                    "(operation=clone_voice)."
                ),
            },
            "reference_text": {
                "type": "string",
                "description": (
                    "Fallback transcript applied to every sample in `audio_paths` "
                    "when `reference_texts` is not given (operation=clone_voice)."
                ),
            },
            "description": {
                "type": "string",
                "description": "Optional free-text notes for the profile (operation=clone_voice).",
            },
            "default_engine": {
                "type": "string",
                "enum": sorted(CLONING_ENGINES),
                "default": "qwen",
                "description": (
                    "Engine voicebox will use for subsequent /generate calls "
                    "against this profile. Must be a CLONING_ENGINE — preset "
                    "voices don't accept samples (operation=clone_voice)."
                ),
            },

            # ---- list_cloned_voices ----
            "include_presets": {
                "type": "boolean",
                "default": False,
                "description": (
                    "If true, include preset and designed voices in the result "
                    "in addition to cloned voices (operation=list_cloned_voices)."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=100, network_required=False
    )
    retry_policy = RetryPolicy(
        max_retries=1,
        backoff_seconds=2.0,
        # Voicebox is local but its generation worker can transiently 502 if
        # a model is still loading — a single retry covers that without
        # doubling the cost of long synthesis calls.
        retryable_errors=["502", "503", "504", "connection_error"],
    )
    idempotency_key_fields = [
        "operation",
        "text",
        "profile_id",
        "engine",
        "language",
        "model_size",
        "name",
        "audio_paths",
    ]
    side_effects = [
        "writes audio file to output_path (operation=text_to_speech)",
        "creates a VoiceProfile on the local Voicebox instance (operation=clone_voice)",
        "attaches reference audio to a VoiceProfile (operation=clone_voice)",
    ]
    user_visible_verification = [
        "Listen to the synthesized audio for natural speech quality (text_to_speech).",
        "Speak a short sentence with the new profile_id after clone_voice to confirm fidelity.",
    ]

    # ------------------------------------------------------------------
    # Status & cost
    # ------------------------------------------------------------------

    def get_status(self) -> ToolStatus:
        """Healthy iff Voicebox's /health endpoint responds 200 within 2s.

        A reachable but unhealthy Voicebox (e.g. model still loading) is
        surfaced as DEGRADED so the agent can still see it in the provider
        menu and decide whether to wait or fall back.
        """
        base = self._base_url()
        try:
            resp = requests.get(f"{base}/health", timeout=2)
        except requests.RequestException:
            return ToolStatus.UNAVAILABLE
        if resp.status_code == 200:
            return ToolStatus.AVAILABLE
        if resp.status_code in (502, 503, 504):
            return ToolStatus.DEGRADED
        return ToolStatus.UNAVAILABLE

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # Voicebox is local and free; surface 0.0 so the cost rollup stays
        # honest. clone_voice + list_cloned_voices also cost nothing.
        return 0.0

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        # Default op back-compat: if a caller passes only {"text": ...}, treat
        # it as text_to_speech (matches ElevenLabs' pre-clone convention).
        # Also accept "generate" as text_to_speech for cross-provider compatibility.
        operation = inputs.get("operation")
        if operation == "generate":
            operation = "text_to_speech"
        if operation is None:
            operation = "text_to_speech" if inputs.get("text") else "list_cloned_voices"

        start = time.time()
        try:
            if operation == "text_to_speech":
                result = self._generate(inputs)
            elif operation == "clone_voice":
                result = self._clone_voice(inputs)
            elif operation == "list_cloned_voices":
                result = self._list_cloned_voices(inputs)
            else:
                return ToolResult(
                    success=False,
                    error=(
                        f"Unknown operation: {operation!r}. "
                        "Expected one of: text_to_speech, clone_voice, list_cloned_voices."
                    ),
                )
        except requests.RequestException as exc:
            return ToolResult(
                success=False,
                error=(
                    f"Voicebox REST call failed: {type(exc).__name__}: {exc}. "
                    f"Is Voicebox running at {self._base_url()}? "
                    f"Override with VOICEBOX_REST_URL."
                ),
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"voicebox_tts operation '{operation}' failed: {type(exc).__name__}: {exc}",
            )

        result.duration_seconds = round(time.time() - start, 2)
        return result

    # ------------------------------------------------------------------
    # text_to_speech: POST /generate → poll SSE → GET /audio/{gen_id}
    # ------------------------------------------------------------------

    def _generate(self, inputs: dict[str, Any]) -> ToolResult:
        text = inputs.get("text")
        profile_id = inputs.get("profile_id")
        if not text:
            return ToolResult(success=False, error="text_to_speech requires `text`.")
        if not profile_id:
            return ToolResult(
                success=False,
                error=(
                    "text_to_speech requires `profile_id`. Run clone_voice first "
                    "or pick one from list_cloned_voices."
                ),
            )

        # Build the GenerationRequest. Send only fields the caller actually
        # provided — voicebox has tight regex validators (language/engine) and
        # will 400 on extras.
        gen_payload: dict[str, Any] = {
            "profile_id": profile_id,
            "text": text,
            "language": inputs.get("language", "en"),
        }
        for opt_key in ("engine", "model_size", "instruct", "seed", "personality"):
            if opt_key in inputs and inputs[opt_key] is not None:
                gen_payload[opt_key] = inputs[opt_key]

        gen_resp = requests.post(
            f"{self._base_url()}/generate",
            json=gen_payload,
            headers=self._headers(),
            timeout=30,
        )
        # Voicebox surfaces engine/language/regex failures as HTTP 400 with a
        # `detail` field — pass that through verbatim so the agent can correct
        # the bad field without guessing.
        if not gen_resp.ok:
            return self._http_error("POST /generate", gen_resp)
        gen_id = (gen_resp.json() or {}).get("id")
        if not gen_id:
            return ToolResult(
                success=False,
                error=f"Voicebox POST /generate returned no `id`: {gen_resp.text[:300]}",
            )

        # Poll the SSE status endpoint. Voicebox's status payload is:
        #   {"id": "...", "status": "queued"|"generating"|"completed"|"failed",
        #    "duration": ..., "error": ..., "source": ...}
        # We read the SSE stream and stop on terminal status; bounded by
        # timeout_seconds so a stuck worker can't hang the pipeline.
        timeout_s = int(inputs.get("timeout_seconds") or DEFAULT_GENERATION_TIMEOUT_S)
        terminal = self._wait_for_generation(gen_id, timeout_s)
        if not terminal["ok"]:
            return ToolResult(
                success=False,
                error=(
                    f"Voicebox generation {gen_id} did not complete: "
                    f"{terminal.get('error') or 'timeout'}"
                ),
                data={
                    "provider": self.provider,
                    "generation_id": gen_id,
                    "profile_id": profile_id,
                    "status": terminal.get("status"),
                    "duration": terminal.get("duration"),
                },
            )

        # Pull the audio bytes from /audio/{gen_id} (FastAPI FileResponse on
        # voicebox's side) and write to the active project's assets/audio/.
        audio_resp = requests.get(
            f"{self._base_url()}/audio/{gen_id}",
            headers=self._headers(),
            timeout=120,
            stream=True,
        )
        if not audio_resp.ok:
            return self._http_error(f"GET /audio/{gen_id}", audio_resp)

        output_path = self._resolve_output_path(inputs, gen_id, audio_resp)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("wb") as fh:
            for chunk in audio_resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    fh.write(chunk)

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "generation_id": gen_id,
                "profile_id": profile_id,
                "engine": gen_payload.get("engine"),
                "language": gen_payload.get("language"),
                "duration": terminal.get("duration"),
                "output": str(output_path),
                "model": gen_payload.get("engine") or "voicebox",
            },
            artifacts=[str(output_path)],
            model=gen_payload.get("engine") or "voicebox",
        )

    def _wait_for_generation(self, gen_id: str, timeout_s: int) -> dict[str, Any]:
        """Poll /generate/{id}/status (SSE) until terminal status or timeout.

        Returns a dict like:
          {"ok": True,  "status": "completed", "duration": 3.42}
          {"ok": False, "status": "failed",    "error": "..."}
          {"ok": False, "status": "timeout",   "error": "..."}

        We don't use the `requests` SSE iterator directly because it would
        block until the server closes the stream; instead we open the stream
        with a generous read timeout and break on the first terminal event.
        """
        deadline = time.monotonic() + timeout_s
        last_status = "queued"
        try:
            with requests.get(
                f"{self._base_url()}/generate/{gen_id}/status",
                headers={**self._headers(), "Accept": "text/event-stream"},
                timeout=(10, STATUS_POLL_INTERVAL_S + 5),
                stream=True,
            ) as resp:
                resp.raise_for_status()
                for raw in resp.iter_lines(chunk_size=1):
                    if time.monotonic() > deadline:
                        return {"ok": False, "status": "timeout", "error": f"timeout after {timeout_s}s"}
                    if not raw:
                        continue
                    # SSE lines look like: b"data: {json}".
                    # `resp.iter_lines()` yields bytes; comparing against a
                    # str literal raises `TypeError: startswith first arg
                    # must be bytes or a tuple of bytes, not str`. Compare
                    # against bytes throughout and let json.loads accept the
                    # bytes payload directly.
                    if not raw.startswith(b"data:"):
                        continue
                    payload_bytes = raw[len(b"data:"):].strip()
                    try:
                        payload = json.loads(payload_bytes)
                    except json.JSONDecodeError:
                        continue
                    last_status = payload.get("status") or last_status
                    if last_status == "completed":
                        return {"ok": True, "status": last_status, "duration": payload.get("duration")}
                    if last_status == "failed":
                        return {
                            "ok": False,
                            "status": last_status,
                            "error": payload.get("error") or "voicebox reported status=failed",
                            "duration": payload.get("duration"),
                        }
                    if last_status == "not_found":
                        return {"ok": False, "status": last_status, "error": "generation not found"}
                # Stream closed before reaching terminal status — treat as timeout.
                return {
                    "ok": False,
                    "status": "timeout",
                    "error": f"SSE stream closed at status={last_status!r} after {timeout_s}s",
                }
        except requests.RequestException as exc:
            return {"ok": False, "status": "error", "error": f"{type(exc).__name__}: {exc}"}

    # ------------------------------------------------------------------
    # clone_voice: POST /profiles + POST /profiles/{id}/samples (multipart)
    # ------------------------------------------------------------------

    def _clone_voice(self, inputs: dict[str, Any]) -> ToolResult:
        name = (inputs.get("name") or "").strip()
        audio_paths = inputs.get("audio_paths") or []
        description = inputs.get("description") or ""
        default_engine = inputs.get("default_engine") or "qwen"
        ref_texts = inputs.get("reference_texts")
        fallback_ref_text = inputs.get("reference_text") or ""

        if not name:
            return ToolResult(success=False, error="clone_voice requires `name` (display name).")
        if not audio_paths:
            return ToolResult(
                success=False,
                error="clone_voice requires `audio_paths` (1+ absolute paths to wav/mp3/m4a/ogg/flac/aac/webm/opus samples).",
            )
        if default_engine not in CLONING_ENGINES:
            return ToolResult(
                success=False,
                error=(
                    f"clone_voice: default_engine={default_engine!r} is not a cloning engine. "
                    f"Use one of: {sorted(CLONING_ENGINES)}."
                ),
            )

        # Resolve per-sample transcripts up front; fail fast if the caller
        # gave us a mismatched list (the upload loop would otherwise silently
        # mis-pair samples and transcripts).
        per_sample_texts: list[str] = []
        if ref_texts is not None:
            if len(ref_texts) != len(audio_paths):
                return ToolResult(
                    success=False,
                    error=(
                        f"clone_voice: reference_texts length ({len(ref_texts)}) must "
                        f"match audio_paths length ({len(audio_paths)})."
                    ),
                )
            per_sample_texts = [str(t) for t in ref_texts]
        else:
            per_sample_texts = [fallback_ref_text] * len(audio_paths)

        # Step 1: create the empty profile.
        profile_payload: dict[str, Any] = {
            "name": name,
            "language": inputs.get("language", "en"),
            "voice_type": "cloned",
            "default_engine": default_engine,
        }
        if description:
            profile_payload["description"] = description
        create_resp = requests.post(
            f"{self._base_url()}/profiles",
            json=profile_payload,
            headers=self._headers(),
            timeout=30,
        )
        if not create_resp.ok:
            return self._http_error("POST /profiles", create_resp)
        profile = create_resp.json()
        profile_id = profile.get("id")
        if not profile_id:
            return ToolResult(
                success=False,
                error=f"Voicebox POST /profiles returned no `id`: {create_resp.text[:300]}",
            )

        # Step 2: upload each sample. Failures here are destructive — the
        # profile exists but is half-populated. We surface that explicitly so
        # the agent can decide to retry the upload or delete the profile.
        uploaded = 0
        failed_samples: list[dict[str, Any]] = []
        for audio_path, ref_text in zip(audio_paths, per_sample_texts):
            p = Path(audio_path)
            if not p.exists():
                failed_samples.append({"path": str(p), "error": "file not found"})
                continue
            if p.suffix.lower() not in _ALLOWED_SAMPLE_EXTS:
                failed_samples.append(
                    {"path": str(p), "error": f"unsupported extension {p.suffix!r}"}
                )
                continue
            try:
                with p.open("rb") as fh:
                    sample_resp = requests.post(
                        f"{self._base_url()}/profiles/{profile_id}/samples",
                        files={"file": (p.name, fh, "audio/mpeg")},
                        data={"reference_text": ref_text},
                        headers=self._headers(),
                        timeout=300,
                    )
            except OSError as exc:
                failed_samples.append({"path": str(p), "error": f"read failed: {exc}"})
                continue
            if not sample_resp.ok:
                err = self._format_http_error(sample_resp)
                failed_samples.append({"path": str(p), "error": err})
                continue
            uploaded += 1

        if uploaded == 0:
            # Roll forward without rolling back — the caller can decide whether
            # to keep the empty profile or delete it. We surface the failure
            # list explicitly so they can act.
            return ToolResult(
                success=False,
                error=(
                    f"clone_voice created profile {profile_id!r} but no samples "
                    f"were uploaded successfully. failures={failed_samples}"
                ),
                data={
                    "provider": self.provider,
                    "profile_id": profile_id,
                    "name": name,
                    "uploaded_samples": 0,
                    "failed_samples": failed_samples,
                },
            )

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "profile_id": profile_id,  # voicebox term; parallel to ElevenLabs' "voice_id"
                "name": name,
                "description": description,
                "default_engine": default_engine,
                "sample_count": uploaded,
                "failed_samples": failed_samples,
                "raw": profile,
            },
            model=f"voicebox_{default_engine}",
        )

    # ------------------------------------------------------------------
    # list_cloned_voices: GET /profiles (filtered to voice_type=cloned)
    # ------------------------------------------------------------------

    def _list_cloned_voices(self, inputs: dict[str, Any]) -> ToolResult:
        include_presets = bool(inputs.get("include_presets"))

        resp = requests.get(
            f"{self._base_url()}/profiles",
            headers=self._headers(),
            timeout=30,
        )
        if not resp.ok:
            return self._http_error("GET /profiles", resp)

        profiles = resp.json() or []
        # Tag cloned voices for callers (matches ElevenLabs' is_cloned flag
        # so downstream selectors can filter uniformly across providers).
        for prof in profiles:
            vtype = prof.get("voice_type") or "cloned"
            prof.setdefault("voice_type", vtype)
            prof["is_cloned"] = vtype == "cloned"

        if not include_presets:
            profiles = [p for p in profiles if p.get("is_cloned")]

        return ToolResult(
            success=True,
            data={
                "provider": self.provider,
                "voice_count": len(profiles),
                "cloned_count": sum(1 for p in profiles if p.get("is_cloned")),
                "voices": profiles,
                "scope": "cloned" if not include_presets else "all",
            },
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _base_url(self) -> str:
        # Match the OpenMontage MCP server's convention (mcp_server.py:2394)
        # so the two integration paths (REST and MCP) stay in sync when an
        # operator points both at a remote voicebox host.
        return os.environ.get("VOICEBOX_REST_URL", DEFAULT_VOICEBOX_REST_URL).rstrip("/")

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Accept": "application/json",
            # voicebox's request_is_loopback middleware looks at this header
            # to gate audio_path access; we always run on loopback but the
            # header is mandatory regardless.
            "X-Voicebox-Client-Id": VOICEBOX_CLIENT_ID,
        }

    def _resolve_output_path(
        self,
        inputs: dict[str, Any],
        gen_id: str,
        audio_resp: requests.Response,
    ) -> Path:
        """Pick the output path: explicit override, then infer project dir.

        Pipeline convention: assets always land under
        projects/<project_id>/assets/audio/. Falling back to cwd only when no
        project context can be inferred keeps Backlot happy (the board
        watches projects/*/events.jsonl and won't surface files elsewhere).
        """
        explicit = inputs.get("output_path")
        if explicit:
            return Path(explicit)

        # Pull the file extension from the response so voicebox can pick the
        # container (typically .wav) without us hard-coding it.
        ext = ".wav"
        cdisp = audio_resp.headers.get("content-disposition", "")
        if "filename=" in cdisp:
            tail = cdisp.split("filename=", 1)[1].strip().strip('"').strip("';")
            suffix = Path(tail).suffix.lower()
            if suffix:
                ext = suffix
        elif "audio/mpeg" in (audio_resp.headers.get("content-type") or ""):
            ext = ".mp3"

        from lib.events import infer_project_dir  # local import: base_tool loads .env on import

        project_dir = infer_project_dir(inputs)
        if project_dir is not None:
            return project_dir / "assets" / "audio" / f"voicebox_{gen_id}{ext}"
        return Path(f"voicebox_{gen_id}{ext}")

    @staticmethod
    def _format_http_error(resp: requests.Response) -> str:
        """Best-effort structured error string from a voicebox error response."""
        try:
            detail = resp.json()
        except ValueError:
            detail = resp.text
        return f"{resp.status_code} {detail}"

    def _http_error(self, op: str, resp: requests.Response) -> ToolResult:
        return ToolResult(
            success=False,
            error=f"voicebox_tts: {self._format_http_error(resp)} (op={op})",
        )