"""Contract and transport tests for the internal Voicebox adapter."""

from __future__ import annotations

from unittest.mock import Mock

from tools.audio.voicebox_tts import VoiceboxTTS
from tools.audio.voicebox_voice_clone import VoiceboxVoiceClone


def _response(payload: dict, status_code: int = 200) -> Mock:
    response = Mock()
    response.status_code = status_code
    response.json.return_value = payload
    if status_code >= 400:
        from requests import HTTPError

        response.raise_for_status.side_effect = HTTPError(f"HTTP {status_code}")
    return response


def test_voicebox_clone_sends_consent_and_normalizes_voice_id(monkeypatch):
    monkeypatch.setenv("VOICEBOX_BASE_URL", "http://voicebox.local")
    monkeypatch.setenv("VOICEBOX_TOKEN", "secret-token")
    response = _response({"data": {"provider_voice_id": "vb-123", "task_id": "task-1"}})
    post = Mock(return_value=response)
    monkeypatch.setattr("lib.voicebox_client.requests.post", post)

    result = VoiceboxVoiceClone().execute(
        {"sample_path": "asset://sample-1", "consent": True, "user_id": "u-1"}
    )

    assert result.success
    assert result.data["voice_id"] == "vb-123"
    assert result.data["provider_voice_id"] == "vb-123"
    request = post.call_args.kwargs
    assert request["json"]["consent"] is True
    assert request["json"]["sample_path"] == "asset://sample-1"
    assert request["headers"]["Authorization"] == "Bearer secret-token"


def test_voicebox_tts_writes_explicit_base64_audio_and_segments(monkeypatch, tmp_path):
    monkeypatch.setenv("VOICEBOX_BASE_URL", "http://voicebox.local")
    monkeypatch.setenv("VOICEBOX_TOKEN", "secret-token")
    response = _response(
        {
            "audio_base64": "aGVsbG8=",
            "segments": [{"text": "hello", "start": 0, "end": 1}],
            "task_id": "tts-1",
        }
    )
    monkeypatch.setattr("lib.voicebox_client.requests.post", Mock(return_value=response))
    output = tmp_path / "voice.mp3"

    result = VoiceboxTTS().execute(
        {"text": "hello", "voice_id": "vb-123", "output_path": str(output), "subtitle": True}
    )

    assert result.success
    assert result.data["output"] == str(output)
    assert result.data["segments"][0]["text"] == "hello"
    assert output.read_bytes() == b"hello"


def test_voicebox_errors_do_not_echo_token(monkeypatch):
    monkeypatch.setenv("VOICEBOX_BASE_URL", "http://voicebox.local")
    monkeypatch.setenv("VOICEBOX_TOKEN", "secret-token")
    response = _response({}, status_code=500)
    monkeypatch.setattr("lib.voicebox_client.requests.post", Mock(return_value=response))

    result = VoiceboxTTS().execute({"text": "hello"})

    assert not result.success
    assert "secret-token" not in (result.error or "")
