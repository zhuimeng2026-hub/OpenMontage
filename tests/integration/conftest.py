"""Shared fixtures for voicebox integration tests.

These tests hit live voicebox + OpenMontage MCP servers, so fixtures
skip gracefully when services aren't available rather than fail the
whole pytest run. Each fixture that allocates persistent state
(voice profiles, MCP sessions) cleans up after itself.
"""

from __future__ import annotations

import json
import math
import os
import struct
import uuid
import wave
from pathlib import Path
from typing import Any

import pytest
import requests


# ---------------------------------------------------------------------------
# URLs & env
# ---------------------------------------------------------------------------

VOICEBOX_DEFAULT_URL = "http://127.0.0.1:17493"
# OpenMontage MCP server exposes two mounts:
#   - OpenMontage's own FastMCP at /mcp/
#   - Voicebox reverse proxy at /voicebox/* (forwards to voicebox :17493/mcp/)
# We keep the *base* URL in OM_DEFAULT_BASE_URL and construct the two paths
# explicitly per fixture. The OM server itself listens on the base host:port.
OM_DEFAULT_BASE_URL = "http://127.0.0.1:8900"

VOICEBOX_CLIENT_ID = "openmontage-integration-test"

# How long we'll wait for voicebox's /health before declaring it down.
# 2s matches the production tool's own health-check timeout so behaviour
# stays consistent with the BaseTool's get_status() logic.
VOICEBOX_HEALTH_TIMEOUT_S = 2.0

# TTS round-trip can run 10s to several minutes depending on engine + GPU.
# Test-only limit so a stuck worker doesn't hang the suite forever. 600s
# matches the warmup budget; in practice warm tests complete in 5-30s.
TTS_TIMEOUT_S = int(os.environ.get("VOICEBOX_TEST_TTS_TIMEOUT_S", "600"))

# Project root, for sys.path so tests can import `tools.*` like the rest
# of the test tree does (see tests/tools/test_azure_stt.py for precedent).
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="session", autouse=True)
def _ensure_pythonpath() -> None:
    """Add the repo root to sys.path so `import tools.*` works under pytest."""
    import sys
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))


# ---------------------------------------------------------------------------
# Voicebox availability + base URL
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def voicebox_base_url() -> str:
    """Voicebox REST base URL (env-overridable for remote voicebox hosts)."""
    return os.environ.get("VOICEBOX_REST_URL", VOICEBOX_DEFAULT_URL).rstrip("/")


@pytest.fixture(scope="session")
def voicebox_available(voicebox_base_url: str) -> str:
    """Skip the test if voicebox isn't running. Returns the base URL on success."""
    try:
        resp = requests.get(f"{voicebox_base_url}/health", timeout=VOICEBOX_HEALTH_TIMEOUT_S)
    except requests.RequestException as exc:
        pytest.skip(f"voicebox not reachable at {voicebox_base_url}: {exc}")
    if resp.status_code != 200:
        pytest.skip(
            f"voicebox at {voicebox_base_url} returned {resp.status_code} "
            f"to /health (degraded or unhealthy)"
        )
    return voicebox_base_url


def _vb_headers() -> dict[str, str]:
    """Headers every voicebox REST call must send.

    X-Voicebox-Client-Id is required by voicebox's middleware (it gates
    audio_path access for non-loopback callers). For integration tests on
    localhost we still send it so the path matches production callers.
    """
    return {
        "Accept": "application/json",
        "X-Voicebox-Client-Id": VOICEBOX_CLIENT_ID,
    }


# ---------------------------------------------------------------------------
# Sample audio fixture
# ---------------------------------------------------------------------------

SAMPLE_WAV = PROJECT_ROOT / "tests" / "fixtures" / "voicebox" / "sample_5s.wav"
SAMPLE_DURATION_S = 5
SAMPLE_RATE_HZ = 16000


def _generate_sample_wav(path: Path) -> None:
    """Write a 5-second 220Hz sine wave mono 16-bit PCM WAV.

    Generated lazily so the binary doesn't need to be committed. Qwen3-TTS
    is fine cloning from a synthetic tone for a smoke test; we're verifying
    the integration plumbing, not voice-fidelity. Real fidelity testing
    belongs in evals/.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    n_samples = SAMPLE_DURATION_S * SAMPLE_RATE_HZ
    amplitude = 8000  # ~25% of int16 max — quiet but non-zero
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)  # 16-bit PCM
        wf.setframerate(SAMPLE_RATE_HZ)
        for i in range(n_samples):
            sample = int(amplitude * math.sin(2 * math.pi * 220.0 * i / SAMPLE_RATE_HZ))
            wf.writeframes(struct.pack("<h", sample))


@pytest.fixture(scope="session")
def sample_audio() -> Path:
    """Absolute path to a 5-second WAV suitable as a voicebox clone sample."""
    if not SAMPLE_WAV.exists() or SAMPLE_WAV.stat().st_size < 1024:
        _generate_sample_wav(SAMPLE_WAV)
    return SAMPLE_WAV


@pytest.fixture(scope="session")
def sample_audio_pair(sample_audio: Path) -> list[Path]:
    """Two paths (same file twice) for multi-sample clone tests."""
    # Reusing the same fixture path keeps the binary small. voicebox
    # deduplicates by content hash so two refs to the same file is fine.
    return [sample_audio, sample_audio]


# ---------------------------------------------------------------------------
# Profile cleanup
# ---------------------------------------------------------------------------

_CREATED_PROFILE_IDS: list[str] = []  # session-scoped, see _cleanup_profiles


@pytest.fixture
def created_profile_ids() -> list[str]:
    """Per-test alias for the session-scoped created-profile list.

    Tests append the profile_ids they allocate. The autouse
    `_cleanup_profiles` fixture DELETEs every entry at teardown of the
    test that appended it (it snapshots the list length on entry). Auto
    getfixturevalue() was deprecated in pytest 9, so we still make this
    an explicit fixture -- tests that don't allocate profiles just
    ignore the return value.
    """
    return _CREATED_PROFILE_IDS


@pytest.fixture(autouse=True)
def _cleanup_profiles(voicebox_available: str, created_profile_ids: list[str]) -> None:
    """DELETE every profile this test appended.

    Snapshots the list length on entry so a test that adds N profiles
    only triggers N deletions, not the entire session's accumulation.
    Cleanup failures don't mask test results.
    """
    snapshot_len = len(created_profile_ids)
    yield
    while len(created_profile_ids) > snapshot_len:
        pid = created_profile_ids.pop()
        try:
            requests.delete(
                f"{voicebox_available}/profiles/{pid}",
                headers=_vb_headers(),
                timeout=10,
            )
        except requests.RequestException:
            pass


# ---------------------------------------------------------------------------
# Shared cloned profile (session-scoped — cloning is the slow part)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def shared_clone_profile(
    voicebox_available: str,
    sample_audio: Path,
) -> dict[str, Any]:
    """One cloned voice profile shared across all TTS tests in the session.

    Voice cloning is the slow path on voicebox (model loading + sample
    ingestion). Sharing one profile across tests keeps the suite fast while
    still proving the integration end-to-end. Cleanup is best-effort at
    session teardown -- a failed cleanup leaves an artifact that subsequent
    runs will overwrite or skip.

    Side effect: pre-warms the Qwen TTS engine by issuing a tiny
    generation against the freshly-cloned profile. Without this, the
    first text_to_speech test in the session burns the entire TTS
    timeout on Qwen's model load (1.7B model on first use). Pre-warming
    keeps the TTS tests bounded even on cold voicebox installs.
    """
    name = f"pytest-shared-{uuid.uuid4().hex[:8]}"
    create_resp = requests.post(
        f"{voicebox_available}/profiles",
        json={
            "name": name,
            "language": "en",
            "voice_type": "cloned",
            "default_engine": "qwen",
        },
        headers=_vb_headers(),
        timeout=30,
    )
    if not create_resp.ok:
        pytest.skip(f"could not create shared profile: {create_resp.status_code} {create_resp.text[:200]}")
    profile = create_resp.json()
    profile_id = profile["id"]

    with sample_audio.open("rb") as fh:
        sample_resp = requests.post(
            f"{voicebox_available}/profiles/{profile_id}/samples",
            files={"file": (sample_audio.name, fh, "audio/wav")},
            data={"reference_text": "two two two two two"},
            headers=_vb_headers(),
            timeout=300,
        )
    if not sample_resp.ok:
        # Try to roll back the empty profile so we don't leak it.
        requests.delete(
            f"{voicebox_available}/profiles/{profile_id}",
            headers=_vb_headers(),
            timeout=10,
        )
        pytest.skip(
            f"could not upload sample to shared profile: "
            f"{sample_resp.status_code} {sample_resp.text[:200]}"
        )

    # Pre-warm the Qwen engine. Short text + generous timeout because the
    # 1.7B model takes ~30-120s to load on first use depending on host.
    warmup_timeout = int(os.environ.get("VOICEBOX_TEST_WARMUP_TIMEOUT_S", "600"))
    try:
        warmup_resp = requests.post(
            f"{voicebox_available}/generate",
            json={
                "profile_id": profile_id,
                "text": "warmup",
                "language": "en",
                "engine": "qwen",
            },
            headers=_vb_headers(),
            timeout=30,
        )
        if warmup_resp.ok:
            warmup_id = warmup_resp.json().get("id")
            if warmup_id:
                wait_for_generation(voicebox_available, warmup_id, timeout_s=warmup_timeout)
    except (requests.RequestException, TimeoutError):
        # Pre-warm is best-effort. If it fails, individual TTS tests will
        # still run with their own timeout; they'll either succeed (model
        # eventually loaded) or time out (broken install).
        pass

    # Session teardown runs AFTER `yield profile` (below) so the test that
    # consumes this fixture sees a profile that still exists. Previous
    # versions of this fixture deleted before yielding, which made any
    # test depending on `shared_clone_profile["id"]` fail with HTTP 404
    # "Profile not found" because the profile was already gone.
    yield profile

    try:
        requests.delete(
            f"{voicebox_available}/profiles/{profile_id}",
            headers=_vb_headers(),
            timeout=10,
        )
    except requests.RequestException:
        pass


# ---------------------------------------------------------------------------
# OpenMontage MCP availability (for the reverse-proxy integration layer)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def openmontage_mcp_base_url() -> str:
    """OpenMontage MCP server base URL (host:port, no path).

    The server mounts two paths on this base:
      - /mcp/         OpenMontage's own FastMCP server
      - /voicebox/mcp/ Reverse proxy to voicebox's MCP server
    Tests construct the full path they need; this fixture just resolves
    the host:port from env.
    """
    return os.environ.get("OM_MCP_URL", OM_DEFAULT_BASE_URL).rstrip("/")


@pytest.fixture(scope="session")
def openmontage_mcp_token() -> str:
    """Bearer token for OpenMontage MCP. Read from .env or env var.

    The repo's .env loader lives in tools/base_tool.py but importing it
    pulls in heavy dependencies we don't need for HTTP-only integration
    tests. Instead we read .env directly with a minimal parser --
    `KEY=value` per line, `# comments`, optional quotes. python-dotenv is
    not a dependency of this repo so we don't rely on it.
    """
    env_path = PROJECT_ROOT / ".env"
    if env_path.is_file():
        for raw in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    token = os.environ.get("MCP_API_TOKEN")
    if not token:
        pytest.skip("MCP_API_TOKEN not set -- cannot exercise reverse-proxy path")
    return token


@pytest.fixture(scope="session")
def openmontage_mcp_available(
    openmontage_mcp_base_url: str, openmontage_mcp_token: str
) -> str:
    """Verify OpenMontage MCP server responds to initialize. Returns the
    full /mcp/ URL on success. Tests that need the reverse-proxy path
    build the /voicebox/mcp/ URL from this base.
    """
    url = openmontage_mcp_base_url.rstrip("/") + "/mcp/"
    try:
        resp = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {openmontage_mcp_token}",
            },
            json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "voicebox-integration-test", "version": "0"},
                },
            },
            timeout=5,
        )
    except requests.RequestException as exc:
        pytest.skip(f"OpenMontage MCP not reachable at {url}: {exc}")
    if resp.status_code != 200:
        pytest.skip(f"OpenMontage MCP initialize returned {resp.status_code}")
    return url


# ---------------------------------------------------------------------------
# MCP session helper
# ---------------------------------------------------------------------------

def open_mcp_session(
    url: str,
    headers: dict[str, str],
    client_name: str = "voicebox-integration-test",
) -> requests.Session:
    """Open a JSON-RPC MCP session and return a configured requests.Session.

    FastMCP requires:
      1. POST initialize  -> Mcp-Session-Id header is set on response
      2. notifications/initialized  (no response expected)
      3. tools/call with Mcp-Session-Id header preserved

    The returned requests.Session has the session id baked into its default
    headers so subsequent calls don't have to thread it through.
    """
    sess = requests.Session()
    sess.headers.update(headers)

    init_resp = sess.post(
        url,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": client_name, "version": "0"},
            },
        },
        timeout=10,
    )
    init_resp.raise_for_status()
    session_id = init_resp.headers.get("mcp-session-id")
    if not session_id:
        raise RuntimeError(
            f"MCP server at {url} did not return Mcp-Session-Id header "
            f"(status={init_resp.status_code}, body={init_resp.text[:200]})"
        )
    sess.headers["Mcp-Session-Id"] = session_id

    # Fire-and-forget initialized notification -- FastMCP requires it before
    # any tools/call. No response body expected.
    sess.post(
        url,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=5,
    )
    return sess


def call_mcp_tool(
    sess: requests.Session, url: str, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Invoke a MCP tool and return the parsed JSON-RPC result dict.

    FastMCP returns Server-Sent Events by default (Accept includes
    text/event-stream). Even when the client asks for JSON, the response
    body is an SSE stream of `event: message\\ndata: {...}\\n\\n` lines.
    We scan the body for the JSON-RPC payload rather than relying on
    `resp.json()`. Raises on a JSON-RPC `error` field or non-2xx HTTP.
    """
    resp = sess.post(
        url,
        json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        timeout=300,
    )
    resp.raise_for_status()
    payload = _extract_jsonrpc_payload(resp.text)
    if "error" in payload:
        raise RuntimeError(f"MCP tool {name} returned error: {payload['error']}")
    return payload.get("result", {})


def _extract_jsonrpc_payload(body: str) -> dict[str, Any]:
    """Pull the JSON-RPC dict out of an SSE / mixed response body.

    FastMCP's default response framing is:
        event: message
        data: {"jsonrpc":"2.0","id":2,"result":...}
        (blank line)
    Some servers reply with a bare JSON object instead (no SSE framing).
    We try the SSE path first, then fall back to bare JSON, so tests
    don't care which the server prefers.
    """
    # SSE path -- scan all `data:` lines, last one wins (FastMCP only
    # emits one but be tolerant).
    last_data: str | None = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            last_data = line[len("data:"):].strip()
    if last_data:
        try:
            parsed = json.loads(last_data)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Bare JSON path -- in case the server replies without SSE framing.
    body_stripped = body.strip()
    if body_stripped.startswith("{"):
        try:
            parsed = json.loads(body_stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise RuntimeError(
        f"could not parse JSON-RPC payload from MCP response body: "
        f"{body[:200]!r}"
    )


@pytest.fixture
def mcp_session_to_voicebox(voicebox_available: str):
    """Open a MCP session directly against voicebox (:17493/mcp)."""
    # voicebox mounts MCP at /mcp with a trailing slash required by FastMCP.
    # The mcp_session helper above does NOT add the trailing slash, so we
    # normalize here.
    url = voicebox_available.rstrip("/") + "/mcp/"
    sess = open_mcp_session(
        url,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Voicebox-Client-Id": VOICEBOX_CLIENT_ID,
        },
        client_name="voicebox-integration-test-direct",
    )
    yield sess, url
    sess.close()


@pytest.fixture
def mcp_session_to_openmontage(
    openmontage_mcp_available: str, openmontage_mcp_token: str
):
    """Open an MCP session through OpenMontage's reverse proxy at
    <base>/voicebox/mcp/. The /voicebox/* path in mcp_server.py forwards
    the request to voicebox's /mcp/, stripping Authorization and
    injecting X-Voicebox-Client-Id.

    Proves the Bearer auth + X-Voicebox-Client-Id injection chain
    end-to-end.
    """
    # Strip the trailing /mcp/ from the openmontage path so we can append
    # /voicebox/mcp/ instead. The reverse proxy is mounted at /voicebox/*,
    # not under /mcp/.
    base = openmontage_mcp_available.rstrip("/").removesuffix("/mcp")
    url = f"{base}/voicebox/mcp/"
    sess = open_mcp_session(
        url,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {openmontage_mcp_token}",
        },
        client_name="voicebox-integration-test-via-om",
    )
    yield sess, url
    sess.close()


# ---------------------------------------------------------------------------
# Voicebox generation status polling (used by REST text_to_speech tests)
# ---------------------------------------------------------------------------

def wait_for_generation(
    base_url: str, generation_id: str, timeout_s: int = TTS_TIMEOUT_S
) -> dict[str, Any]:
    """Block until voicebox reports a terminal status for `generation_id`.

    Polls /generate/{id}/status SSE stream and reads events until status
    is `completed` or `failed`. Mirrors the production polling loop in
    tools/audio/voicebox_tts.py::_wait_for_generation but is kept local to
    the test layer so the production code stays free of test-only paths.

    Returns the terminal status payload dict; raises on failure / timeout.
    """
    import time
    deadline = time.monotonic() + timeout_s
    last_status: str = "queued"
    with requests.get(
        f"{base_url}/generate/{generation_id}/status",
        headers={**_vb_headers(), "Accept": "text/event-stream"},
        timeout=(10, 6),
        stream=True,
    ) as resp:
        resp.raise_for_status()
        for raw in resp.iter_lines(chunk_size=1):
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"voicebox generation {generation_id} still {last_status!r} "
                    f"after {timeout_s}s"
                )
            # requests returns bytes from iter_lines by default. Decode
            # defensively -- the SSE spec is text but voicebox's chunks
            # arrive as bytes from the underlying urllib3 stream.
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="ignore")
            if not raw or not raw.startswith("data:"):
                continue
            try:
                payload = json.loads(raw[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            last_status = payload.get("status") or last_status
            if last_status == "completed":
                return payload
            if last_status == "failed":
                raise RuntimeError(
                    f"voicebox generation failed: {payload.get('error') or payload}"
                )
            if last_status == "not_found":
                raise RuntimeError(f"voicebox generation {generation_id} not found")
    raise TimeoutError(f"SSE stream closed at status={last_status!r} after {timeout_s}s")


@pytest.fixture(scope="session")
def generation_poller(voicebox_available: str):
    """Returns a callable that blocks on a generation_id."""
    def _poll(gen_id: str, timeout_s: int = TTS_TIMEOUT_S) -> dict[str, Any]:
        return wait_for_generation(voicebox_available, gen_id, timeout_s)
    return _poll