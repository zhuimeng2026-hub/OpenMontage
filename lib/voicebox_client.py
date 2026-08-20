"""Small, authenticated client for the internal Voicebox service.

Voicebox is an execution service, not a public MCP surface.  This module keeps
its HTTP details out of the BaseTool implementations and deliberately exposes
only JSON request/response methods.  The service may be local, LAN-only, or a
future IPv6 worker; callers should continue to use the same client contract.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urljoin

import requests


class VoiceboxClientError(RuntimeError):
    """An expected, user-safe Voicebox transport or response error."""


@dataclass(frozen=True)
class VoiceboxConfig:
    """Connection settings loaded from the environment."""

    base_url: str
    token: str
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "VoiceboxConfig":
        raw_timeout = os.environ.get("VOICEBOX_TIMEOUT_SECONDS", "120")
        try:
            timeout = float(raw_timeout)
        except (TypeError, ValueError):
            timeout = 120.0
        # A non-positive timeout makes requests behave surprisingly and is
        # almost always an accidental env configuration.
        if timeout <= 0:
            timeout = 120.0
        return cls(
            base_url=os.environ.get("VOICEBOX_BASE_URL", "").strip().rstrip("/"),
            token=os.environ.get("VOICEBOX_TOKEN", ""),
            timeout_seconds=timeout,
        )

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.token)


class VoiceboxClient:
    """Authenticated JSON client for health, clone, and TTS operations."""

    # Paths are intentionally configurable: deployed Voicebox versions may
    # expose /health, /api/v1/health, or a versioned task endpoint.
    HEALTH_PATH = "/health"
    CLONE_PATH = "/api/v1/voices/clone"
    TTS_PATH = "/api/v1/tts"

    def __init__(
        self,
        *,
        base_url: Optional[str] = None,
        token: Optional[str] = None,
        timeout_seconds: Optional[float] = None,
        session: Any = None,
    ) -> None:
        env = VoiceboxConfig.from_env()
        configured_timeout = env.timeout_seconds
        if timeout_seconds is not None:
            try:
                candidate_timeout = float(timeout_seconds)
            except (TypeError, ValueError):
                candidate_timeout = configured_timeout
            if candidate_timeout > 0:
                configured_timeout = candidate_timeout
        self.config = VoiceboxConfig(
            base_url=(base_url if base_url is not None else env.base_url).strip().rstrip("/"),
            token=token if token is not None else env.token,
            timeout_seconds=configured_timeout,
        )
        self.session = session

    @property
    def configured(self) -> bool:
        return self.config.configured

    def health(self) -> dict[str, Any]:
        """Return the Voicebox health payload or raise a safe client error."""

        return self._request("GET", self._path("VOICEBOX_HEALTH_PATH", self.HEALTH_PATH))

    # Keep the more explicit spelling available to preflight callers while
    # retaining ``health`` as the short client API.
    def health_check(self) -> dict[str, Any]:
        return self.health()

    def clone_voice(
        self,
        *,
        sample_path: str,
        voice_id: Optional[str] = None,
        consent: bool = False,
        **metadata: Any,
    ) -> dict[str, Any]:
        """Submit a voice-clone task using an internal sample path/asset id."""

        if not isinstance(sample_path, str) or not sample_path.strip():
            raise VoiceboxClientError("Voicebox clone requires sample_path")
        if consent is not True:
            raise VoiceboxClientError("Explicit consent is required for voice cloning")

        body: dict[str, Any] = {
            "sample_path": sample_path,
            "consent": True,
        }
        if voice_id:
            body["voice_id"] = voice_id
        body.update({key: value for key, value in metadata.items() if value is not None})
        return self._request("POST", self._path("VOICEBOX_CLONE_PATH", self.CLONE_PATH), body)

    def tts(
        self,
        *,
        text: str,
        voice_id: Optional[str] = None,
        output_path: Optional[str] = None,
        subtitle: bool = True,
        **options: Any,
    ) -> dict[str, Any]:
        """Submit a TTS task and return its JSON result."""

        if not isinstance(text, str) or not text.strip():
            raise VoiceboxClientError("Voicebox TTS requires non-empty text")

        body: dict[str, Any] = {
            "text": text,
            "subtitle": bool(subtitle),
        }
        if voice_id:
            body["voice_id"] = voice_id
        if output_path:
            body["output_path"] = output_path
        body.update({key: value for key, value in options.items() if value is not None})
        return self._request("POST", self._path("VOICEBOX_TTS_PATH", self.TTS_PATH), body)

    def _path(self, env_name: str, default: str) -> str:
        return os.environ.get(env_name, default) or default

    def _request(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None) -> dict[str, Any]:
        if not self.config.base_url:
            raise VoiceboxClientError("VOICEBOX_BASE_URL is not configured")
        if not self.config.token:
            raise VoiceboxClientError("VOICEBOX_TOKEN is not configured")

        url = urljoin(self.config.base_url + "/", path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.config.token}",
            "Content-Type": "application/json",
        }
        try:
            if self.session is not None:
                response = self.session.request(
                    method,
                    url,
                    headers=headers,
                    json=dict(payload) if payload is not None else None,
                    timeout=self.config.timeout_seconds,
                )
            elif method == "GET":
                response = requests.get(
                    url,
                    headers=headers,
                    timeout=self.config.timeout_seconds,
                )
            else:
                response = requests.post(
                    url,
                    headers=headers,
                    json=dict(payload) if payload is not None else None,
                    timeout=self.config.timeout_seconds,
                )
            response.raise_for_status()
        except requests.RequestException as exc:
            raise VoiceboxClientError(self._safe_error(exc)) from exc

        try:
            result = response.json()
        except (TypeError, ValueError) as exc:
            raise VoiceboxClientError(
                f"Voicebox returned a non-JSON response (HTTP {response.status_code})"
            ) from exc
        if not isinstance(result, dict):
            raise VoiceboxClientError("Voicebox returned an invalid JSON object")
        return result

    def _safe_error(self, exc: Exception) -> str:
        message = str(exc)
        token = self.config.token
        if token:
            message = message.replace(token, "[redacted]")
        # Requests may include an Authorization header in a repr produced by a
        # custom adapter.  Never expose that header in a BaseTool error.
        if "authorization" in message.lower():
            return "Voicebox request failed (authentication details redacted)"
        return f"Voicebox request failed: {message or exc.__class__.__name__}"


def decode_audio_base64(value: Any) -> Optional[bytes]:
    """Decode one of the explicit base64 fields returned by Voicebox."""

    if not isinstance(value, str) or not value:
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError):
        return None
