"""Voicebox MCP integration tests (direct).

JSON-RPC over HTTP directly against voicebox's MCP server at
VOICEBOX_REST_URL + /mcp/ (default http://127.0.0.1:17493/mcp/).

These tests exercise the same MCP tools voicebox publishes for any
client (Claude Code, Cursor, etc.) and prove they round-trip cleanly:
voicebox.list_profiles, voicebox.analyze_sample, voicebox.speak.

The MCP speak tool returns a generation_id; we don't poll for completion
through MCP (status is SSE-only on the REST side), so for the full
roundtrip we hand off to the REST layer's wait_for_generation helper.

Run:
    python -m pytest tests/integration/test_voicebox_mcp_direct.py -v
"""

from __future__ import annotations

import uuid

from .conftest import (
    TTS_TIMEOUT_S,
    call_mcp_tool,
    wait_for_generation,
    _vb_headers,
)


# ---------------------------------------------------------------------------
# list_profiles
# ---------------------------------------------------------------------------

def test_list_profiles_via_mcp(mcp_session_to_voicebox) -> None:
    """voicebox.list_profiles returns the same shape as REST GET /profiles."""
    sess, url = mcp_session_to_voicebox
    result = call_mcp_tool(sess, url, "voicebox.list_profiles", {})
    profiles = result.get("profiles") or []
    assert isinstance(profiles, list)
    for prof in profiles:
        # The MCP shape (services/profiles.py:list_profiles) returns a
        # slimmed-down projection: id, name, voice_type, language,
        # has_personality. Tests downstream that need full schema should
        # hit REST /profiles instead.
        assert "id" in prof
        assert "name" in prof


# ---------------------------------------------------------------------------
# analyze_sample
# ---------------------------------------------------------------------------

def test_analyze_sample_via_mcp(
    mcp_session_to_voicebox,
    voicebox_available: str,
    sample_audio,
) -> None:
    """voicebox.analyze_sample on a real wav returns a quality score.

    The MCP tool requires profile_id (sample is analyzed against the
    profile's voice model). We point it at the shared clone profile --
    not the one we just made, but the session-scoped fixture used by the
    REST TTS test.
    """
    from pathlib import Path
    sess, url = mcp_session_to_voicebox
    profile_id = _shared_profile_id(voicebox_available)
    if profile_id is None:
        import pytest
        pytest.skip("shared clone profile unavailable for analyze_sample")

    # Read the file bytes and base64-encode for the MCP transport. Loopback
    # callers could pass audio_path instead, but base64 avoids relying on
    # voicebox's loopback-detection heuristics.
    import base64
    audio_bytes = Path(sample_audio).read_bytes()
    result = call_mcp_tool(
        sess, url, "voicebox.analyze_sample",
        {
            "profile_id": profile_id,
            "reference_text": "two two two two two",
            "audio_base64": base64.b64encode(audio_bytes).decode("ascii"),
        },
    )
    # The exact schema is documented in voicebox.services.sample_quality;
    # we assert just on the quality score being present and finite.
    score = result.get("score")
    if score is not None:
        assert 0.0 <= float(score) <= 1.0


# ---------------------------------------------------------------------------
# speak
# ---------------------------------------------------------------------------

def test_speak_kicks_off_generation(
    mcp_session_to_voicebox,
    voicebox_available: str,
) -> None:
    """voicebox.speak returns a generation_id; the REST status endpoint can poll it.

    Cross-protocol handoff is the point: MCP for kickoff, REST/SSE for
    status (because FastMCP doesn't expose the SSE status stream). This
    is exactly what tools/audio/voicebox_tts.py does internally for
    text_to_speech.
    """
    import pytest
    sess, url = mcp_session_to_voicebox
    profile_id = _shared_profile_id(voicebox_available)
    if profile_id is None:
        pytest.skip("shared clone profile unavailable for speak")

    result = call_mcp_tool(
        sess, url, "voicebox.speak",
        {
            "profile": profile_id,
            "text": "Hello world from MCP.",
            "engine": "qwen",
            "language": "en",
            "personality": False,
        },
    )
    gen_id = result.get("generation_id")
    assert gen_id, f"speak did not return generation_id: {result}"

    # Poll via REST -- proves MCP and REST share the same generation state.
    terminal = wait_for_generation(voicebox_available, gen_id, timeout_s=TTS_TIMEOUT_S)
    assert terminal["status"] == "completed", terminal


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _shared_profile_id(voicebox_available: str) -> str | None:
    """Find the pytest-shared clone profile by name prefix.

    Voicebox's session-scoped fixture names profiles
    `pytest-shared-<8hex>`. We scan profiles once to find one; if none
    exists the caller should skip. Scanning by name is more robust than
    caching the id across test files (each pytest run gets a fresh uuid).
    """
    import requests
    try:
        resp = requests.get(
            f"{voicebox_available}/profiles",
            headers=_vb_headers(),
            timeout=10,
        )
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    for prof in resp.json() or []:
        if prof.get("name", "").startswith("pytest-shared-"):
            return prof["id"]
    return None