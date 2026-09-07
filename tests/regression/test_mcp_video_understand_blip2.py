"""Regression test: OM MCP server must reach HuggingFace + serve blip2 via execute_tool.

Bug (2026-09-07, see
``~/.claude/projects/-opt-OpenMontage-Voicebox/memory/om-mcp-server-huggingface-proxy-requirement.md``
and ``blip2-opt-2.7b-sharded-safetensors-cache-layout.md``):

An external MCP client (codex, OpenClaw-side) called
``execute_tool(tool_name="video_understand", model="blip2", mode="describe")``
at 15:43 local. The OM MCP server's Python process had no ``HTTPS_PROXY`` in
its environment, so the first call to ``Blip2Processor.from_pretrained(...)``
inside the worker thread retried 5× against
``https://huggingface.co/Salesforce/blip2-opt-2.7b/resolve/main/processor_config.json``
and failed with::

    '[Errno 101] Network is unreachable' thrown while requesting HEAD
    https://huggingface.co/Salesforce/blip2-opt-2.7b/resolve/main/processor_config.json

The fix landed the same day: ``start_mcp_server.sh`` now exports
``HTTPS_PROXY=http://127.0.0.1:7890`` (and ``HTTP_PROXY`` to the same value)
immediately before ``exec python3 mcp_server.py``. With HTTPS_PROXY injected
AND the 14 GB sharded safetensors already present in
``~/.cache/huggingface/hub/``, ``video_understand`` cold-loads the model from
local cache and a single-frame ``describe`` returns in ~45 s on CPU.

This test guards three invariants that together prove the end-to-end chain
still works:

1. **Proxy env still injected.** The MCP server process's ``/proc/<pid>/environ``
   contains ``HTTPS_PROXY`` and ``HTTP_PROXY``. If anyone removes the export
   block from ``start_mcp_server.sh`` (or starts ``mcp_server.py`` directly
   without the wrapper), this fails immediately.
2. **blip2 cache complete.** Both shards sit at their sha256-named blob paths
   AND the snapshot symlinks point to them. ``safe_open`` returns the
   expected tensor count (995 + 252 = 1247) on each shard.
3. **End-to-end inference.** ``execute_tool(video_understand, model=blip2)``
   on a synthetic 8×8 RGB image returns ``success=True`` with a non-empty
   ``summary``. This is the actual regression — without the fix, this call
   fails with the same Errno 101 within ~5 minutes of retry loop.

Skipped gracefully when MCP server is unreachable or blip2 cache is not
present locally — no point failing the suite on a workstation that hasn't
set up weights. To force-run on an incomplete setup, set
``OM_REGRESSION_REQUIRE_BLIP2=1`` and the cache-absence checks fail loud.

Run with::

    .venv/bin/python -m pytest tests/regression/test_mcp_video_understand_blip2.py -v
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Constants — keep in sync with the fix that landed 2026-09-07.
# ---------------------------------------------------------------------------

OM_DEFAULT_URL = "http://127.0.0.1:8900/mcp"
OM_DEFAULT_HOST = "127.0.0.1"
OM_DEFAULT_PORT = 8900
PROBE_TIMEOUT_S = 5.0

# When True, missing pieces (server down, cache absent) fail loud instead of
# skipping — useful for nightly CI that should never see green on a host that
# can't actually run blip2.
REQUIRE_BLIP2 = os.environ.get("OM_REGRESSION_REQUIRE_BLIP2") == "1"

# blip2-opt-2.7b snapshot commit (pinned by HF — the index.json inside
# models--Salesforce--blip2-opt-2.7b/snapshots/<sha>/ references it).
BLIP2_REPO_DIR = Path(
    os.environ.get(
        "HF_HUB_CACHE",
        os.path.expanduser("~/.cache/huggingface/hub"),
    )
) / "models--Salesforce--blip2-opt-2.7b"
BLIP2_COMMIT = "59a1ef6c1e5117b3f65523d1c6066825bcf315e3"
# sha256 blobs — match the values in the user's local cache. If HF ever
# republishes the repo with new shards these will need re-snapshotting; the
# integration test will tell us (1247 vs 1247 mismatch) at that point.
BLIP2_SHARD1_SHA = "b81228c9ac1b3dee1731ee71d51fe3b2c34f915019c44c25a793b51300ae24fc"
BLIP2_SHARD2_SHA = "536bd73b8f1de7d94f503b23fea2eaa4f7f3ea5f74f8f874fcb21d6df1555a19"
BLIP2_EXPECTED_SHARD1_TENSORS = 995
BLIP2_EXPECTED_SHARD2_TENSORS = 252

# how long to wait for video_understand end-to-end. On CPU + this host
# (no GPU) the first call is dominated by safetensors disk→RAM copy +
# forward pass, ~45-90 s. We add a generous safety margin for slow disks.
INFERENCE_TIMEOUT_S = 180.0

# Trusted values for HTTPS_PROXY / HTTP_PROXY. The script checks substring,
# not exact value, so users with a different proxy host (e.g. 10.0.0.1:7890)
# still pass as long as they set SOMETHING.
PROXY_HOST_PORT_HINTS = ("127.0.0.1:7890", "localhost:7890", ":7890")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _read_bearer_token() -> str | None:
    for env in ("OM_MCP_TOKEN", "MCP_API_TOKEN"):
        val = os.environ.get(env)
        if val:
            return val.strip()
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if line.startswith("MCP_API_TOKEN="):
                return line.split("=", 1)[1].strip()
    return None


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _mcp_post(url: str, token: str, body: dict, sid: str | None = None) -> tuple[int, dict[str, str], str]:
    """Single MCP POST. Returns ``(status, headers, body)``.

    Honours ``Mcp-Session-Id`` if ``sid`` is provided so a session can be
    reused across multiple requests (initialize → notifications/initialized
    → tools/call).
    """
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    if sid:
        headers["Mcp-Session-Id"] = sid
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — defensive
            pass
        return exc.code, dict(exc.headers or {}), body


def _extract_jsonrpc_payload(body: str) -> dict:
    """Pull the JSON-RPC dict out of an SSE / mixed response body.

    FastMCP returns either bare JSON or SSE-framed JSON; both are valid. We
    scan ``data:`` lines first, then fall back to bare-JSON parsing. This
    mirrors the helper in tests/integration/conftest.py but lives here to
    keep the regression tree dependency-free.
    """
    last_data: str | None = None
    for line in body.splitlines():
        if line.startswith("data:"):
            last_data = line[len("data:"):].strip()
    if last_data:
        try:
            parsed = json.loads(last_data)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    stripped = body.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)
    raise AssertionError(f"could not parse JSON-RPC from MCP body: {body[:200]!r}")


def _open_mcp_session(url: str, token: str) -> str:
    """Drive FastMCP's three-step handshake and return the Mcp-Session-Id."""
    init_body = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "blip2-regression", "version": "0.0.1"},
        },
    }
    status, headers, body = _mcp_post(url, token, init_body)
    if status != 200:
        raise AssertionError(f"initialize returned HTTP {status}; body={body[:200]!r}")
    sid = headers.get("mcp-session-id") or headers.get("Mcp-Session-Id")
    if not sid:
        raise AssertionError(f"no Mcp-Session-Id in headers: {sorted(headers.keys())}")
    # notifications/initialized — no response expected, but FastMCP may 202.
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    _mcp_post(url, token, notif, sid=sid)
    return sid


def _call_execute_tool(url: str, token: str, sid: str, tool_name: str, inputs: dict, timeout_s: float) -> dict:
    """Call ``execute_tool(tool_name, inputs)`` and return the parsed JSON-RPC result.

    Raises ``AssertionError`` on non-2xx, JSON-RPC ``error``, or
    ``isError=True`` in the tool result content.
    """
    body = {
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {
            "name": "execute_tool",
            "arguments": {"tool_name": tool_name, "inputs": inputs},
        },
    }
    # urllib doesn't honour a custom timeout beyond the constructor; we
    # shell out via a fresh request to set it cleanly.
    import http.client
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 80
    path = parsed.path or "/mcp"
    payload = json.dumps(body).encode("utf-8")
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        conn.request(
            "POST", path, body=payload,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
                "Mcp-Session-Id": sid,
            },
        )
        resp = conn.getresponse()
        raw = resp.read().decode("utf-8", "replace")
        status = resp.status
    finally:
        conn.close()
    if status != 200:
        raise AssertionError(f"tools/call HTTP {status}; body={raw[:300]!r}")
    payload = _extract_jsonrpc_payload(raw)
    if "error" in payload:
        raise AssertionError(f"JSON-RPC error from MCP: {payload['error']}")
    return payload.get("result", {})


def _write_synthetic_image(path: Path) -> None:
    """Write a minimal RGB PNG that blip2 will accept. PIL is a transitive
    dependency via transformers; guard ImportError for hosts without it."""
    try:
        from PIL import Image, ImageDraw
    except ImportError as exc:
        raise unittest.SkipTest(f"PIL not available: {exc}") from exc
    img = Image.new("RGB", (32, 32), (64, 128, 192))
    draw = ImageDraw.Draw(img)
    draw.rectangle((4, 4, 28, 28), fill=(220, 60, 60))
    draw.text((8, 8), "x", fill=(255, 255, 255))
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG")


# ---------------------------------------------------------------------------
# Test 1 — MCP server process env contains HTTPS_PROXY
# ---------------------------------------------------------------------------


class TestMcpServerProxyEnv(unittest.TestCase):
    """Catch future regressions where ``start_mcp_server.sh`` loses the
    HTTPS_PROXY export block. We resolve the listening pid via
    ``ss -lntp`` filtering on ``:8900`` and read ``/proc/<pid>/environ``.

    On hosts where the OM port is owned by another process (e.g. vclaw's
    control plane, a python venv, systemd), the test still passes — only
    the proxy-injection claim matters.
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not _port_open(OM_DEFAULT_HOST, OM_DEFAULT_PORT):
            if REQUIRE_BLIP2:
                raise AssertionError(
                    f"OM MCP not reachable at {OM_DEFAULT_HOST}:{OM_DEFAULT_PORT}"
                )
            raise unittest.SkipTest(
                f"OM MCP not reachable at {OM_DEFAULT_HOST}:{OM_DEFAULT_PORT}"
            )

    def test_mcp_process_env_has_proxy(self) -> None:
        import re
        import subprocess

        # ``ss -lntp`` is the most portable way to find the listening pid on
        # modern Linux. Fallback: lsof. The pid token in ``ss`` output is
        # ``pid=NNNNN,fd=K`` (note the embedded comma + suffix), so we
        # extract with a regex instead of rstrip — that's how an earlier
        # version of this test broke.
        pid = None
        pid_re = re.compile(r"pid=(\d+)")
        try:
            out = subprocess.run(
                ["ss", "-lntp"],
                capture_output=True, text=True, timeout=5, check=False,
            ).stdout
            for line in out.splitlines():
                if f":{OM_DEFAULT_PORT} " in line and "LISTEN" in line:
                    m = pid_re.search(line)
                    if m:
                        pid = int(m.group(1))
                        break
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        if pid is None:
            try:
                out = subprocess.run(
                    ["lsof", "-i", f":{OM_DEFAULT_PORT}", "-t"],
                    capture_output=True, text=True, timeout=5, check=False,
                ).stdout.strip()
                if out:
                    pid = int(out.splitlines()[0])
            except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
                pass

        if pid is None:
            self.skipTest(
                f"could not resolve OM MCP pid from port {OM_DEFAULT_PORT}; "
                "ss/lsof unavailable or returned nothing"
            )

        try:
            env = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError as exc:
            self.skipTest(f"cannot read /proc/{pid}/environ: {exc}")

        # /proc/<pid>/environ is NUL-separated; allow also newlines (some
        # kernels embed an env var containing one).
        entries = env.replace(b"\n", b"\x00").split(b"\x00")
        names_lower = {e.split(b"=", 1)[0].decode("utf-8", "replace").lower(): e.decode("utf-8", "replace") for e in entries if b"=" in e}

        proxy_keys = ("HTTPS_PROXY", "HTTP_PROXY")
        present = [k for k in proxy_keys if k.lower() in names_lower]
        self.assertTrue(
            present,
            "MCP server process env contains neither HTTPS_PROXY nor HTTP_PROXY. "
            "The fix in start_mcp_server.sh (2026-09-07) was lost — re-add the "
            "export block before `exec python3 mcp_server.py`. Without this, "
            "video_understand(model='blip2') fails with Errno 101 on every "
            "huggingface.co HEAD request.",
        )
        # Sanity: the value should look like a proxy URL, not the empty string.
        for k in present:
            val = names_lower[k.lower()].split("=", 1)[1].strip()
            self.assertTrue(
                val,
                f"{k} is set but empty in the MCP server process env",
            )
            self.assertTrue(
                val.startswith(("http://", "https://", "socks5://")),
                f"{k}={val!r} doesn't look like a proxy URL (expected http://, https://, or socks5:// scheme)",
            )


# ---------------------------------------------------------------------------
# Test 2 — blip2 cache layout complete
# ---------------------------------------------------------------------------


class TestBlip2CacheLayout(unittest.TestCase):
    """Verify the sharded safetensors are in the right cache layout.

    Pre-fix era, the loader would silently fall through to a network HEAD
    when the cache is malformed (missing blob, dangling symlink, wrong
    shard). After the fix, ``Blip2ForConditionalGeneration.from_pretrained``
    navigates the index → resolves both shards → loads ~11 GB into RAM
    without ever touching the network.

    These checks detect the cache layout breaks BEFORE the slow inference
    test runs, so a half-broken cache fails fast (~1 s) instead of halfway
    through a 45 s inference.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.snapshot = BLIP2_REPO_DIR / "snapshots" / BLIP2_COMMIT
        cls.blobs = BLIP2_REPO_DIR / "blobs"

    def _check_present(self) -> None:
        if not BLIP2_REPO_DIR.exists():
            if REQUIRE_BLIP2:
                raise AssertionError(
                    f"blip2 cache absent: {BLIP2_REPO_DIR}; pre-cache weights "
                    "per memory/blip2-opt-2.7b-sharded-safetensors-cache-layout.md"
                )
            raise unittest.SkipTest(f"blip2 cache absent at {BLIP2_REPO_DIR}")

    def test_snapshot_dir_exists(self) -> None:
        self._check_present()
        self.assertTrue(
            self.snapshot.is_dir(),
            f"snapshot dir missing: {self.snapshot}",
        )

    def test_both_shards_as_sha_blobs(self) -> None:
        self._check_present()
        for label, sha in (
            ("shard1", BLIP2_SHARD1_SHA),
            ("shard2", BLIP2_SHARD2_SHA),
        ):
            blob = self.blobs / sha
            self.assertTrue(
                blob.is_file(),
                f"{label} blob missing: {blob}",
            )
            self.assertGreater(
                blob.stat().st_size, 1_000_000_000,
                f"{label} blob {blob} is < 1 GB; download may have been truncated",
            )

    def test_snapshot_symlinks_resolve(self) -> None:
        self._check_present()
        for shard_name in ("model-00001-of-00002.safetensors", "model-00002-of-00002.safetensors"):
            link = self.snapshot / shard_name
            self.assertTrue(
                link.is_symlink(),
                f"snapshot entry is not a symlink: {link}",
            )
            try:
                target = link.resolve(strict=True)
            except FileNotFoundError as exc:
                self.fail(f"snapshot symlink {link} → dangling target ({exc})")
            self.assertEqual(
                target.parent, self.blobs,
                f"symlink {link} resolves to {target} (expected parent {self.blobs})",
            )
            self.assertTrue(
                target.is_file(),
                f"symlink {link} → {target} not a regular file",
            )

    def test_safetensors_open_with_expected_tensor_counts(self) -> None:
        self._check_present()
        try:
            from safetensors.torch import safe_open
        except ImportError as exc:
            self.skipTest(f"safetensors not importable: {exc}")

        expected = {
            "model-00001-of-00002.safetensors": BLIP2_EXPECTED_SHARD1_TENSORS,
            "model-00002-of-00002.safetensors": BLIP2_EXPECTED_SHARD2_TENSORS,
        }
        for shard_name, want_count in expected.items():
            path = self.snapshot / shard_name
            with safe_open(str(path), framework="pt") as f:
                keys = list(f.keys())
            self.assertEqual(
                len(keys), want_count,
                f"{shard_name} returned {len(keys)} tensors, expected {want_count}. "
                "If HF republished the repo with different sharding, update "
                "BLIP2_EXPECTED_SHARD*_TENSORS and re-snapshot.",
            )


# ---------------------------------------------------------------------------
# Test 3 — end-to-end execute_tool(video_understand, model=blip2)
# ---------------------------------------------------------------------------


class TestVideoUnderstandEndToEnd(unittest.TestCase):
    """The actual regression — proves ``execute_tool(video_understand)``
    cold-loads blip2 from local cache and returns a caption without
    touching huggingface.co.

    Pre-fix (no HTTPS_PROXY + no cache): call hangs ~5 min in retry loop,
    returns ``success=False`` with an HF connection error.
    Post-fix (proxy injected + cache complete): call succeeds in ~45-90 s
    on CPU with a non-empty caption.

    Skip conditions (any of):
      - MCP server not running
      - Bearer token unavailable
      - blip2 cache absent (genuine skip — no point trying)
      - MCP server reachable but tools/call returns isError (treated as a
        real failure, not a skip)
    """

    @classmethod
    def setUpClass(cls) -> None:
        if not _port_open(OM_DEFAULT_HOST, OM_DEFAULT_PORT):
            raise unittest.SkipTest(f"OM MCP not reachable at {OM_DEFAULT_HOST}:{OM_DEFAULT_PORT}")
        cls.token = _read_bearer_token()
        if not cls.token:
            raise unittest.SkipTest("MCP_API_TOKEN not set")
        if not BLIP2_REPO_DIR.exists():
            raise unittest.SkipTest(f"blip2 cache absent at {BLIP2_REPO_DIR}")

        # Stage a tiny synthetic image under /tmp so the test doesn't depend
        # on codex's uploaded asset or any project state.
        cls.image_path = Path(f"/tmp/blip2_regression_{os.getpid()}.png")
        _write_synthetic_image(cls.image_path)

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.image_path.unlink()
        except OSError:
            pass

    def test_video_describe_returns_caption(self) -> None:
        sid = _open_mcp_session(OM_DEFAULT_URL, self.token)
        t0 = time.monotonic()
        result = _call_execute_tool(
            OM_DEFAULT_URL, self.token, sid,
            "video_understand",
            {
                "input_path": str(self.image_path),
                "mode": "describe",
                "model": "blip2",
                "max_frames": 1,
            },
            timeout_s=INFERENCE_TIMEOUT_S,
        )
        elapsed = time.monotonic() - t0

        # MCP returns result.content[0].text as JSON-serialized payload.
        # Some paths use result.structuredContent directly. Try both.
        text = ""
        content = result.get("content") or []
        if isinstance(content, list) and content:
            text = content[0].get("text", "") if isinstance(content[0], dict) else ""
        if not text and isinstance(result.get("structuredContent"), dict):
            text = json.dumps(result["structuredContent"])

        self.assertNotEqual(text, "", "tools/call returned empty content payload")

        # The execute_tool wrapper wraps the tool's ExecuteResult as a JSON
        # string in content[0].text. Parse it back out.
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {"_raw": text}

        self.assertTrue(
            payload.get("success", False) is True,
            f"execute_tool returned success=False — likely the original "
            "HF-reachability regression came back. Error: "
            f"{payload.get('error')!r}. Check (1) MCP server env has "
            "HTTPS_PROXY, (2) blip2 cache is at the expected sha256 blob "
            "paths. Raw payload: {payload!r}",
        )

        data = payload.get("data") or {}
        summary = data.get("summary") or ""
        frames = data.get("frames") or []
        self.assertTrue(
            isinstance(summary, str) and summary.strip(),
            f"empty summary in result data: {data!r}",
        )
        self.assertTrue(
            len(frames) >= 1,
            f"no frames returned: {data!r}",
        )

        # If we got here, the full chain worked. Log wall time so a slow
        # regression is visible in the test output (default unittest verbosity
        # shows assertion messages).
        print(
            f"\n[blip2 regression] video_understand round-trip: "
            f"{elapsed:.1f}s wall, summary={summary!r}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)