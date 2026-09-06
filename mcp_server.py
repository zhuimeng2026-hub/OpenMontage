"""OpenMontage MCP Server — exposes tools, pipelines, and checkpoints over MCP.

Run with streamable-http (for remote access):
    python mcp_server.py

Run with stdio (for local agent integration):
    python mcp_server.py stdio
"""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import sys
import threading
import time
import traceback
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal, Optional
from urllib.parse import urlparse

import httpx

# Ensure OpenMontage project root is on sys.path so tools/ and lib/ resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Change CWD so relative paths (pipeline/, projects/, output/) resolve correctly.
os.chdir(_PROJECT_ROOT)

# Logging setup (must be after _PROJECT_ROOT is defined)
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(exist_ok=True)
_log = logging.getLogger("mcp_server")
_log.setLevel(logging.INFO)
_formatter = logging.Formatter("[%(asctime)s] %(levelname)s %(message)s", datefmt="%H:%M:%S")

# File handler with rotation (10 MB per file, keep 5 backups).
# If the primary log file is locked by another process (e.g. a log viewer
# holding an exclusive handle on Windows), fall back to a timestamped file
# instead of crashing at startup.
if not _log.handlers:
    _log_candidates = [
        _LOG_DIR / "mcp_server.log",
        _LOG_DIR / f"mcp_server_{int(time.time())}.log",
    ]
    _file_handler: Optional[logging.Handler] = None
    for _lp in _log_candidates:
        try:
            _file_handler = RotatingFileHandler(
                _lp,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            break
        except (PermissionError, OSError):
            continue
    if _file_handler is None:
        _file_handler = logging.NullHandler()
    _file_handler.setFormatter(_formatter)
    _log.addHandler(_file_handler)

    # Stderr handler (for journal/systemd)
    _stderr_handler = logging.StreamHandler(sys.stderr)
    _stderr_handler.setFormatter(_formatter)
    _log.addHandler(_stderr_handler)

# ---------------------------------------------------------------------------
# 独立健康日志（logs/mcp_health.log）—— 供外部检测运行状态
# ---------------------------------------------------------------------------
# 与 mcp_server.log（全量请求/业务日志）分离，只写高信号、结构化的
# `event=... key=value` 行，便于 grep/tail/监控脚本判断进程是否健康：
#   - event=heartbeat status=ok ...  每 30s 心跳，停止即异常
#   - event=tool_sync state=submit|done|timeout tool=...  工具执行生命周期与耗时
#   - event=executor_wedge / event=executor_replaced ...  to_thread 卡死自愈记录
# 采用与 mcp_server.log 相同的锁文件回退策略（文件被占用时写时间戳文件，
# 仍不可用时退化 NullHandler），避免在 Windows 下因日志句柄被占而启动崩溃。
_health_log = logging.getLogger("mcp_health")
_health_log.setLevel(logging.INFO)
_health_log.propagate = False
if not _health_log.handlers:
    _health_candidates = [
        _LOG_DIR / "mcp_health.log",
        _LOG_DIR / f"mcp_health_{int(time.time())}.log",
    ]
    _health_handler: Optional[logging.Handler] = None
    for _hp in _health_candidates:
        try:
            _health_handler = RotatingFileHandler(
                _hp,
                maxBytes=10 * 1024 * 1024,
                backupCount=5,
                encoding="utf-8",
            )
            break
        except (PermissionError, OSError):
            continue
    if _health_handler is None:
        _health_handler = logging.NullHandler()
    _health_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S"
    ))
    _health_log.addHandler(_health_handler)

_PROCESS_START = time.time()
_tool_pending_lock = threading.Lock()
_tool_pending: dict[str, int] = {}  # tool name -> 在飞工具调用数（submit++ / done--）


def _health(event: str, **fields) -> None:
    """写一行结构化健康日志；失败静默（不影响业务）。"""
    try:
        parts = [f"event={event}"]
        parts += [f"{k}={v}" for k, v in fields.items() if v is not None]
        _health_log.info(" ".join(parts))
    except Exception:  # noqa: BLE001 - 健康日志绝不允许拖垮业务
        pass


def _pending_tool_calls() -> dict[str, int]:
    with _tool_pending_lock:
        return dict(_tool_pending)


def _health_bump_tool(name: str, delta: int) -> None:
    with _tool_pending_lock:
        _tool_pending[name] = max(0, _tool_pending.get(name, 0) + delta)

# Ensure OpenMontage project root is on sys.path so tools/ and lib/ resolve.
_PROJECT_ROOT = Path(__file__).resolve().parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# Change CWD so relative paths (pipeline/, projects/, output/) resolve correctly.
os.chdir(_PROJECT_ROOT)

from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field

from tools.tool_registry import registry
from tools.base_tool import ToolStatus
from lib import checkpoint as ckpt
from lib import paths as lib_paths
from lib import pipeline_loader
from lib.effects_parser import (
    apply_effects_to_edit_decisions,
    effects_animation_for_cut,
    parse_effects_segments,
    segment_animation,
)
from lib.mcp_session import (
    get_mcp_request_id,
    get_mcp_session_id,
    reset_mcp_request_id,
    reset_mcp_session_id,
    set_mcp_request_id,
    set_mcp_session_id,
)
from lib.workbuddy_session import (
    begin_render,
    find_asset,
    session_hash,
    update as update_session,
    find_session_by_job_id,
    update_session_by_job_id,
    recover_orphans_and_rebuild_index,
    fail_job_by_id,
)
from lib.render_progress import publish, progress_event, subscribe, unsubscribe
from lib.media_job_store import create_job as create_media_job
from lib.media_job_store import get_job as get_media_job
from lib.media_job_store import update_job as update_media_job
from lib.media_job_store import recover_incomplete_jobs as recover_media_jobs
from lib.render_queue import (
    fair_render_queue_snapshot,
    get_render_queue,
    save_job_record,
    delete_job_record,
    load_job_record,
    all_job_records,
)
from lib.user_auth import default_user_store
from lib.web_auth_app import build_web_mount

# Phase B of user isolation — durable session→principal registry.
# The registry is the authoritative source across ASGI task boundaries;
# ContextVar below remains as a fast-path cache (Phase 3 semantics).
from lib.principal_registry import (  # noqa: E402  (depends on ContextVar)
    Principal as _Principal,
    PrincipalNotFound as _PrincipalNotFound,
    PrincipalOwnerConflict as _PrincipalOwnerConflict,
    get_mcp_session_id_from_scope as _get_session_id_from_scope,
    get_mcp_session_header_from_scope as _get_session_header_from_scope,
)
import lib.principal_registry as _principal_registry


# ---------------------------------------------------------------------------
# VClaw user-id ContextVar (Phase 3 of user isolation via MCP session)
# ---------------------------------------------------------------------------
# vclaw is the source of truth for user identity and emits an opaque
# ``X-VClaw-User-Id`` plus a dedicated HMAC assertion on every attributed MCP
# request it proxies. Possession of the shared ``MCP_API_TOKEN`` alone is not
# sufficient to claim a user; see ``BearerTokenAuthMiddleware.__call__``.
_user_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "vclaw_user_id", default=None
)
_initialize_request_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "mcp_initialize_request", default=False
)

_ASSERTION_HEADER = b"x-vclaw-user-assertion"
_ASSERTION_VERSION = "v1"
_ASSERTION_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_assertion_replay_lock = threading.Lock()
_assertion_replay: dict[str, float] = {}


def _assertion_secret() -> bytes | None:
    raw = os.environ.get("OPENMONTAGE_VCLAW_ASSERTION_SECRET", "")
    return raw.encode("utf-8") if raw else None


def _assertion_max_age() -> int:
    raw = os.environ.get("OPENMONTAGE_VCLAW_ASSERTION_MAX_AGE_SECONDS", "60")
    try:
        value = int(raw)
    except ValueError:
        return 60
    return max(1, min(value, 300))


def _verify_vclaw_assertion(
    raw_uid: bytes,
    raw_assertion: bytes,
    *,
    method: str,
    path: str,
    session_id: str,
    body: bytes,
) -> str | None:
    """Verify a vclaw HMAC user assertion bound to this exact request.

    The ordinary MCP bearer token is intentionally insufficient for user
    attribution: direct callers may know it but cannot mint this dedicated
    assertion.  Timestamp + nonce + method/path/session/body binding provides
    bounded replay protection and prevents copying an assertion to another
    tool call.
    """
    secret = _assertion_secret()
    if secret is None or not isinstance(raw_uid, bytes) or not isinstance(raw_assertion, bytes):
        return None
    uid = _sanitize_vclaw_user_id(raw_uid)
    if uid is None or uid.encode("ascii") != raw_uid:
        return None
    try:
        text = raw_assertion.decode("ascii")
        version, stamp_text, nonce, supplied = text.split(".", 3)
        stamp = int(stamp_text)
    except (UnicodeDecodeError, ValueError):
        return None
    if version != _ASSERTION_VERSION or not _ASSERTION_NONCE_RE.fullmatch(nonce):
        return None
    if len(supplied) != 64 or not re.fullmatch(r"[0-9a-f]{64}", supplied):
        return None
    if abs(time.time() - stamp) > _assertion_max_age():
        return None
    canonical = "\n".join((
        _ASSERTION_VERSION,
        uid,
        str(stamp),
        nonce,
        method,
        path,
        session_id,
        hashlib.sha256(body).hexdigest(),
    )).encode("utf-8")
    expected = hmac.new(secret, canonical, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, supplied):
        return None
    now = time.time()
    with _assertion_replay_lock:
        cutoff = now - _assertion_max_age()
        for key, seen in list(_assertion_replay.items()):
            if seen < cutoff:
                _assertion_replay.pop(key, None)
        if nonce in _assertion_replay:
            return None
        _assertion_replay[nonce] = now
    return uid


def _is_initialize_body(body: bytes) -> bool:
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("method") == "initialize"


def _has_duplicate_security_headers(scope: dict) -> bool:
    """Reject ambiguous duplicate security headers before dict collapsing."""
    names = {
        b"authorization",
        b"mcp-session-id",
        b"x-vclaw-user-id",
        b"x-vclaw-user-assertion",
    }
    counts: dict[bytes, int] = {}
    for raw_key, _ in scope.get("headers") or []:
        if isinstance(raw_key, bytes):
            key = raw_key.lower()
            if key in names:
                counts[key] = counts.get(key, 0) + 1
    return any(count > 1 for count in counts.values())

# Allow-list matches vclaw's internal ``users.id`` shape: opaque token,
# ~32 chars, charset [a-zA-Z0-9._-]. The validation now lives in
# ``lib.principal_sanitize.sanitize_principal_id`` so the Phase B registry
# (``lib.principal_registry.bind``) and the Phase 3 middleware share one
# definition. Any character outside the set is silently dropped (with a
# warning) — never raise on a malformed header, because that would let a
# hostile header block legitimate traffic.
from lib.principal_sanitize import (  # noqa: E402  (after _user_id_ctx for clarity)
    MAX_PRINCIPAL_ID_LEN as _max_user_id_len,
    sanitize_principal_id as _sanitize_vclaw_user_id,
)


def current_user_id() -> Optional[str]:
    """Return the ``X-VClaw-User-Id`` value bound to the current request, or
    ``None`` if the request was service-token (no user attribution).

    Available to tool implementations starting in Phase 4. Never raise.
    Note: phase B added ``current_principal()`` (returns a ``Principal``);
    this function stays for callers that only want the id string. Do NOT
    change its semantics — tools use it as a fast-path cache while
    ``current_principal()`` is the recommended entry point.
    """
    return _user_id_ctx.get()


def current_principal() -> _Principal:
    """Return the authenticated principal for the current MCP session.

    Phase B (durable registry). Lookup order matches the spec:

    1. An existing ``Mcp-Session-Id`` is resolved from the durable registry,
       preserving its stored namespace across key rotation. Missing or
       unverifiable bindings fail closed; the ContextVar cannot retarget a
       session.
    2. Only the first ``initialize`` request (without a session id) may use
       the Phase 3 ContextVar fast-path.
    3. Raise ``PrincipalNotFound`` when the applicable source misses.

    Tenant id is not exposed by any v1 / Phase B header, so it is always
    ``None`` until vclaw adds tenant-preserving refresh tokens.
    """
    sid = get_mcp_session_id()
    if sid:
        # An existing MCP session has a durable identity binding. Never fall
        # back to the request ContextVar when the row is missing or fails
        # namespace verification: that would allow a session id to be paired
        # with a different user's fast-path header.
        registered = _principal_registry.lookup(sid)
        if registered is None:
            raise _PrincipalNotFound(
                "mcp session id has no valid durable principal binding"
            )
        return registered
    fast = current_user_id()
    if fast and _initialize_request_ctx.get():
        # No session id means this is the initial initialize request. The
        # middleware has authenticated and sanitized this user id; it is safe
        # only for this pre-session handshake.
        return _Principal(kind="user", principal_id=fast, tenant_id=None)
    if not sid:
        raise _PrincipalNotFound(
            "no mcp session id and no Phase 3 fast-path user id"
        )


def get_mcp_session_id_from_scope(scope: dict) -> Optional[str]:
    """Re-export of ``lib.principal_registry.get_mcp_session_id_from_scope``.

    Phase B moved the header read here so the middleware path and any test
    helper that constructs an ASGI scope by hand share one definition.
    Return ``None`` when the header is missing or fails sanitisation; both
    hostile and pre-handshake values collapse to the same ``None``.
    """
    return _get_session_id_from_scope(scope)


# ---------------------------------------------------------------------------
# Session-id propagation fix (streamable-http, stateful transport)
# ---------------------------------------------------------------------------
# FastMCP's stateful StreamableHTTP runs *every* tool call for a session inside a
# per-session background task (StreamableHTTPSessionManager.run_server) — NOT
# inside the per-request ASGI task. A ContextVar set by BearerTokenAuthMiddleware
# (per-request task) therefore never reaches the tools, which is exactly why the
# old "Streamable HTTP Mcp-Session-Id is required" error survived every ContextVar
# fix. The transport's connect() is entered *inside* that background task and
# wraps the whole self.app.run(...) message loop, so setting the session
# ContextVar there makes it visible to every tool call processed for the session.
try:
    from contextlib import asynccontextmanager
    from mcp.server.streamable_http import StreamableHTTPServerTransport as _SHT

    _orig_connect = _SHT.connect

    @asynccontextmanager
    async def _patched_connect(self):
        sid = getattr(self, "mcp_session_id", None)
        token = set_mcp_session_id(sid) if sid else None
        try:
            async with _orig_connect(self) as streams:
                yield streams
        finally:
            if token is not None:
                reset_mcp_session_id(token)

    _SHT.connect = _patched_connect
except Exception as _patch_err:  # pragma: no cover - defensive
    _log.warning("Session-id connect() patch failed: %s", _patch_err)


_business_log = logging.getLogger("session_video")
_business_log.setLevel(logging.INFO)
_business_log.propagate = False
if not _business_log.handlers:
    _business_candidates = [
        _LOG_DIR / "session_video.log",
        _LOG_DIR / f"session_video_{int(time.time())}.log",
    ]
    _business_handler: Optional[logging.Handler] = None
    for _blp in _business_candidates:
        try:
            _business_handler = RotatingFileHandler(
                _blp, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
            )
            break
        except (PermissionError, OSError):
            continue
    if _business_handler is None:
        _business_handler = logging.NullHandler()
    _business_handler.setFormatter(logging.Formatter("%(message)s"))
    _business_log.addHandler(_business_handler)


# ---------------------------------------------------------------------------
# Dedicated decompose-path logger (logs/decompose.log) — channel-separated
# from session_video.log (full business events) and mcp_health.log (server
# liveness). Consumed by tools/decompose_health_monitor.py Probe B.
# ---------------------------------------------------------------------------
_decompose_log = logging.getLogger("decompose")
_decompose_log.setLevel(logging.INFO)
_decompose_log.propagate = False
if not _decompose_log.handlers:
    _decompose_candidates = [
        _LOG_DIR / "decompose.log",
        _LOG_DIR / f"decompose_{int(time.time())}.log",
    ]
    _decompose_handler: Optional[logging.Handler] = None
    for _dlp in _decompose_candidates:
        try:
            _decompose_handler = RotatingFileHandler(
                _dlp, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8",
            )
            break
        except (PermissionError, OSError):
            continue
    if _decompose_handler is None:
        _decompose_handler = logging.NullHandler()
    _decompose_handler.setFormatter(logging.Formatter("%(message)s"))
    _decompose_log.addHandler(_decompose_handler)


def _decompose_event(event: str, **fields: Any) -> None:
    try:
        parts = [f"event={event}"]
        parts += [f"{k}={v}" for k, v in fields.items() if v is not None]
        parts.insert(1, f"ts={time.time()}")
        _decompose_log.info(" ".join(parts))
    except Exception:  # noqa: BLE001
        pass


def _event(event: str, *, include_traceback: bool = False, **fields: Any) -> None:
    record = {"event": event, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    if include_traceback:
        record["traceback"] = traceback.format_exc()
    record = _safe_inputs(record)
    level = logging.ERROR if event == "workflow_failed" else logging.INFO
    _business_log.log(level, json.dumps(record, ensure_ascii=False, separators=(",", ":")))


def _safe_inputs(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: ("<redacted>" if key.lower() in {"content_base64", "chunk_base64", "token", "cookie", "authorization", "mcp_session_id"} else _safe_inputs(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [_safe_inputs(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", value)
        value = re.sub(
            r"(?i)((?:api[_-]?key|token|cookie|authorization)\s*[:=]\s*)[^\s,;]+",
            r"\1<redacted>",
            value,
        )
    return value


async def _run_tool_sync(tool, inputs: dict[str, Any]) -> Any:
    """Execute a blocking ``BaseTool`` in a worker thread while preserving the
    MCP session/request ContextVars.

    FastMCP dispatches synchronous ``@mcp.tool()`` functions to a worker thread
    (via ``asyncio.to_thread`` / ``anyio.to_thread``), and those helpers do not
    copy ContextVars. A session id set in the ASGI middleware would therefore be
    invisible to code running inside the thread, making ``get_mcp_session_id()``
    return ``None`` (the "Mcp-Session-Id is required" failure).

    Capturing the context explicitly here keeps the session/request ids alive
    across the hop — mirroring the fix already applied inside ``execute_tool``.
    """
    name = getattr(tool, "name", type(tool).__name__)
    _log.info("tool.sync.submit name=%s", name)
    _health("tool_sync", state="submit", tool=name)
    _health_bump_tool(name, +1)
    _started_at = time.monotonic()
    ctx = contextvars.copy_context()
    fut = asyncio.to_thread(ctx.run, tool.execute, inputs)
    # 安全网：即使底层线程池/执行器异常卡死，也不会让请求永久挂起——
    # 超时后向上抛错，FastMCP 会回 500，客户端可感知并重试而非无限等待。
    try:
        result = await asyncio.wait_for(fut, timeout=900)
    except asyncio.TimeoutError:
        elapsed_ms = round((time.monotonic() - _started_at) * 1000)
        _log.error("tool.sync.timeout name=%s (executor wedge?) — abandoning call", name)
        _health("tool_sync", state="timeout", tool=name, elapsed_ms=elapsed_ms)
        _health_bump_tool(name, -1)
        raise
    elapsed_ms = round((time.monotonic() - _started_at) * 1000)
    _log.info("tool.sync.done name=%s elapsed_ms=%d", name, elapsed_ms)
    _health("tool_sync", state="done", tool=name, elapsed_ms=elapsed_ms)
    _health_bump_tool(name, -1)
    return result


def _start_executor_health_monitor() -> asyncio.Task:
    """在 MCP 主事件循环上启动默认 executor 健康自愈监控。

    背景：长驻进程曾出现 ``asyncio.to_thread`` 默认 executor 卡死——
    dispatch 已写日志但工具永远不被任何 worker 拾取（事件循环仍活、多个
    worker 全空闲），外部 upload_asset_chunk 全部挂起，只能靠重启恢复。
    此处每 30s 用空操作探测默认 executor；一旦超时即替换为新 executor，
    使后续工具调用立即恢复，无需人工重启。返回主循环上的监控 task。
    """
    import concurrent.futures as _cf

    async def _monitor(loop: asyncio.AbstractEventLoop) -> None:
        _log.info("executor.health.monitor started on loop %r", loop)
        _health("executor_monitor", state="started", loop=str(loop))
        while True:
            await asyncio.sleep(30)
            executor = getattr(loop, "_default_executor", None)
            threads = len(getattr(executor, "_threads", ())) if executor is not None else 0
            pending = _pending_tool_calls()
            uptime_s = round(time.time() - _PROCESS_START)
            if executor is None:
                _health("heartbeat", status="no_executor_yet", uptime_s=uptime_s,
                        tool_pending=sum(pending.values()))
                continue  # 尚未创建默认 executor，无从探测
            try:
                fut = loop.run_in_executor(executor, lambda: None)
                await asyncio.wait_for(fut, timeout=8)
                _log.debug("executor.health.ok threads=%d", threads)
                _health("heartbeat", status="ok", executor_threads=threads,
                        tool_pending=sum(pending.values()),
                        tool_pending_detail=",".join(f"{k}:{v}" for k, v in pending.items()),
                        uptime_s=uptime_s)
            except asyncio.TimeoutError:
                _log.error(
                    "executor.health.wedge default executor unresponsive (submit no-op "
                    "not picked up in 8s; workers=%d) — replacing it",
                    threads,
                )
                _health("executor_wedge", executor_threads=threads,
                        tool_pending=sum(pending.values()), uptime_s=uptime_s)
                try:
                    loop.set_default_executor(_cf.ThreadPoolExecutor(
                        max_workers=32, thread_name_prefix="asyncio",
                    ))
                    _log.warning("executor.health.replaced default executor")
                    _health("executor_replaced", old_executor_threads=threads,
                            uptime_s=uptime_s)
                except Exception as exc:  # noqa: BLE001
                    _log.exception("executor.health.replace failed: %s", exc)
                    _health("executor_replace_failed", error=str(exc))
            except Exception as exc:  # noqa: BLE001
                _log.warning("executor.health.check error: %s", exc)
                _health("executor_check_error", error=str(exc))

    loop = asyncio.get_running_loop()
    task = loop.create_task(_monitor(loop))
    return task


# ---------------------------------------------------------------------------
# Discover tools at import time
# ---------------------------------------------------------------------------
_discovered = registry.discover()
# Do not probe every provider during import. Several status checks intentionally
# inspect local/network services (ComfyUI, npm/HyperFrames, GPU backends); doing
# that eagerly makes importing the MCP module block before tests or the server
# can start. Availability is queried lazily by the provider menu/preflight.
_AVAILABLE = None
print(
    f"[mcp_server] Discovered {len(_discovered)} tools (availability deferred)",
    file=sys.stderr,
)

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------
mcp = FastMCP(
    "OpenMontage",
    host="::",
    port=int(os.environ.get("MCP_PORT", "8900")),
    # Keep the MCP transport stateful so clients receive and reuse
    # Mcp-Session-Id.  The session id is also used to isolate uploads.
    stateless_http=False,
    json_response=True,
    instructions=(
        "OpenMontage agentic video production system. "
        "Use list_tools to discover available tools, execute_tool to run them, "
        "list_pipelines to see production workflows, and checkpoint tools to "
        "manage pipeline state. Every tool call returns a structured result "
        "with success, data, artifacts, cost_usd, and duration_seconds."
    ),
)

# ---------------------------------------------------------------------------
# Pydantic return models
# ---------------------------------------------------------------------------


class ToolSummary(BaseModel):
    name: str = Field(description="Tool name")
    capability: str = Field(description="What the tool does (e.g. tts, image_generation)")
    provider: str = Field(description="Service provider (e.g. elevenlabs, ffmpeg, fal)")
    status: str = Field(description="available | unavailable | degraded")
    tier: str = Field(description="core | voice | enhance | generate | source | analyze | publish")
    runtime: str = Field(description="local | local_gpu | api | hybrid")
    stability: str = Field(description="experimental | beta | production")


class ExecuteResult(BaseModel):
    success: bool = Field(description="Whether the tool execution succeeded")
    data: dict[str, Any] = Field(default_factory=dict, description="Tool output data")
    artifacts: list[str] = Field(default_factory=list, description="Output file paths")
    error: Optional[str] = Field(default=None, description="Error message if failed")
    cost_usd: float = Field(default=0.0, description="Actual cost in USD")
    duration_seconds: float = Field(default=0.0, description="Execution time in seconds")
    seed: Optional[int] = Field(default=None, description="Random seed used")
    model: Optional[str] = Field(default=None, description="Model identifier used")


class DryRunResult(BaseModel):
    tool: str
    estimated_cost_usd: float
    estimated_runtime_seconds: float
    status: str
    would_execute: bool


class UploadAssetResult(BaseModel):
    success: bool = Field(description="Whether the asset was stored")
    asset: dict[str, Any] = Field(default_factory=dict, description="Asset manifest entry")
    asset_manifest: dict[str, Any] = Field(default_factory=dict, description="Manifest fragment ready for video tools")
    deduplicated: bool = Field(default=False, description="Whether an identical existing file was reused")
    status: Optional[str] = None
    asset_count: int = 0
    message: Optional[str] = None
    next_action: Optional[str] = None
    batch_id: Optional[str] = None
    error: Optional[str] = Field(default=None, description="Error message if upload failed")


class S3UploadResult(BaseModel):
    success: bool = Field(description="Whether the upload completed")
    url: Optional[str] = Field(default=None, description="Public or pre-signed download URL")
    object_key: Optional[str] = Field(default=None, description="S3 object key")
    bucket: Optional[str] = Field(default=None, description="Target bucket name")
    visibility: Optional[str] = Field(default=None, description="public or private")
    expires_at: Optional[str] = Field(default=None, description="ISO expiry for private URLs")
    download_page_url: Optional[str] = Field(default=None, description="HTML download page URL (when requested)")
    uploaded_files: list[dict[str, Any]] = Field(default_factory=list, description="Per-file upload records")
    publish_log: dict[str, Any] = Field(default_factory=dict, description="Schema-valid publish_log entry")
    error: Optional[str] = Field(default=None, description="Error message if upload failed")


class CheckpointData(BaseModel):
    version: str
    project_id: str
    pipeline_type: str
    stage: str
    status: str
    timestamp: Optional[str] = None
    artifacts: dict[str, Any] = Field(default_factory=dict)
    human_approval_required: bool = False
    human_approved: bool = False
    error: Optional[str] = None


class PipelineStatus(BaseModel):
    project_id: str
    pipeline_type: Optional[str]
    completed_stages: list[str]
    next_stage: Optional[str]
    latest_checkpoint: Optional[CheckpointData] = None


class PipelineInfo(BaseModel):
    name: str
    version: str = "unknown"
    category: str = "unknown"
    stages: list[str] = Field(default_factory=list)
    description: str = ""


# ---------------------------------------------------------------------------
# Discovery tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_tools(
    capability: Optional[str] = None,
    status: Optional[str] = None,
    provider: Optional[str] = None,
    tier: Optional[str] = None,
) -> list[ToolSummary]:
    """List all registered OpenMontage tools with their status.

    Optionally filter by capability (e.g. 'tts', 'image_generation', 'video_post'),
    status ('available', 'unavailable'), provider (e.g. 'elevenlabs', 'ffmpeg'), or
    tier ('core', 'voice', 'enhance', 'generate', 'source', 'analyze', 'publish').
    """
    tools = list(registry._tools.values())

    if capability:
        tools = [t for t in tools if t.capability == capability]
    if provider:
        tools = [t for t in tools if t.provider == provider]
    if tier:
        tools = [t for t in tools if t.tier.value == tier]

    results = []
    for t in tools:
        tool_status = t.get_status().value
        if status and tool_status != status:
            continue
        results.append(ToolSummary(
            name=t.name,
            capability=t.capability,
            provider=t.provider,
            status=tool_status,
            tier=t.tier.value,
            runtime=t.runtime.value,
            stability=t.stability.value,
        ))
    return results


@mcp.tool()
def get_tool_info(tool_name: str) -> dict[str, Any]:
    """Get the full tool contract: input/output schemas, dependencies, cost info, etc.

    Use this before calling execute_tool to understand what inputs a tool accepts
    and what it returns. The input_schema field is a JSON Schema object.
    """
    tool = registry.get(tool_name)
    if tool is None:
        return {"error": f"Tool '{tool_name}' not found. Use list_tools to see available tools."}
    return tool.get_info()


@mcp.tool()
def get_capabilities() -> dict[str, list[dict[str, Any]]]:
    """Get all tools grouped by capability (tts, image_generation, video_post, etc.).

    Returns a dict mapping capability names to lists of tool info dicts.
    Useful for understanding what the system can do at a glance.
    """
    return registry.capability_catalog()


@mcp.tool()
def get_provider_menu() -> dict[str, Any]:
    """Get a human-ready provider menu with configured/unconfigured counts.

    Returns the preflight-style capability menu showing which providers are
    configured (have API keys) vs available but not yet set up.
    Also includes composition runtime availability (remotion, hyperframes, ffmpeg).
    """
    return registry.provider_menu_summary()


# ---------------------------------------------------------------------------
# Subtitle & Voice Convenience Wrappers
#
# These are domain-flavored entry points over the underlying tools
# (`video_compose`, `elevenlabs_tts`). They keep the MCP surface small and
# give external clients a vocabulary tied to the user-visible feature instead
# of the internal `execute_tool(tool_name="...", inputs={...})` envelope.
#
# Both still go through the registry, so all governance (cost tracking,
# review hooks, decision log) applies the same as for any other tool call.
# ---------------------------------------------------------------------------


@mcp.tool()
async def burn_subtitles(
    input_path: str,
    subtitle_path: str,
    output_path: Optional[str] = None,
    subtitle_style: Optional[dict[str, Any]] = None,
    codec: str = "libx264",
    crf: int = 23,
) -> ExecuteResult:
    """Burn a subtitle file (.srt / .ass / .vtt) into a video.

    Thin wrapper over `video_compose` with `operation=burn_subtitles`. Uses
    FFmpeg's `subtitles=` filter; codec defaults to `libx264` so the result
    is widely playable. Audio is copied losslessly (no re-encode).
    """
    tool = registry.get("video_compose")
    if tool is None:
        return ExecuteResult(success=False, error="video_compose tool not registered")

    inputs: dict[str, Any] = {
        "operation": "burn_subtitles",
        "input_path": input_path,
        "subtitle_path": subtitle_path,
        "codec": codec,
        "crf": crf,
    }
    if output_path:
        inputs["output_path"] = output_path
    if subtitle_style:
        inputs["subtitle_style"] = subtitle_style

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"burn_subtitles failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


@mcp.tool()
async def clone_voice(
    name: str,
    audio_paths: list[str],
    description: Optional[str] = None,
    engine: Optional[str] = "qwen",
    reference_texts: Optional[list[str]] = None,
    reference_text: Optional[str] = None,
) -> ExecuteResult:
    """Create a cloned voice profile via the local Voicebox service.

    Routes through the `voicebox_tts` tool, which talks to the Voicebox REST
    API on `http://127.0.0.1:17493` by default (override with
    `VOICEBOX_REST_URL`). Voice data never leaves the host.

    Engines that support voice cloning on this Voicebox:
      qwen, luxtts, chatterbox, chatterbox_turbo, tada.
    Default `qwen` (Qwen3-TTS instant clone). Preset voices like `kokoro`
    do not accept reference samples.

    Recommended total sample duration >= 30 seconds for a usable Qwen3-TTS
    clone. Returns the new `profile_id` for use with voicebox text-to-speech.

    Voicebox requires each audio sample to have a matching transcript
    (`reference_texts` — one entry per audio_paths entry, in order). If you
    don't have per-sample transcripts, pass `reference_text` to apply the
    same transcript to every sample (low-quality fallback).
    """
    tool = registry.get("voicebox_tts")
    if tool is None:
        return ExecuteResult(success=False, error="voicebox_tts tool not registered")

    inputs: dict[str, Any] = {
        "operation": "clone_voice",
        "name": name,
        "audio_paths": audio_paths,
        "default_engine": engine or "qwen",
    }
    if description:
        inputs["description"] = description
    if reference_texts is not None:
        inputs["reference_texts"] = reference_texts
    if reference_text is not None:
        inputs["reference_text"] = reference_text

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"clone_voice failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


@mcp.tool()
async def list_cloned_voices(
    include_presets: bool = False,
) -> ExecuteResult:
    """List voice profiles on the local Voicebox instance.

    By default returns only `voice_type=cloned` profiles (those created via
    `clone_voice` / `voicebox_clone_voice`). Set `include_presets=True` to
    also include preset and designed voices.

    Each entry has `id`, `name`, `voice_type`, and an `is_cloned` flag.
    """
    tool = registry.get("voicebox_tts")
    if tool is None:
        return ExecuteResult(success=False, error="voicebox_tts tool not registered")

    inputs: dict[str, Any] = {
        "operation": "list_cloned_voices",
        "include_presets": include_presets,
    }

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"list_cloned_voices failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


@mcp.tool()
async def voicebox_clone_voice(
    name: str,
    audio_paths: list[str],
    description: Optional[str] = None,
    default_engine: Optional[str] = "qwen",
    reference_texts: Optional[list[str]] = None,
    reference_text: Optional[str] = None,
) -> ExecuteResult:
    """Create a Voicebox voice profile and attach 1+ reference audio samples.

    Talks to the local Voicebox REST API (default http://127.0.0.1:17493) via
    the `voicebox_tts` BaseTool. Returns the new `profile_id` for use with
    `voicebox_tts` `text_to_speech` to generate narration in the cloned voice.

    Recommended total sample duration >= 30 seconds for a usable Qwen3-TTS
    clone. Requires Voicebox to be running locally; override
    VOICEBOX_REST_URL for remote hosts.
    """
    tool = registry.get("voicebox_tts")
    if tool is None:
        return ExecuteResult(success=False, error="voicebox_tts tool not registered")

    inputs: dict[str, Any] = {
        "operation": "clone_voice",
        "name": name,
        "audio_paths": audio_paths,
    }
    if description is not None:
        inputs["description"] = description
    if default_engine is not None:
        inputs["default_engine"] = default_engine
    if reference_texts is not None:
        inputs["reference_texts"] = reference_texts
    if reference_text is not None:
        inputs["reference_text"] = reference_text

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"voicebox_clone_voice failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


@mcp.tool()
async def voicebox_tts(
    text: str,
    profile_id: str,
    language: Optional[str] = "en",
    engine: Optional[str] = None,
    model_size: Optional[str] = None,
    instruct: Optional[str] = None,
    personality: Optional[bool] = None,
    seed: Optional[int] = None,
    output_path: Optional[str] = None,
    timeout_seconds: Optional[int] = None,
) -> ExecuteResult:
    """Synthesize speech via the local Voicebox REST API.

    Uses a VoiceProfile created via `voicebox_clone_voice` (or a preset
    profile surfaced by `voicebox_list_cloned_voices`). Generates audio on
    the host running Voicebox — no API keys, no cloud spend, voice data
    never leaves the machine.

    Returns ExecuteResult with `artifacts=[output_path]` pointing at the
    synthesized audio file (placed under the active project's
    assets/audio/ when no `output_path` is given).
    """
    tool = registry.get("voicebox_tts")
    if tool is None:
        return ExecuteResult(success=False, error="voicebox_tts tool not registered")

    inputs: dict[str, Any] = {
        "operation": "text_to_speech",
        "text": text,
        "profile_id": profile_id,
    }
    if language is not None:
        inputs["language"] = language
    if engine is not None:
        inputs["engine"] = engine
    if model_size is not None:
        inputs["model_size"] = model_size
    if instruct is not None:
        inputs["instruct"] = instruct
    if personality is not None:
        inputs["personality"] = personality
    if seed is not None:
        inputs["seed"] = seed
    if output_path is not None:
        inputs["output_path"] = output_path
    if timeout_seconds is not None:
        inputs["timeout_seconds"] = timeout_seconds

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"voicebox_tts failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


@mcp.tool()
async def voicebox_list_cloned_voices(
    include_presets: bool = False,
) -> ExecuteResult:
    """List voice profiles on the local Voicebox instance.

    By default returns only `voice_type=cloned` profiles (those created via
    `voicebox_clone_voice`). Set `include_presets=True` to also include
    preset and designed voices. Each entry has `id`, `name`, `voice_type`,
    and an `is_cloned` flag — mirroring ElevenLabs' shape so downstream
    selectors can filter uniformly across providers.
    """
    tool = registry.get("voicebox_tts")
    if tool is None:
        return ExecuteResult(success=False, error="voicebox_tts tool not registered")

    inputs: dict[str, Any] = {
        "operation": "list_cloned_voices",
        "include_presets": include_presets,
    }

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"voicebox_list_cloned_voices failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


@mcp.tool()
async def edge_tts(
    text: str,
    voice: str = "zh-CN-XiaoxiaoNeural",
    rate: str = "+0%",
    volume: str = "+0%",
    pitch: str = "+0Hz",
    output_path: str = "tts_output.mp3",
) -> ExecuteResult:
    """Synthesize speech with Microsoft Edge TTS (free, no API key required).

    The upstream tool's default voice `zh-CN-YunxiNeural` is rejected by
    Microsoft's edge TTS service from many IPs (returns `NoAudioReceived`).
    This wrapper defaults to `zh-CN-XiaoxiaoNeural` which is reliably
    reachable. If you want YunxiNeural explicitly, set `voice` — but be
    ready to fall back if you see "No audio was received".

    Voices verified working from this host:
      zh-CN-XiaoxiaoNeural   (中文女声，温暖)
      zh-CN-YunjianNeural    (中文男声，激情)
      zh-CN-XiaoyiNeural     (中文女声，活泼)
      zh-CN-liaoning-XiaobeiNeural (东北口音女声)
      en-US-AvaNeural        (英文女声)
      en-US-AndrewNeural     (英文男声)

    Returns ExecuteResult with `artifacts=[output_path]`.
    """
    tool = registry.get("edge_tts")
    if tool is None:
        return ExecuteResult(success=False, error="edge_tts tool not registered")

    inputs: dict[str, Any] = {
        "text": text,
        "voice": voice,
        "rate": rate,
        "volume": volume,
        "pitch": pitch,
        "output_path": output_path,
    }

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(success=False, error=f"edge_tts failed: {type(e).__name__}: {e}")

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


# ---------------------------------------------------------------------------
# Analysis Convenience Wrappers
#
# Domain-flavored entry points over `tools/analysis/*` tools. They keep the
# MCP surface vocabulary tied to the user-visible feature instead of the
# internal `execute_tool(tool_name="...", inputs={...})` envelope.
# Both still go through the registry, so all governance (cost tracking,
# review hooks, decision log) applies the same as for any other tool call.
# ---------------------------------------------------------------------------


@mcp.tool()
async def scene_detect(
    input_path: str,
    method: Optional[str] = "content",
    threshold: Optional[float] = None,
    min_scene_length_seconds: Optional[float] = 1.0,
    output_path: Optional[str] = None,
) -> ExecuteResult:
    """Detect scene boundaries in a video.

    Thin wrapper over the `scene_detect` tool. When PySceneDetect is installed
    the detector uses it; otherwise falls back to FFmpeg's
    `select=gt(scene,...)` filter. The robust long-video path is automatic:
    videos >= 5 minutes are detected chunk-by-chunk and merged globally so
    FFmpeg does not blow up on 4K / long-form sources. Partial segment
    failures are reported but never silently dropped — `status` becomes
    `degraded` and `diagnostics` carries the chunk-level error trail;
    `scene_count` / `scenes` still reflect what was recovered.

    Args:
        input_path: Path to the source video (mp4/mov/mkv/...).
        method: Detection method — 'content' (default), 'threshold', or
            'adaptive'.
        threshold: Detection threshold (interpretation depends on method).
        min_scene_length_seconds: Minimum scene length; scenes shorter than
            this are merged with their neighbors. Default 1.0, minimum 0.1.
        output_path: Where to write the scene list JSON. Defaults to
            `<input_path>.scenes.json` next to the source.
    """
    tool = registry.get("scene_detect")
    if tool is None:
        return ExecuteResult(success=False, error="scene_detect tool not registered")

    inputs: dict[str, Any] = {
        "input_path": input_path,
        "method": method,
        "min_scene_length_seconds": min_scene_length_seconds,
    }
    if threshold is not None:
        inputs["threshold"] = threshold
    if output_path:
        inputs["output_path"] = output_path

    ctx = contextvars.copy_context()
    try:
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs)
    except Exception as e:
        return ExecuteResult(
            success=False,
            error=f"scene_detect failed: {type(e).__name__}: {e}",
        )

    return ExecuteResult(
        success=result.success,
        data=result.data,
        artifacts=result.artifacts,
        error=result.error,
        cost_usd=result.cost_usd,
        duration_seconds=result.duration_seconds,
        model=result.model,
    )


# ---------------------------------------------------------------------------
# Execution tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def execute_tool(
    tool_name: str,
    inputs: dict[str, Any],
) -> ExecuteResult:
    """Execute an OpenMontage tool by name with the given inputs.

    IMPORTANT: Use get_tool_info first to understand the tool's input_schema.
    The inputs dict must match the tool's expected parameters.

    Returns structured result with success status, output data, file artifacts,
    cost in USD, and execution duration.
    """
    import asyncio
    import logging as _logging

    _log = _logging.getLogger("mcp_server")
    _log.info("execute_tool called: %s inputs=%s", tool_name, json.dumps(_safe_inputs(inputs), ensure_ascii=False))

    tool = registry.get(tool_name)
    if tool is None:
        return ExecuteResult(
            success=False,
            error=f"Tool '{tool_name}' not found. Use list_tools to discover tools.",
        )

    tool_status = tool.get_status()
    if tool_status == ToolStatus.UNAVAILABLE:
        return ExecuteResult(
            success=False,
            error=f"Tool '{tool_name}' is unavailable — dependencies not met. "
                  f"Check get_tool_info for required dependencies.",
        )

    try:
        # 同步工具经 asyncio.to_thread 在 worker 线程执行；该方法默认不复制
        # ContextVar，导致中间件注入的 mcp-session-id 在工具体内读不到
        # （get_mcp_session_id() 返回 None → "Mcp-Session-Id is required"）。
        # 显式复制当前上下文并带入线程，使 session 跨线程可见。
        ctx = contextvars.copy_context()
        # 同时把 session id 注入 inputs dict，这样像 upload_asset 那样直接读
        # inputs["mcp_session_id"] 的子工具也能拿到。复制避免污染调用方。
        inputs_for_call = dict(inputs)
        if "mcp_session_id" not in inputs_for_call:
            sid = get_mcp_session_id()
            if sid:
                inputs_for_call["mcp_session_id"] = sid
        result = await asyncio.to_thread(ctx.run, tool.execute, inputs_for_call)
        _log.info("execute_tool done: %s success=%s duration=%.2fs",
                  tool_name, result.success, result.duration_seconds or 0)
        # Log the full error (was [:80] — too short; masked the real cause of
        # Remotion failures and left the client in a retry loop seeing only
        # "Remotion render failed ... Underlying error:" with nothing after).
        # 2000 chars fits the typical "Remotion render failed (exit N):\n<25-line
        # stderr tail>" shape from video_compose._remotion_render.
        _log.info("execute_tool response: tool=%s success=%s data_keys=%s error=%s",
                  tool_name, result.success, list(result.data.keys()) if result.data else None,
                  (result.error[:2000] if result.error else None))

        # Build the data envelope. If the sub-tool returned no "data" key (or an
        # empty dict), fall back to forwarding all non-reserved top-level fields
        # from the ToolResult — this handles tools such as upload_asset that
        # project their payload onto the result dict rather than into result.data.
        RESERVED = {"success", "error", "artifacts", "data",
                    "cost_usd", "duration_seconds", "seed", "model"}
        if result.data:
            envelope_data = result.data
        else:
            # dataclasses.asdict yields {"success": ..., "data": {}, ...}.
            # Strip reserved keys (incl. the empty "data" field itself) AND any
            # None/empty values so the envelope stays clean for failed runs.
            raw = dataclasses.asdict(result)
            envelope_data = {
                k: v for k, v in raw.items()
                if k not in RESERVED and v
            }

        return ExecuteResult(
            success=result.success,
            data=envelope_data,
            artifacts=result.artifacts,
            error=result.error,
            cost_usd=result.cost_usd,
            duration_seconds=result.duration_seconds,
            seed=result.seed,
            model=result.model,
        )
    except Exception as e:
        _log.exception("execute_tool exception: %s", tool_name)
        return ExecuteResult(
            success=False,
            error=f"Execution failed: {type(e).__name__}: {e}",
        )


@mcp.tool()
async def upload_asset(
    project_id: str,
    filename: str,
    content_base64: str,
    mime_type: Optional[str] = None,
    sha256: Optional[str] = None,
    overwrite: bool = False,
) -> UploadAssetResult:
    """Upload client-local media into a project-scoped assets directory.

    The returned path is safe to pass to ``video_compose`` or an AI video
    provider on this server.  ``content_base64`` may be raw base64 or a
    ``data:...;base64,...`` URI.  Client-local paths are intentionally not
    accepted because the MCP server cannot access the caller's filesystem.
    """
    tool = registry.get("upload_asset")
    if tool is None:
        return UploadAssetResult(success=False, error="upload_asset tool is not registered")
    result = await _run_tool_sync(tool, {
        "project_id": project_id,
        "filename": filename,
        "content_base64": content_base64,
        "mime_type": mime_type,
        "sha256": sha256,
        "overwrite": overwrite,
        "mcp_session_id": get_mcp_session_id(),
    })
    data = result.data or {}
    batch = data.get("batch") or {}
    count = len(batch.get("assets", []))
    if result.success and batch:
        duration_ms = round((result.duration_seconds or 0) * 1000)
        request_id = get_mcp_request_id() or uuid.uuid4().hex
        batch_project = batch.get("project_id") or project_id
        _event("asset_uploaded", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
        _event("batch_collecting", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
    return UploadAssetResult(
        success=result.success,
        asset=data.get("asset", {}),
        asset_manifest=data.get("asset_manifest", {}),
        deduplicated=bool(data.get("deduplicated", False)),
        status=batch.get("status"),
        asset_count=count,
        message=f"已收到 {count} 张图片。你可以继续上传，上传完成后发送“生成视频”。" if batch else None,
        next_action="continue_upload_or_generate" if batch else None,
        batch_id=batch.get("batch_id"),
        error=result.error,
    )


@mcp.tool()
async def upload_asset_chunk(
    operation: Literal["start", "append", "complete"],
    project_id: Optional[str] = None,
    filename: Optional[str] = None,
    total_bytes: Optional[int] = None,
    mime_type: Optional[str] = None,
    sha256: Optional[str] = None,
    upload_id: Optional[str] = None,
    offset: Optional[int] = None,
    chunk_base64: Optional[str] = None,
) -> dict[str, Any]:
    """Resumable upload for 1080p-class media through small MCP requests.

    Call in order: start, append one or more chunks, complete. Each chunk
    should be at most 1 MiB; the server verifies size, offset and SHA-256.

    Required arguments per operation (missing ones are rejected before
    anything is written):

    - start:    project_id, filename, total_bytes
    - append:   upload_id, offset, chunk_base64
    - complete: upload_id

    ``project_id`` must be a safe basename: 1-128 chars, starting with a
    letter or digit, followed by letters, digits, '.', '_' or '-' (for
    example ``mclaw-demo``). Send the same project_id on every call of one
    upload.
    """
    tool = registry.get("upload_asset_chunk")
    if tool is None:
        return {"success": False, "error": "upload_asset_chunk is not registered"}
    started = time.monotonic()
    request_id = get_mcp_request_id() or uuid.uuid4().hex
    sid_digest = session_hash(get_mcp_session_id())
    upload_digest = hashlib.sha256(upload_id.encode()).hexdigest()[:12] if upload_id else None
    request_log = re.sub(r"[^A-Za-z0-9._-]", "_", str(request_id))[:128]
    operation_log = re.sub(r"[^A-Za-z0-9._-]", "_", str(operation))[:32]
    project_log = re.sub(r"[^A-Za-z0-9._-]", "_", str(project_id))[:128]
    _log.info(
        "upload_asset_chunk dispatch operation=%s request_id=%s session_hash=%s project_id=%s "
        "upload_hash=%s total_bytes=%s offset=%s chunk_b64_chars=%s",
        operation_log, request_log, sid_digest, project_log, upload_digest, total_bytes, offset,
        len(chunk_base64) if isinstance(chunk_base64, str) else 0,
    )
    try:
        result = await _run_tool_sync(tool, {
            "operation": operation,
            "project_id": project_id,
            "filename": filename,
            "total_bytes": total_bytes,
            "mime_type": mime_type,
            "sha256": sha256,
            "upload_id": upload_id,
            "offset": offset,
            "chunk_base64": chunk_base64,
            "mcp_session_id": get_mcp_session_id(),
        })
    except Exception:
        _log.exception(
            "upload_asset_chunk crashed operation=%s request_id=%s session_hash=%s elapsed_ms=%d",
            operation_log, request_log, sid_digest, round((time.monotonic() - started) * 1000),
        )
        raise
    error_log = (
        _safe_inputs(result.error[:200]).replace("\r", "\\r").replace("\n", "\\n")
        if result.error else None
    )
    _log.info(
        "upload_asset_chunk completed operation=%s request_id=%s session_hash=%s success=%s "
        "elapsed_ms=%d error=%s",
        operation_log, request_log, sid_digest, result.success,
        round((time.monotonic() - started) * 1000),
        error_log,
    )
    data = result.data or {}
    batch = data.get("batch") or {}
    count = len(batch.get("assets", []))
    if result.success and batch:
        duration_ms = round((result.duration_seconds or 0) * 1000)
        request_id = get_mcp_request_id() or uuid.uuid4().hex
        batch_project = batch.get("project_id") or project_id
        _event("asset_uploaded", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
        _event("batch_collecting", request_id=request_id, session_hash=session_hash(get_mcp_session_id()), project_id=batch_project, batch_id=batch.get("batch_id"), asset_count=count, status="collecting_assets", duration_ms=duration_ms)
    public_data = {key: value for key, value in data.items() if key != "batch"}
    response = {"success": result.success, **public_data, "artifacts": result.artifacts, "error": result.error}
    if batch:
        response.update({"status": batch.get("status"), "asset_count": count, "message": f"已收到 {count} 张图片。你可以继续上传，上传完成后发送“生成视频”。", "next_action": "continue_upload_or_generate", "batch_id": batch.get("batch_id")})
    return response


@mcp.tool()
async def read_session_asset(relative_path: str) -> dict[str, Any]:
    """Stream a session-uploaded asset back as base64 by repo-relative path.

    Exists so a BFF on a different host can serve ``<img>`` thumbnails
    without needing a shared filesystem with this MCP server. The BFF
    performs the owner-scope whitelist check (via ``get_session_assets``)
    before calling this; the tool itself only enforces path safety and
    reads the bytes. ``relative_path`` must live under ``<repo>/projects/``.

    The response carries ``data_base64`` (the bytes), ``bytes`` (size),
    ``mime_type`` (guessed from extension) and ``filename``. Errors are
    surfaced as ``{"success": False, "error": "..."}``.
    """
    tool = registry.get("read_session_asset")
    if tool is None:
        return {"success": False, "error": "read_session_asset tool is not registered"}
    result = await _run_tool_sync(tool, {
        "relative_path": relative_path,
        "mcp_session_id": get_mcp_session_id(),
    })
    if not result.success:
        return {"success": False, "error": result.error or "read failed"}
    data = result.data or {}
    return {
        "success": True,
        "bytes": data.get("bytes"),
        "data_base64": data.get("data_base64"),
        "mime_type": data.get("mime_type"),
        "filename": data.get("filename"),
        "relative_path": data.get("relative_path"),
    }


@mcp.tool(structured_output=False)
async def read_session_asset_image(relative_path: str) -> Any:
    """Return a session-uploaded image as a native MCP image content block.

    Same input contract as ``read_session_asset``, but the response is an MCP
    ``image`` content item instead of a JSON dict, so clients that render
    content natively draw the picture instead of dumping base64 text.

    Only ``.png/.jpg/.jpeg/.gif/.webp`` are supported — those are the formats
    MCP ``ImageContent`` can carry. For anything else (mp4, srt, mp3) use
    ``read_session_asset`` and decode ``data_base64`` yourself.

    ``structured_output=False`` is required: FastMCP only runs its
    ``Image`` -> ``ImageContent`` conversion when there is no output model,
    and an ``Image`` object cannot be validated against one.
    """
    tool = registry.get("read_session_asset_image")
    if tool is None:
        return {"success": False, "error": "read_session_asset_image tool is not registered"}
    result = await _run_tool_sync(tool, {
        "relative_path": relative_path,
        "mcp_session_id": get_mcp_session_id(),
    })
    if not result.success:
        return {"success": False, "error": result.error or "read failed"}
    # Return the Image object itself, not the dict: _convert_to_content only
    # emits ImageContent for `Image`, everything else degrades to TextContent.
    return (result.data or {})["image"]


def _resolve_session_asset_path(asset: dict) -> Path:
    """Resolve a session asset to an absolute path from its OS-portable
    ``relative_path`` (posix, relative to the repo root).

    Session assets are persisted with ``relative_path`` only; the absolute
    location is recomputed from the *current* repo root so the same session
    works on heterogeneous deployments (dev on Windows, prod on Linux). The
    upload-time absolute ``path`` is intentionally never read — it is
    OS-specific and invalid on any other machine, which is exactly what broke
    uploads in the Win10/Ubuntu setup.
    """
    rel = asset.get("relative_path")
    if not rel:
        raise ValueError("session asset has no relative_path; cannot resolve a filesystem location")
    # Keep this anchor identical to upload_asset/upload_asset_chunk. The
    # relative path is relative to PROJECTS_DIR.parent, which may be a
    # configured custom root outside the source checkout.
    candidate = (Path(lib_paths.PROJECTS_DIR).resolve().parent / rel).resolve()
    if not candidate.is_file():
        raise ValueError(f"session asset not found at resolved relative path: {rel}")
    return candidate


@mcp.tool()
async def get_session_assets() -> dict[str, Any]:
    """Return the images already uploaded for the current MCP session.

    Lets the frontend show what is already on the server so the user does not
    re-upload files after a partial upload failure. Each asset carries
    ``relative_path`` (posix, repo-root-relative), ``original_filename``,
    ``sha256``, ``bytes`` and ``type``. Returns ``{assets: []}`` when nothing
    has been uploaded for this session yet.
    """
    from lib.workbuddy_session import get_session_assets as _get_session_assets

    try:
        assets = _get_session_assets(get_mcp_session_id())
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("get_session_assets failed: %s", exc)
        return {"success": False, "error": str(exc), "assets": []}
    return {"success": True, "assets": assets}


def _cues_to_srt(cues: list[dict[str, Any]]) -> str:
    """Serialize subtitle cues to SRT text.

    Accepted cue shape: ``{index, start, end, text}`` with numeric seconds for
    start/end. The serializer is best-effort: missing fields are coerced to 0
    / "" rather than raising, since by the time we burn the renderer has
    already succeeded and a malformed cue shouldn't kill the share-link path.
    """
    def _stamp(seconds: float) -> str:
        ms_total = max(0, int(round(float(seconds) * 1000)))
        h, rem = divmod(ms_total, 3_600_000)
        m, rem = divmod(rem, 60_000)
        s, ms = divmod(rem, 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

    blocks: list[str] = []
    for i, raw in enumerate(cues or []):
        try:
            start = float(raw.get("start", 0))
            end = float(raw.get("end", 0))
        except (TypeError, ValueError):
            continue
        text = str(raw.get("text", "")).strip()
        idx = raw.get("index", i + 1)
        blocks.append(f"{idx}\n{_stamp(start)} --> {_stamp(end)}\n{text}\n")
    if not blocks:
        return ""
    return "\n".join(blocks).rstrip() + "\n"


def _chunk_metadata(
    assets_offset: int,
    chunk_count: int,
    chunk_limit: int,
    total_in_session: int,
) -> dict[str, Any]:
    """Build the chunk descriptor attached to ``edit_decisions.metadata.chunk``.

    Lets downstream consumers (Remotion templates, future stitchers,
    Backlot board) reconstruct the pagination shape without having to
    re-derive it from the asset list.
    """
    chunk_end = assets_offset + chunk_count
    chunk_index = assets_offset // chunk_limit if chunk_limit > 0 else 0
    return {
        "offset": assets_offset,
        "limit": chunk_limit,
        "count": chunk_count,
        "total_in_session": total_in_session,
        "chunk_index": chunk_index,
        "is_last": chunk_end >= total_in_session,
    }


def _ensure_governance_fields(
    edit_decisions: dict[str, Any],
    *,
    default_renderer_family: Optional[str],
    script_id: Optional[str],
    delivery_promise_override: Optional[dict] = None,
) -> None:
    """Guarantee edit_decisions carries renderer_family + metadata.delivery_promise.

    Mutates edit_decisions in place. Idempotent — never overwrites caller-set
    values. Required because ``video_compose._pre_compose_validation`` BLOCKS
    the render when ``renderer_family`` is missing, and silently skips the
    proposal→compose delivery-promise contract when ``metadata.delivery_promise``
    is absent. This is the single construction point for ``edit_decisions`` in
    the BFF/MCP pipeline, so we guarantee the contract here.

    Why these defaults:
      - ``renderer_family="animation-first"`` matches ``script_families[
        "photo-ken-burns"]`` (the default script_id) and is a legal value in
        ``tools/video/video_compose.py::RENDERER_FAMILY_MAP``.
      - ``motion_required=False`` is passed explicitly to ``classify_from_brief``
        because image-batch / template-batch inputs are stills; without this the
        classifier outputs ``MOTION_LED`` and ``validate_cuts`` BLOCKS on the
        motion-ratio rule.
    """
    # renderer_family: keep existing if set, else default.
    existing_rf = edit_decisions.get("renderer_family")
    if not existing_rf or not isinstance(existing_rf, str):
        edit_decisions["renderer_family"] = default_renderer_family or "animation-first"

    # metadata.delivery_promise: keep existing if set.
    metadata = edit_decisions.setdefault("metadata", {})
    existing_dp = metadata.get("delivery_promise") or edit_decisions.get("delivery_promise")
    if existing_dp:
        return
    if delivery_promise_override:
        metadata["delivery_promise"] = delivery_promise_override
        return

    pipeline_for_script = {
        "photo-ken-burns": "cinematic",
        "cinematic-montage": "cinematic",
        "ecommerce-product-demo": "hybrid",
    }
    pipeline_type = pipeline_for_script.get(script_id) or "hybrid"
    try:
        from lib.delivery_promise import classify_from_brief
        promise = classify_from_brief(
            pipeline_type,
            {
                # image-only inputs — never promise motion-required delivery.
                "motion_required": False,
                "has_footage": False,
                "tone": "corporate",
                "quality": "presentable",
            },
        )
        metadata["delivery_promise"] = promise.to_dict()
    except Exception as exc:  # noqa: BLE001 — never block dispatch on defaulting
        # Last-resort literal default so the field is at least non-null.
        metadata["delivery_promise"] = {
            "promise_type": "hybrid",
            "motion_required": False,
            "source_required": False,
            "tone_mode": "corporate",
            "quality_floor": "presentable",
            "approved_fallback": None,
        }
        _log.warning("default delivery_promise fallback used (classify failed: %s)", exc)


@mcp.tool()
async def create_remotion_video_share(
    project_id: Optional[str] = None,
    script_id: str = "photo-ken-burns",
    duration_per_image: float = 3.0,
    aspect_ratio: str = "9:16",
    title: Optional[str] = None,
code: Optional[str] = None,
    queue_owner_id: Optional[str] = None,
    delivery_promise_override: Optional[dict] = None,
    effects: Optional[str] = None,
    subtitles: Optional[list[dict[str, Any]]] = None,
    assets_offset: int = 0,
    assets_limit: int = 0,
    # Cloned-voice workflow (optional, no-op when voice_sample_asset_id is None):
    # voice_sample_asset_id/script/language/subtitle/subtitle_style/voice_consent
    # trigger a clone→tts→audio_mix→caption_burn chain inside the render
    # worker. ``voice_consent=true`` is REQUIRED whenever any voice input is
    # supplied — see tests/test_workbuddy_session_remotion_share.py.
    voice_sample_asset_id: Optional[str] = None,
    script: Optional[str] = None,
    language: str = "zh",
    subtitle: bool = True,
    subtitle_style: str = "short_video",
    voice_consent: bool = False,
) -> dict[str, Any]:
    """Generate and share a Remotion photo video from images in this MCP session.

    Images and the session are intentionally implicit: upload them first with
    upload_asset or upload_asset_chunk, then call this tool with no paths.
    Call this when the user says "生成视频", "开始生成", or "就这些，生成吧".

    Optional passthrough fields:
      - ``effects``: free-text natural-language description of the visual
        effects / camera moves / transitions the caller wants. Forwarded as
        ``metadata.effects`` for downstream consumers (Remotion templates,
        future renderers) to honour. Mirrors VClaw Studio's "视频效果" panel.
      - ``subtitles``: list of cue dicts (``index``, ``start``, ``end``,
        ``text``) — forwarded as ``metadata.subtitles``. Closed the schema
        fork with VClaw Studio (see
        ``docs/remotion-effects-field-review-2026-08-31.md`` §6).

    Multi-chunk pagination (``assets_offset`` / ``assets_limit``):
      The single-call cap is ``min(OPENMONTAGE_MAX_SESSION_IMAGES,
      floor(600/duration_per_image))``. When a session holds more images than
      that — a normal occurrence once production pipelines upload hundreds of
      frames in one MCP session — call this tool multiple times, paginating
      through the session's asset list:

          # chunk 1: images [0..20)
          create_remotion_video_share(assets_offset=0, assets_limit=20)
          # poll get_render_status → published, then:
          # chunk 2: images [20..40)
          create_remotion_video_share(assets_offset=20, assets_limit=20)

      - ``assets_offset`` (default ``0``) is the start index into the
        session's asset list.
      - ``assets_limit`` (default ``0`` = use ``OPENMONTAGE_MAX_SESSION_IMAGES``)
        is the maximum number of images rendered in this chunk. Cannot exceed
        the env cap.
      - The per-chunk render still honours the 600-second total-duration
        ceiling (``duration × chunk_count ≤ 600``).
      - Each call returns its own ``render_job_id`` and video; the caller is
        responsible for stitching the chunks downstream (e.g. via
        ``video_compose`` overlay).
      - The chunk metadata (``offset``, ``limit``, ``count``,
        ``total_in_session``, ``is_last``) is attached to
        ``edit_decisions.metadata.chunk`` for downstream consumers.
      - ``allow_continue`` semantics: rendering a non-first chunk after a
        previous chunk already published is permitted. Concurrent chunks on
        the same session are still rejected.

    This tool is *non-blocking*: it validates inputs, claims a render job
    (``render_job_id``), and dispatches the actual render→upload→share pipeline
    to a background thread. It returns immediately with the job id. Poll
    ``get_render_status`` with that id to track progress and fetch the final
    share URL. This avoids the long HTTP request that previously timed out on
    the client side for multi-minute Remotion renders.
    """
    script_families = {
        "photo-ken-burns": "animation-first",
        "cinematic-montage": "cinematic-trailer",
        "ecommerce-product-demo": "ecommerce-product-demo",
    }
    # Custom composition mode: a caller-supplied TSX source. Bypasses the
    # templated script_id lookup entirely and requires the safeguard flag.
    if code:
        if not str(os.environ.get("CUSTOM_COMPOSITION_ENABLED", "")).strip().lower() in ("1", "true", "yes", "on"):
            return {
                "success": False, "status": "failed", "stage": "validation",
                "message": "自定义合成渲染未启用：请在 .env 设置 CUSTOM_COMPOSITION_ENABLED=true",
                "error": "custom composition rendering disabled",
            }
        renderer_family = "custom-composition"
    else:
        renderer_family = script_families.get(script_id)
        if renderer_family is None:
            return {"success": False, "status": "failed", "stage": "validation", "message": f"Unknown script_id: {script_id}", "error": f"script_id must be one of: {', '.join(sorted(script_families))}"}

    if subtitle_style not in {"short_video", "standard"}:
        return {"success": False, "status": "failed", "stage": "validation", "error": "subtitle_style must be short_video or standard"}

    voice_requested = voice_sample_asset_id is not None or script is not None or voice_consent
    if voice_requested:
        if not voice_consent:
            return {"success": False, "status": "failed", "stage": "consent", "error": "voice_consent=true is required"}
        if not isinstance(script, str) or not script.strip() or len(script) > 10000:
            return {"success": False, "status": "failed", "stage": "validation", "error": "script must contain 1-10000 characters"}
        if not voice_sample_asset_id:
            return {"success": False, "status": "failed", "stage": "validation", "error": "voice_sample_asset_id is required for the voice workflow"}

    started = time.monotonic()
    sid = get_mcp_session_id()
    request_id = get_mcp_request_id() or uuid.uuid4().hex
    total_in_session = 0  # populated after we know the asset list size
    try:
        assets_offset = max(0, int(assets_offset))
        assets_limit = max(0, int(assets_limit))
    except (TypeError, ValueError) as exc:
        return {"success": False, "status": "failed", "stage": "validation",
                "message": f"assets_offset / assets_limit must be integers: {exc}",
                "error": str(exc)}
    try:
        # assets_offset > 0 means "rendering a continuation chunk"; allow
        # resuming a session whose previous chunk already published.
        digest, state = begin_render(sid, project_id, allow_continue=assets_offset > 0)
    except Exception as exc:
        _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=session_hash(sid), status="failed", stage="session", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
        return {"success": False, "status": "failed", "stage": "session", "message": str(exc), "error": str(exc)}

    project = state["project_id"]
    batch_id = state["batch_id"]
    job_id = state["render_job_id"]
# Generic session registration also tracks uploaded video/audio. The legacy
    # photo renderer must continue to operate only on image assets, so any
    # video / audio entries (uploaded for the cloned-voice or caption workflows)
    # are filtered out here — they live in ``state["media_assets"]`` and are
    # looked up directly via ``lib.workbuddy_session.find_asset`` when needed.
    assets = [asset for asset in state.get("assets", []) if asset.get("type") == "image"]
    total_in_session = len(assets)
    try:
        if not assets:
            raise ValueError("No completed images found in the current MCP session batch")
        duration = float(duration_per_image)
        if duration < 1 or duration > 30:
            raise ValueError("duration_per_image must be between 1 and 30 seconds")
        max_images = max(1, int(os.environ.get("OPENMONTAGE_MAX_SESSION_IMAGES", "20")))
        effective_limit = assets_limit if assets_limit > 0 else max_images
        if effective_limit > max_images:
            raise ValueError(
                f"assets_limit cannot exceed OPENMONTAGE_MAX_SESSION_IMAGES ({max_images})"
            )
        if assets_offset >= total_in_session:
            raise ValueError(
                f"assets_offset ({assets_offset}) is beyond the session batch "
                f"({total_in_session} images)"
            )
        chunk_end = min(assets_offset + effective_limit, total_in_session)
        chunk_assets = assets[assets_offset:chunk_end]
        if not chunk_assets:
            raise ValueError(
                f"No images in the requested range [{assets_offset}:{chunk_end}]"
            )
        if duration * len(chunk_assets) > 600:
            raise ValueError(
                f"The requested photo video chunk ({len(chunk_assets)} images × "
                f"{duration}s = {duration * len(chunk_assets)}s) exceeds the 600 second limit"
            )
        dimensions = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}
        if aspect_ratio not in dimensions:
            raise ValueError("aspect_ratio must be one of 9:16, 16:9, or 1:1")
        width, height = dimensions[aspect_ratio]
        # Phase C: anchor the per-project workspace to the current
        # principal's namespace. ``upload_asset`` / ``upload_asset_chunk``
        # land their bytes under ``projects/<users|services>/<namespace_key>/
        # <project_id>/assets/`` via the same ``ProjectWorkspace`` factory, so
        # the authorization root here has to be derived the same way. The
        # pre-Phase-C ``_PROJECT_ROOT / "projects" / project`` root rejected
        # every namespaced upload as "outside the project workspace" — that
        # broke upload→render — and simultaneously accepted any *other*
        # user's asset that happened to sit under the shared legacy tree.
        from lib.project_workspace import ProjectWorkspace  # lazy: keeps mcp_server import graph light
        try:
            workspace = ProjectWorkspace.for_current_principal(project)
        except _PrincipalNotFound as exc:
            # Re-raise as ValueError so the enclosing validation handler
            # reports a stable, non-leaky message: the registry's own text
            # carries the raw session id, which must not reach a tool result.
            raise ValueError(
                "no authenticated principal is bound to this MCP session; "
                "cannot resolve the per-user project workspace"
            ) from exc
        # Session assets always live under ``<workspace>/assets/`` (the upload
        # tools write to ``assets/_sessions/<session_digest>/``), so authorize
        # against that subtree rather than the whole workspace — renders and
        # checkpoints are not uploadable material.
        # Reads may come from either the canonical v2 project or the
        # authenticated principal's unmigrated v1 project.  Uploads and
        # renders always target ``workspace.root`` (v2); this allow-list is
        # read-only and remains scoped to the same principal.
        assets_roots = tuple(
            (candidate_root / "assets").resolve(strict=False)
            for candidate_root in workspace.read_roots
        )
        safe_assets = []
        for asset in chunk_assets:
            path = _resolve_session_asset_path(asset)
            if not any(
                path == assets_root or assets_root in path.parents
                for assets_root in assets_roots
            ):
                raise ValueError("session asset path is outside the project workspace")
            if not path.is_file() or asset.get("type") != "image":
                raise ValueError(f"session asset is not a readable image: {path.name}")
            # Recompute and persist the OS-correct absolute path so downstream
            # consumers (custom `images`, ecommerce slots) no longer depend on
            # the upload-time absolute path that may belong to another OS.
            safe_assets.append({**asset, "path": str(path), "source_tool": "upload_asset", "scene_id": f"photo-{len(safe_assets):04d}"})

        voice_sample = None
        if voice_requested:
            voice_sample = find_asset(sid, voice_sample_asset_id, project_id=project)
            if not voice_sample or voice_sample.get("type") != "audio":
                raise ValueError("voice_sample_asset_id is not an audio asset in the current MCP session/project")
            voice_path = Path(voice_sample.get("path", "")).resolve()
            try:
                voice_path.relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError("voice sample asset path is outside the project workspace") from exc
            if not voice_path.is_file():
                raise ValueError(f"voice sample asset is not readable: {voice_path.name}")

        motion = ["zoom-in", "pan-left", "ken-burns", "pan-right"]
        cuts = []
        scene_plan = []
        edit_decisions = {}
        if code:
            # Custom composition mode: the caller supplied TSX source. The
            # uploaded session images are passed directly to the user code via
            # the `images` field (referenced with staticFile() inside the code).
            images = [a["path"] for a in safe_assets]
            edit_decisions = {
                "version": "1.0",
                "cuts": [],
                "render_runtime": "remotion",
                "renderer_family": "custom-composition",
                "composition_mode": "custom",
                "custom_code": code,
                "images": images,
                "duration_per_image": duration,
                "metadata": {
                    "title": title or f"{project} 自定义合成",
                    "script_id": "custom",
                    "targetDurationSeconds": duration * max(len(images), 1),
                    "compose_target": {"width": width, "height": height, "fit": "cover"},
                    "chunk": _chunk_metadata(
                        assets_offset, len(safe_assets), effective_limit, total_in_session
                    ),
                },
            }
            if effects:
                edit_decisions["metadata"]["effects"] = effects
            if subtitles:
                edit_decisions["metadata"]["subtitles"] = subtitles
        else:
            for index, asset in enumerate(safe_assets):
                start_seconds = index * duration
                end_seconds = (index + 1) * duration
                # Parse natural-language effects when present: each cut gets a
                # keyword-scored animation token plus the segment text stashed
                # in transform.effects / shot_language.effects for richer
                # downstream templates. When effects is empty, the helper
                # returns ("zoom-in", "") and the round-robin below takes over.
                effects_animation, effects_segment = effects_animation_for_cut(
                    effects, index, len(safe_assets)
                )
                animation = effects_animation if effects else motion[index % len(motion)]
                cut_transform: dict[str, Any] = {"animation": animation}
                shot_language: dict[str, Any] = {
                    "camera_movement": animation, "shot_size": "full-frame",
                }
                if effects_segment:
                    cut_transform["effects"] = effects_segment
                    shot_language["effects"] = effects_segment
                cuts.append({
                    "id": f"cut-{index:04d}", "source": asset["id"], "in_seconds": start_seconds,
                    "out_seconds": end_seconds, "layer": "primary", "transition_in": "fade" if index else "cut",
                    "transition_duration": 0.25 if index else 0, "transform": cut_transform,
                })
                scene_plan.append({
                    "type": "image",
                    "description": f"Uploaded customer photo {index + 1}",
                    "shot_intent": "Present the uploaded photo with restrained camera motion",
                    "narrative_role": "customer_photo",
                    "hero_moment": index == 0,
                    "shot_language": shot_language,
                })
            edit_decisions = {
                "version": "1.0", "cuts": cuts, "render_runtime": "remotion",
                "renderer_family": renderer_family, "composition_mode": "templated",
                "metadata": {"title": title or f"{project} photo video", "script_id": script_id, "targetDurationSeconds": duration * len(safe_assets), "compose_target": {"width": width, "height": height, "fit": "cover"}, "chunk": _chunk_metadata(assets_offset, len(safe_assets), effective_limit, total_in_session)},
            }
            # Passthrough fields: only added when caller supplied them, so the
            # edit_decisions payload stays clean for clients that don't use
            # these yet (back-compat with frameflow_e2e.py and friends).
            if effects:
                edit_decisions["metadata"]["effects"] = effects
            if subtitles:
                edit_decisions["metadata"]["subtitles"] = subtitles
        # Governance contract: edit_decisions MUST carry renderer_family and
        # metadata.delivery_promise before dispatching _run_render_job, otherwise
        # video_compose._pre_compose_validation BLOCKS the render.
        # See _ensure_governance_fields docstring for default-field rationale.
        _ensure_governance_fields(
            edit_decisions,
            default_renderer_family=renderer_family,
            script_id=script_id if not code else None,
            delivery_promise_override=delivery_promise_override,
        )
        if script_id == "ecommerce-product-demo" and not code:
            if len(safe_assets) < 4:
                raise ValueError("ecommerce-product-demo requires at least 4 uploaded images")
            # The Remotion composition has four semantic slots. Keep the
            # uploaded batch order deterministic: hero, product, detail,
            # lifestyle. Additional uploads remain available in the batch but
            # are intentionally ignored by this four-slot template.
            edit_decisions.update({
                "brandName": "VOYAGE",
                "productName": "AeroShell Carry-On",
                "promise": "Travel lighter. Move further.",
                "price": "$189",
                "compareAtPrice": "$239",
                "offer": "Free worldwide shipping · 30-day returns",
                "cta": "SHOP NOW",
                "featureOne": {"title": "Silent 360° wheels", "body": "Glides smoothly through terminals and streets."},
                "featureTwo": {"title": "Impact-ready shell", "body": "Aerospace-grade protection for every trip."},
                "featureThree": {"title": "Smart interior", "body": "Compression panels keep every item in place."},
                "specs": [{"label": "CAPACITY", "value": "38 L"}, {"label": "WEIGHT", "value": "3.2 kg"}, {"label": "WARRANTY", "value": "Lifetime"}],
                # The template's default props include a demo bgm.wav. This
                # production path has no supplied music, so override it with
                # an empty value instead of requesting a non-existent asset.
                "assets": {"hero": safe_assets[0]["path"], "product": safe_assets[1]["path"], "detail": safe_assets[2]["path"], "lifestyle": safe_assets[3]["path"], "music": ""},
                "accentColor": "#D1A84B",
                "targetDurationSeconds": duration * len(safe_assets),
            })
        asset_manifest = {"version": "1.0", "assets": safe_assets, "metadata": {"project_id": project, "batch_id": batch_id}}
        # Renders land in the same per-principal namespace as the assets they
        # were composed from (``ProjectWorkspace.renders``), so one user's
        # output can never be overwritten by another user rendering the same
        # project_id.
        output = workspace.renders / f"{batch_id}-{job_id}.mp4"
        profile = {"9:16": "tiktok", "16:9": "generic_hd", "1:1": "instagram_feed"}[aspect_ratio]
    except Exception as exc:
        update_session(sid, status="failed", failure_stage="validation", video_path=None, error=str(exc))
        _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=session_hash(sid), project_id=project, batch_id=batch_id, asset_count=len(assets), render_job_id=job_id, status="failed", stage="validation", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
        return {"success": False, "status": "failed", "stage": "validation", "batch_id": batch_id, "render_job_id": job_id, "message": f"参数校验失败：{exc}", "error": str(exc)}

    # Dispatch the long-running render→upload→share pipeline to a background
    # thread so this MCP call returns immediately (no client-side timeout).
    # The BFF supplies a stable, opaque authenticated-user key. Direct MCP
    # clients fall back to their MCP session id. It is used only for fair
    # scheduling and is never returned to another caller.
    owner_id = (queue_owner_id or sid or "anonymous").strip()[:256]
    job_kwargs = dict(
        sid=sid, request_id=request_id, project=project, batch_id=batch_id,
        job_id=job_id, safe_assets=safe_assets, edit_decisions=edit_decisions,
        asset_manifest=asset_manifest, scene_plan=scene_plan, profile=profile,
        output=str(output), title=title, asset_count=len(safe_assets),
queue_owner_id=owner_id,
        # Subtitle cues for post-render burn. Persisted so a job that survives
        # a restart still gets its subtitles burned.
        subtitles=subtitles,
        voice_sample_path=str(voice_sample["path"]) if voice_requested else None,
        script=script.strip() if voice_requested else None,
        language=language, subtitle=subtitle, subtitle_style=subtitle_style,
        voice_consent=voice_consent,
    )
    # Persist the dispatch kwargs so a job still waiting for a render slot at
    # restart time can be re-dispatched (see recover_orphans + _drain_queued_jobs).
    try:
        save_job_record(job_id, job_kwargs)
    except Exception as exc:  # noqa: BLE001 - queued jobs must be recoverable
        error = f"failed to persist render job before dispatch: {exc}"
        update_session(sid, status="failed", failure_stage="queue_persistence", error=error)
        _event("workflow_failed", request_id=request_id, session_hash=session_hash(sid), project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="queue_persistence", error=error)
        return {"success": False, "status": "failed", "stage": "queue_persistence", "render_job_id": job_id, "error": error, "message": "渲染任务持久化失败，请稍后重试。"}
    queue_ready = threading.Event()
    threading.Thread(
        target=_run_render_job,
        kwargs={**job_kwargs, "_queue_ready_event": queue_ready},
        daemon=True,
    ).start()
    # Wait only until the worker has registered with the fair gate. This keeps
    # sequential submissions FIFO without waiting for an actual render slot.
    queue_ready.wait(timeout=5)
    _event("render_queued", request_id=request_id, session_hash=session_hash(sid), project_id=project, batch_id=batch_id, asset_count=len(safe_assets), render_job_id=job_id, status="queued", duration_ms=round((time.monotonic() - started) * 1000))
    chunk_info = _chunk_metadata(assets_offset, len(safe_assets), effective_limit, total_in_session)
    _event("render_chunk_queued", request_id=request_id, session_hash=session_hash(sid), project_id=project, batch_id=batch_id, render_job_id=job_id, **chunk_info)
    return {
        "success": True, "status": "queued",
        "render_job_id": job_id, "batch_id": batch_id, "project_id": project,
        "asset_count": len(safe_assets), "duration_seconds": duration * len(safe_assets),
        "chunk": chunk_info,
        "message": "视频渲染已在后台启动，请使用 get_render_status(render_job_id) 轮询进度与最终结果。"
        if assets_offset == 0
        else f"分批渲染第 {chunk_info['chunk_index'] + 1} 段（{len(safe_assets)} 张图）已启动，"
             f"轮询完成后调用本工具传 assets_offset={chunk_info['offset'] + len(safe_assets)} 渲染下一批。",
    }


def _run_render_job(
    *, sid, request_id, project, batch_id, job_id, safe_assets,
    edit_decisions, asset_manifest, scene_plan, profile, output, title, asset_count,
queue_owner_id=None, subtitles=None, _queue_ready_event=None,
    voice_sample_path=None, script=None, language="zh", subtitle=True,
    subtitle_style="short_video", voice_consent=False,
) -> None:
    """Background worker: render → (optionally burn subtitles) → upload to Weiyun → share link.

    When ``subtitles`` is a non-empty cue list, the rendered MP4 is post-processed
    through ``video_compose(operation='burn_subtitles')`` with a temp SRT file
    written from the cues, and the burned output replaces the original for upload.
    The unburned render is kept on disk for debugging only when the burn fails.

    Runs in a daemon thread with its own asyncio event loop. Updates the MCP
    session state (status / video_path / share_url) so ``get_render_status`` can
    report progress. Mirrors the synchronous pipeline that previously blocked
    the MCP call.
    """
    # Defense-in-depth: even for jobs persisted before the governance fix,
    # guarantee edit_decisions carries renderer_family and delivery_promise
    # before video_compose's pre-compose validation runs. This catches jobs
    # restored from .mcp_jobs.json by _drain_queued_jobs after a server restart.
    _ensure_governance_fields(
        edit_decisions,
        default_renderer_family=edit_decisions.get("renderer_family"),
        script_id=edit_decisions.get("metadata", {}).get("script_id"),
        delivery_promise_override=None,
    )
    async def _worker() -> None:
        set_mcp_session_id(sid)
        started = time.monotonic()
        digest = session_hash(sid)

        def _publish_progress(partial: dict) -> None:
            """Bridge a partial progress dict onto the SSE bus for this job.

            Also books the render queue and mirrors queue state into the session
            so ``get_render_status`` (polling) and the SSE snapshot both expose
            ``render_phase`` / ``queue_position`` / ``queue_depth``. The precise
            slot gate (enter/leave) lives in tools/video/video_compose.py; here
            we react to its ``slot_waiting`` / ``slot_acquired`` markers.
            """
            status = partial.get("status")
            if partial.get("slot_waiting"):
                # Was just placed into the waiting set; surface its rank.
                partial = {**partial,
                           "render_phase": "queued_for_slot"}
                update_session(sid, render_phase="queued_for_slot",
                               queue_position=partial.get("position"),
                               queue_depth=partial.get("queue_depth"))
                if _queue_ready_event is not None:
                    _queue_ready_event.set()
            elif partial.get("slot_acquired"):
                update_session(sid, status="rendering", render_phase="rendering",
                               queue_position=None, queue_depth=None)
            elif status in ("published", "failed"):
                # Terminal: drop any residual queue bookkeeping.
                try:
                    get_render_queue().leave(job_id)
                except Exception:  # noqa: BLE001
                    pass
                update_session(sid, render_phase=None,
                               queue_position=None, queue_depth=None)
            publish(job_id, progress_event(job_id, **partial))

        try:
            _event("render_requested", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=asset_count, render_job_id=job_id, status="rendering")
            _event("render_started", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=asset_count, render_job_id=job_id, status="rendering")
            render_tool = registry.get("video_compose")
            if render_tool is None:
                raise RuntimeError("video_compose tool is not registered")
            render_result = await _run_tool_sync(render_tool, {"operation": "render", "edit_decisions": edit_decisions, "asset_manifest": asset_manifest, "scene_plan": scene_plan, "profile": profile, "output_path": output, "remotion_timeout_ms": 600000, "_progress_callback": _publish_progress, "_job_id": job_id, "_queue_owner_id": queue_owner_id or sid})
            if not render_result.success:
                raise RuntimeError(render_result.error or "Remotion render failed")
            video_path = output
            update_session(sid, status="rendered", video_path=video_path)
            _publish_progress({"phase": "render", "status": "rendered", "message": "Render finished"})
            _event("render_completed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=asset_count, render_job_id=job_id, status="rendered", duration_ms=round((time.monotonic() - started) * 1000))
        except Exception as exc:
            update_session(sid, status="failed", video_path=locals().get("video_path"), failure_stage="render", error=str(exc))
            _publish_progress({"phase": "render", "status": "failed", "error": str(exc)})
            _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=asset_count, render_job_id=job_id, status="failed", stage="render", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
            return

# Optional: burn subtitles onto the rendered MP4 before upload.
        # video_compose(operation='burn_subtitles') wraps FFmpeg's subtitles=
        # filter. On success video_path is replaced with the burned file; on
        # failure we keep the original render (subtitle burn is best-effort).
        if subtitles:
            burn_tool = registry.get("video_compose")
            if burn_tool is None:
                _log.warning("burn_subtitles skipped: video_compose not registered (job %s)", job_id)
            else:
                srt_path = Path(output).with_suffix(".srt")
                try:
                    srt_path.write_text(_cues_to_srt(subtitles), encoding="utf-8")
                except OSError as exc:
                    _log.warning("burn_subtitles: failed to write %s: %s", srt_path, exc)
                else:
                    burned_path = str(Path(output).with_name(
                        f"{Path(output).stem}-subtitled{Path(output).suffix}"
                    ))
                    _publish_progress({"phase": "subtitle_burn", "status": "running", "message": "Burning subtitles into rendered video"})
                    try:
                        burn_result = await _run_tool_sync(burn_tool, {
                            "operation": "burn_subtitles",
                            "input_path": video_path,
                            "subtitle_path": str(srt_path),
                            "output_path": burned_path,
                            "codec": "libx264",
                            "crf": 23,
                        })
                        if burn_result.success and Path(burned_path).is_file():
                            video_path = burned_path
                            update_session(sid, status="subtitled", video_path=video_path)
                            _publish_progress({"phase": "subtitle_burn", "status": "done", "message": "Subtitles burned"})
                            _event("subtitle_burn_completed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="subtitled", cue_count=len(subtitles))
                        else:
                            _log.warning("burn_subtitles failed for job %s: %s", job_id, burn_result.error)
                            _publish_progress({"phase": "subtitle_burn", "status": "failed", "error": burn_result.error})
                    except Exception as exc:  # noqa: BLE001 - burn failure must not block upload
                        _log.warning("burn_subtitles raised for job %s: %s", job_id, exc)
                        _publish_progress({"phase": "subtitle_burn", "status": "failed", "error": str(exc)})

        # Voice is an optional post-render stage. Keeping it here means the
        # legacy image-only call has the exact same render/upload/share path,
        # while voice jobs retain the original render_job_id and session state.
        voice_requested = voice_sample_path is not None or script is not None or voice_consent
        if voice_requested:
            try:
                if not voice_consent:
                    raise RuntimeError("voice_consent=true is required")
                if not voice_sample_path or not isinstance(script, str) or not script.strip():
                    raise RuntimeError("voice sample and non-empty script are required for the voice workflow")

                def required_tool(name: str):
                    tool = registry.get(name)
                    if tool is None:
                        raise RuntimeError(f"{name} tool is not registered")
                    return tool

                update_session(sid, status="voice_cloning", video_path=video_path)
                _publish_progress({"phase": "voice_cloning", "status": "voice_cloning", "message": "Cloning voice"})
                cloned = await _run_tool_sync(required_tool("voicebox_voice_clone"), {
                    "sample_path": voice_sample_path, "consent": True,
                    "voice_id": f"om-{job_id[:20]}",
                })
                if not cloned.success:
                    raise RuntimeError(cloned.error or "Voicebox voice clone failed")
                clone_data = cloned.data or {}
                voice_id = clone_data.get("provider_voice_id") or clone_data.get("voice_id")
                if not voice_id:
                    raise RuntimeError("Voicebox voice clone returned no voice_id")

                renders = (_PROJECT_ROOT / "projects" / project / "renders").resolve()
                renders.mkdir(parents=True, exist_ok=True)
                audio_path = str(renders / f"{job_id}-voice.mp3")
                update_session(sid, status="tts_generating", video_path=video_path)
                _publish_progress({"phase": "tts_generating", "status": "tts_generating", "message": "Generating cloned narration"})
                spoken = await _run_tool_sync(required_tool("voicebox_tts"), {
                    "text": script, "voice_id": voice_id, "output_path": audio_path,
                    "subtitle": subtitle, "language": language,
                })
                if not spoken.success:
                    raise RuntimeError(spoken.error or "Voicebox TTS failed")
                spoken_data = spoken.data or {}
                audio_path = spoken_data.get("audio_path") or spoken_data.get("output") or audio_path
                segments = spoken_data.get("segments") or []

                revoiced = str(renders / f"{job_id}-revoiced.mp4")
                update_session(sid, status="audio_mixing", video_path=video_path)
                _publish_progress({"phase": "audio_mixing", "status": "audio_mixing", "message": "Replacing video audio"})
                mixed = await _run_tool_sync(required_tool("audio_mixer"), {
                    "operation": "replace_video_audio", "video_path": video_path,
                    "audio_path": audio_path, "output_path": revoiced,
                })
                if not mixed.success:
                    raise RuntimeError(mixed.error or "Audio replacement failed")
                video_path = (mixed.data or {}).get("output") or revoiced

                if subtitle and not segments:
                    transcript_dir = renders / f"{job_id}-transcript"
                    update_session(sid, status="transcribing", video_path=video_path)
                    _publish_progress({"phase": "transcribing", "status": "transcribing", "message": "Generating subtitle timestamps"})
                    transcript = await _run_tool_sync(required_tool("transcriber"), {
                        "input_path": audio_path, "language": language,
                        "output_dir": str(transcript_dir),
                    })
                    if not transcript.success:
                        raise RuntimeError(transcript.error or "Narration transcription failed")
                    segments = (transcript.data or {}).get("segments") or []

                if subtitle:
                    if not segments:
                        raise RuntimeError("No timed transcript segments were produced")
                    captioned = str(renders / f"{job_id}-captioned.mp4")
                    update_session(sid, status="subtitle_burning", video_path=video_path)
                    _publish_progress({"phase": "subtitle_burning", "status": "subtitle_burning", "message": "Burning subtitles"})
                    burned = await _run_tool_sync(required_tool("remotion_caption_burn"), {
                        "input_path": video_path, "output_path": captioned,
                        "segments": segments,
                        "words_per_page": 4 if subtitle_style == "short_video" else 8,
                    })
                    if not burned.success:
                        raise RuntimeError(burned.error or "Subtitle burn failed")
                    video_path = (burned.data or {}).get("output") or captioned

                update_session(sid, status="rendered", video_path=video_path)
                _publish_progress({"phase": "voice", "status": "rendered", "message": "Voice and subtitles finished"})
            except Exception as exc:
                update_session(sid, status="failed", video_path=locals().get("video_path", output), failure_stage="voice", error=str(exc))
                _publish_progress({"phase": "voice", "status": "failed", "error": str(exc)})
                _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, asset_count=asset_count, render_job_id=job_id, status="failed", stage="voice", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
                return

        upload_tool = registry.get("weiyun_upload")
        if upload_tool is None:
            error = "weiyun_upload tool is not registered"
            update_session(sid, status="failed", failure_stage="weiyun_upload", video_path=video_path, error=error)
            _publish_progress({"phase": "upload", "status": "failed", "error": error})
            _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_upload", duration_ms=round((time.monotonic() - started) * 1000), error=error)
            return
        _event("weiyun_publish_started", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="uploading")
        _publish_progress({"phase": "upload", "status": "uploading", "message": "Uploading rendered video to Weiyun"})
        try:
            uploaded = await _run_tool_sync(upload_tool, {"video_path": video_path, "target_dir": "", "overwrite": False})
        except Exception as exc:
            update_session(sid, status="failed", failure_stage="weiyun_upload", video_path=video_path, error=str(exc))
            _publish_progress({"phase": "upload", "status": "failed", "error": str(exc)})
            _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_upload", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
            return
        if not uploaded.success:
            error = uploaded.error or "Weiyun upload failed"
            update_session(sid, status="failed", failure_stage="weiyun_upload", video_path=video_path, error=error)
            _publish_progress({"phase": "upload", "status": "failed", "error": error})
            _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_upload", duration_ms=round((time.monotonic() - started) * 1000), error=error)
            return

        file_id = (uploaded.data or {}).get("file_id")
        _publish_progress({"phase": "upload", "status": "uploaded", "message": "Upload complete"})
        # Use the token-based Weiyun share-link tool (weiyun_share_link). The legacy
        # "weiyun.gen_share_link" name resolved to the mcporter-based wrapper, which
        # is no longer installed; this is the same token tool the standalone
        # weiyun_gen_share_link MCP tool uses.
        share_tool = registry.get("weiyun_share_link")
        if share_tool is None or not file_id:
            error = "weiyun_share_link tool is unavailable" if share_tool is None else "upload returned no file_id"
            update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path, error=error)
            _publish_progress({"phase": "share", "status": "failed", "error": error})
            _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=error)
            return
        try:
            _publish_progress({"phase": "share", "status": "sharing", "message": "Generating Weiyun share link"})
            shared = await _run_tool_sync(share_tool, {"file_list": [file_id], "share_name": title or f"{project}-{batch_id}"})
        except Exception as exc:
            update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path, error=str(exc))
            _publish_progress({"phase": "share", "status": "failed", "error": str(exc)})
            _event("workflow_failed", include_traceback=True, request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=str(exc))
            return
        if not shared.success:
            error = shared.error or "Weiyun share link failed"
            update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path, error=error)
            _publish_progress({"phase": "share", "status": "failed", "error": error})
            _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=error)
            return
        share_url = (shared.data or {}).get("short_url") or (shared.data or {}).get("share_url")
        if not share_url:
            error = "Weiyun share tool returned no share URL"
            update_session(sid, status="failed", failure_stage="weiyun_share", video_path=video_path, error=error)
            _publish_progress({"phase": "share", "status": "failed", "error": error})
            _event("workflow_failed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, status="failed", stage="weiyun_share", duration_ms=round((time.monotonic() - started) * 1000), error=error)
            return
        update_session(sid, status="published", share_url=share_url, video_path=video_path, error=None)
        _publish_progress({"phase": "share", "status": "published", "share_url": share_url, "message": "Share link ready"})
        _event("weiyun_publish_completed", request_id=request_id, session_hash=digest, project_id=project, batch_id=batch_id, render_job_id=job_id, asset_count=asset_count, status="published", duration_ms=round((time.monotonic() - started) * 1000))

    try:
        asyncio.run(_worker())
    except Exception as exc:
        update_session(sid, status="failed", failure_stage="background_crash", error=f"background render job crashed: {exc}")
        _log.exception("background render job crashed for job %s", job_id)
    finally:
        if _queue_ready_event is not None:
            _queue_ready_event.set()
        # Drop the durable job record once this run is terminal (success or
        # failure). Re-dispatched jobs from a restart reuse the same record and
        # only delete it when *their* run finishes.
        try:
            delete_job_record(job_id)
        except Exception:  # noqa: BLE001
            pass


def _queue_media_workflow(
    *, sid: str, project_id: str, job_type: str, video_asset_id: str,
    voice_sample_asset_id: str | None = None, script: str | None = None,
    language: str | None = None, subtitle: bool = True,
    subtitle_style: str = "short_video", title: str | None = None,
) -> dict[str, Any]:
    """Validate session assets and dispatch a durable media workflow."""
    from lib.workbuddy_session import find_asset

    digest = session_hash(sid)
    if not digest:
        return {"success": False, "status": "failed", "stage": "session", "error": "Streamable HTTP Mcp-Session-Id is required"}
    video = find_asset(sid, video_asset_id, project_id=project_id)
    if not video or video.get("type") != "video":
        return {"success": False, "status": "failed", "stage": "validation", "error": "video_asset_id is not a video uploaded in this MCP session"}
    voice_sample = None
    if voice_sample_asset_id:
        voice_sample = find_asset(sid, voice_sample_asset_id, project_id=project_id)
        if not voice_sample or voice_sample.get("type") != "audio":
            return {"success": False, "status": "failed", "stage": "validation", "error": "voice_sample_asset_id is not an audio file uploaded in this MCP session"}

    root = (_PROJECT_ROOT / "projects" / project_id).resolve()
    for asset in (video, voice_sample):
        if not asset:
            continue
        path = Path(asset.get("path", "")).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            return {"success": False, "status": "failed", "stage": "validation", "error": "session asset path is outside the project workspace"}
        if not path.is_file():
            return {"success": False, "status": "failed", "stage": "validation", "error": f"uploaded asset is missing: {path.name}"}

    job = create_media_job(
        session_hash=digest, project_id=project_id, job_type=job_type,
        title=title, metadata={
            "video_asset_id": video_asset_id,
            "voice_sample_asset_id": voice_sample_asset_id,
            "language": language,
            "subtitle": subtitle,
            "subtitle_style": subtitle_style,
        },
    )
    threading.Thread(
        target=_run_media_workflow,
        kwargs={
            "sid": sid, "job_id": job["job_id"], "project_id": project_id,
            "job_type": job_type, "video_path": video["path"],
            "voice_sample_path": voice_sample["path"] if voice_sample else None,
            "script": script, "language": language, "subtitle": subtitle,
            "subtitle_style": subtitle_style, "title": title,
        },
        daemon=True,
    ).start()
    publish(job["job_id"], progress_event(job["job_id"], phase="queue", status="queued", message="Media job queued"))
    return {
        "success": True, "status": "queued", "render_job_id": job["job_id"],
        "job_id": job["job_id"], "project_id": project_id,
        "message": "任务已进入后台队列，请使用 get_render_status 查询进度和分享链接。",
    }


def _run_media_workflow(
    *, sid: str, job_id: str, project_id: str, job_type: str,
    video_path: str, voice_sample_path: str | None, script: str | None,
    language: str | None, subtitle: bool, subtitle_style: str,
    title: str | None,
) -> None:
    """Run caption/voice work, publish the result, and persist every phase."""
    async def _worker() -> None:
        set_mcp_session_id(sid)
        renders = _PROJECT_ROOT / "projects" / project_id / "renders"
        renders.mkdir(parents=True, exist_ok=True)

        def stage(name: str, progress: int, message: str, **changes: Any) -> None:
            update_media_job(job_id, status=name, current_stage=name, progress=progress, **changes)
            publish(job_id, progress_event(job_id, phase=name, status=name, progress=progress, message=message, **changes))

        def required_tool(name: str):
            tool = registry.get(name)
            if tool is None:
                raise RuntimeError(f"{name} tool is not registered")
            return tool

        try:
            segments: list[dict[str, Any]] = []
            working_video = video_path
            if job_type == "cloned_voice":
                stage("voice_cloning", 10, "Cloning voice")
                cloned = await _run_tool_sync(required_tool("voicebox_voice_clone"), {
                    "sample_path": voice_sample_path, "consent": True,
                    "voice_id": f"om-{job_id[:20]}",
                })
                if not cloned.success:
                    raise RuntimeError(cloned.error or "Voicebox voice clone failed")
                clone_data = cloned.data or {}
                voice_id = clone_data.get("provider_voice_id") or clone_data.get("voice_id")
                if not voice_id:
                    raise RuntimeError("Voicebox voice clone returned no voice_id")
                update_media_job(job_id, executor="voicebox", executor_job_id=clone_data.get("job_id"), executor_worker_id=clone_data.get("worker_id"))

                audio_path = str(renders / f"{job_id}-voice.mp3")
                stage("tts_generating", 30, "Generating cloned narration")
                spoken = await _run_tool_sync(required_tool("voicebox_tts"), {
                    "text": script, "voice_id": voice_id, "output_path": audio_path,
                    "subtitle": subtitle, "language": language,
                })
                if not spoken.success:
                    raise RuntimeError(spoken.error or "Voicebox TTS failed")
                spoken_data = spoken.data or {}
                audio_path = spoken_data.get("audio_path") or spoken_data.get("output") or audio_path
                segments = spoken_data.get("segments") or []

                stage("audio_mixing", 50, "Replacing video audio")
                revoiced = str(renders / f"{job_id}-revoiced.mp4")
                mixed = await _run_tool_sync(required_tool("audio_mixer"), {
                    "operation": "replace_video_audio", "video_path": video_path,
                    "audio_path": audio_path, "output_path": revoiced,
                })
                if not mixed.success:
                    raise RuntimeError(mixed.error or "Audio replacement failed")
                working_video = (mixed.data or {}).get("output") or revoiced
                if subtitle and not segments:
                    stage("transcribing", 60, "Generating subtitle timestamps")
                    transcript = await _run_tool_sync(required_tool("transcriber"), {
                        "input_path": audio_path, "language": language,
                        "output_dir": str(renders / f"{job_id}-transcript"),
                    })
                    if not transcript.success:
                        raise RuntimeError(transcript.error or "Narration transcription failed")
                    segments = (transcript.data or {}).get("segments") or []
            else:
                stage("transcribing", 25, "Transcribing source video")
                transcript = await _run_tool_sync(required_tool("transcriber"), {
                    "input_path": video_path, "language": language,
                    "output_dir": str(renders / f"{job_id}-transcript"),
                })
                if not transcript.success:
                    raise RuntimeError(transcript.error or "Video transcription failed")
                segments = (transcript.data or {}).get("segments") or []

            final_video = working_video
            if subtitle:
                if not segments:
                    raise RuntimeError("No timed transcript segments were produced")
                stage("subtitle_burning", 70, "Burning subtitles")
                captioned = str(renders / f"{job_id}-captioned.mp4")
                burned = await _run_tool_sync(required_tool("remotion_caption_burn"), {
                    "input_path": working_video, "output_path": captioned,
                    "segments": segments,
                    "words_per_page": 4 if subtitle_style == "short_video" else 8,
                })
                if not burned.success:
                    raise RuntimeError(burned.error or "Subtitle burn failed")
                final_video = (burned.data or {}).get("output") or captioned

            stage("uploading", 85, "Uploading video to Weiyun", video_path=final_video)
            uploaded = await _run_tool_sync(required_tool("weiyun_upload"), {
                "video_path": final_video, "target_dir": "", "overwrite": False,
            })
            if not uploaded.success:
                raise RuntimeError(uploaded.error or "Weiyun upload failed")
            file_id = (uploaded.data or {}).get("file_id")
            if not file_id:
                raise RuntimeError("Weiyun upload returned no file_id")

            stage("sharing", 95, "Generating Weiyun share link")
            shared = await _run_tool_sync(required_tool("weiyun_share_link"), {
                "file_list": [file_id], "share_name": title or f"{project_id}-{job_id[:8]}",
            })
            if not shared.success:
                raise RuntimeError(shared.error or "Weiyun share link failed")
            share_url = (shared.data or {}).get("short_url") or (shared.data or {}).get("share_url")
            if not share_url:
                raise RuntimeError("Weiyun share tool returned no share URL")
            update_media_job(job_id, status="published", current_stage="published", progress=100, result_url=share_url, video_path=final_video, error_code=None, error_message=None)
            publish(job_id, progress_event(job_id, phase="share", status="published", progress=100, share_url=share_url, message="Share link ready"))
        except Exception as exc:  # noqa: BLE001
            current = get_media_job(job_id) or {}
            failed_stage = current.get("current_stage") or "workflow"
            update_media_job(job_id, status="failed", current_stage=failed_stage, error_code=failed_stage, error_message=str(exc))
            publish(job_id, progress_event(job_id, phase=failed_stage, status="failed", error=str(exc), message="Media workflow failed"))
            _log.exception("media workflow failed for job %s", job_id)

    try:
        asyncio.run(_worker())
    except Exception as exc:  # noqa: BLE001
        update_media_job(job_id, status="failed", current_stage="background_crash", error_code="background_crash", error_message=str(exc))
        _log.exception("media workflow background crash for job %s", job_id)


@mcp.tool()
async def create_captioned_video_share(
    project_id: str, video_asset_id: str, language: Optional[str] = "zh",
    subtitle_style: str = "short_video", title: Optional[str] = None,
) -> dict[str, Any]:
    """Add synchronized subtitles to an uploaded video and return a share job."""
    if subtitle_style not in {"short_video", "standard"}:
        return {"success": False, "status": "failed", "stage": "validation", "error": "subtitle_style must be short_video or standard"}
    return _queue_media_workflow(
        sid=get_mcp_session_id(), project_id=project_id, job_type="captioned_video",
        video_asset_id=video_asset_id, language=language,
        subtitle=True, subtitle_style=subtitle_style, title=title,
    )


@mcp.tool()
async def create_cloned_voice_video_share(
    project_id: str, video_asset_id: str, voice_sample_asset_id: str,
    script: str, audio_mode: str = "replace", subtitle: bool = True,
    language: Optional[str] = "zh", subtitle_style: str = "short_video",
    title: Optional[str] = None, voice_consent: bool = False,
) -> dict[str, Any]:
    """Clone an authorized voice, dub a video, optionally caption, and share."""
    if not voice_consent:
        return {"success": False, "status": "failed", "stage": "consent", "error": "voice_consent=true is required"}
    if audio_mode != "replace":
        return {"success": False, "status": "failed", "stage": "validation", "error": "MVP supports audio_mode=replace only"}
    if not isinstance(script, str) or not script.strip() or len(script) > 10000:
        return {"success": False, "status": "failed", "stage": "validation", "error": "script must contain 1-10000 characters"}
    if subtitle_style not in {"short_video", "standard"}:
        return {"success": False, "status": "failed", "stage": "validation", "error": "subtitle_style must be short_video or standard"}
    return _queue_media_workflow(
        sid=get_mcp_session_id(), project_id=project_id, job_type="cloned_voice",
        video_asset_id=video_asset_id, voice_sample_asset_id=voice_sample_asset_id,
        script=script.strip(), language=language, subtitle=subtitle,
        subtitle_style=subtitle_style, title=title,
    )


def _find_session_by_job(render_job_id: str) -> dict[str, Any] | None:
    """Back-compat alias for ``find_session_by_job_id`` (O(1) index lookup)."""
    return find_session_by_job_id(render_job_id)


def _valid_weiyun_share_url(value: Any) -> bool:
    parsed = urlparse(str(value or ""))
    return parsed.scheme == "https" and bool(parsed.netloc)


_retry_publish_locks: dict[str, threading.Lock] = {}
_retry_publish_locks_guard = threading.Lock()


def _retry_publish_lock(job_id: str) -> threading.Lock:
    with _retry_publish_locks_guard:
        return _retry_publish_locks.setdefault(job_id, threading.Lock())


@mcp.tool()
async def retry_render_publish(render_job_id: str) -> dict[str, Any]:
    """Serialize publish retries for one job to prevent duplicate uploads."""
    job_id = (render_job_id or "").strip()
    state = find_session_by_job_id(job_id)
    if state and state.get("status") == "published" and _valid_weiyun_share_url(state.get("share_url")):
        return {"success": True, "render_job_id": job_id, "status": "published", "stage": None, "share_url": state.get("share_url"), "error": None}
    lock = _retry_publish_lock(job_id)
    if not lock.acquire(blocking=False):
        state = find_session_by_job_id(job_id) or {}
        return {"success": False, "render_job_id": job_id, "status": state.get("status", "uploading"), "stage": "in_progress", "share_url": state.get("share_url"), "error": "A publish retry is already in progress for this render job"}
    try:
        return await _retry_render_publish_impl(job_id)
    finally:
        lock.release()


async def _retry_render_publish_impl(job_id: str) -> dict[str, Any]:
    """Retry only the Weiyun upload/share stages for an existing render.

    This never invokes the renderer.  It is safe to call repeatedly: an
    already published job returns its existing share URL without another
    upload.  A failed retry keeps the persisted local video path intact.
    """
    request_id = get_mcp_request_id() or uuid.uuid4().hex
    state = find_session_by_job_id(job_id)
    if not state:
        error = f"No render job found for render_job_id '{job_id}'"
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": "lookup", "share_url": None, "error": error}

    existing_url = state.get("share_url")
    if state.get("status") == "published" and _valid_weiyun_share_url(existing_url):
        return {"success": True, "render_job_id": job_id, "status": "published", "stage": None, "share_url": existing_url, "error": None}

    # Distinguish three pre-upload conditions so the front-end can stop
    # looping on "retry" for jobs that retry cannot recover:
    #   * render_incomplete — render never finished (MCP killed mid-render,
    #     render_thread crashed, or pre-render validation failed). State was
    #     written with video_path=None and status="failed" / failure_stage
    #     in {"render", "validation", "background_crash"}. Caller must
    #     re-issue create_remotion_video_share to get a fresh render_job_id.
    #   * video_missing — render claimedsuccess (status in
    #     {rendered, uploading, sharing, published} or failure_stage in
    #     {weiyun_upload, weiyun_share}) but the on-disk file is gone.
    #     Retry cannot recover without re-rendering.
    #   * otherwise — video_path is set and the file exists; proceed.
    video_path_raw = state.get("video_path")
    has_video_path = bool(video_path_raw and str(video_path_raw).strip())
    failure_stage = state.get("failure_stage")
    status = state.get("status")

    if not has_video_path and (
        status == "failed"
        or failure_stage in {"render", "validation", "background_crash"}
    ):
        error = (
            f"Render for render_job_id '{job_id}' did not complete "
            f"(status={status!r}, failure_stage={failure_stage!r}); "
            "re-issue create_remotion_video_share to start a fresh render."
        )
        _event(
            "weiyun_publish_retry_failed",
            request_id=request_id,
            session_hash="job-index",
            render_job_id=job_id,
            status="failed",
            stage="render_incomplete",
            error=error,
        )
        return {
            "success": False,
            "render_job_id": job_id,
            "status": "failed",
            "stage": "render_incomplete",
            "share_url": existing_url,
            "error": error,
            "retryable": False,
        }

    video_path = Path(str(video_path_raw or "")).expanduser()
    if not video_path.is_file():
        error = (
            f"Persisted video file is missing on disk: {video_path} "
            f"(status={status!r}, failure_stage={failure_stage!r})"
        )
        _event(
            "weiyun_publish_retry_failed",
            request_id=request_id,
            session_hash="job-index",
            render_job_id=job_id,
            status="failed",
            stage="video_missing",
            error=error,
        )
        return {
            "success": False,
            "render_job_id": job_id,
            "status": "failed",
            "stage": "video_missing",
            "share_url": existing_url,
            "error": error,
            "retryable": False,
        }

    project = state.get("project_id") or "openmontage"
    batch_id = state.get("batch_id") or job_id
    update_session_by_job_id(job_id, status="uploading", failure_stage=None, error=None, video_path=str(video_path))
    _event("weiyun_publish_retry_started", request_id=request_id, session_hash="job-index", project_id=project, batch_id=batch_id, render_job_id=job_id, status="uploading")

    upload_tool = registry.get("weiyun_upload")
    if upload_tool is None:
        stage, error = "weiyun_upload", "weiyun_upload tool is not registered"
        update_session_by_job_id(job_id, status="failed", failure_stage=stage, error=error, video_path=str(video_path))
        _event("weiyun_publish_retry_failed", request_id=request_id, session_hash="job-index", render_job_id=job_id, status="failed", stage=stage, error=error)
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": stage, "share_url": existing_url, "error": error}
    try:
        uploaded = await _run_tool_sync(upload_tool, {"video_path": str(video_path), "target_dir": "", "overwrite": False})
    except Exception as exc:  # noqa: BLE001
        stage, error = "weiyun_upload", str(exc)
        update_session_by_job_id(job_id, status="failed", failure_stage=stage, error=error, video_path=str(video_path))
        _event("weiyun_publish_retry_failed", request_id=request_id, session_hash="job-index", render_job_id=job_id, status="failed", stage=stage, error=error)
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": stage, "share_url": existing_url, "error": error}
    if not uploaded.success:
        stage, error = "weiyun_upload", uploaded.error or "Weiyun upload failed"
        update_session_by_job_id(job_id, status="failed", failure_stage=stage, error=error, video_path=str(video_path))
        _event("weiyun_publish_retry_failed", request_id=request_id, session_hash="job-index", render_job_id=job_id, status="failed", stage=stage, error=error)
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": stage, "share_url": existing_url, "error": error}

    file_id = (uploaded.data or {}).get("file_id")
    share_tool = registry.get("weiyun_share_link")
    if share_tool is None or not file_id:
        stage = "weiyun_share"
        error = "weiyun_share_link tool is unavailable" if share_tool is None else "upload returned no file_id"
        update_session_by_job_id(job_id, status="failed", failure_stage=stage, error=error, video_path=str(video_path))
        _event("weiyun_publish_retry_failed", request_id=request_id, session_hash="job-index", render_job_id=job_id, status="failed", stage=stage, error=error)
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": stage, "share_url": existing_url, "error": error}
    update_session_by_job_id(job_id, status="sharing", failure_stage=None, error=None, video_path=str(video_path))
    try:
        shared = await _run_tool_sync(share_tool, {"file_list": [file_id], "share_name": f"{project}-{batch_id}"})
    except Exception as exc:  # noqa: BLE001
        stage, error = "weiyun_share", str(exc)
        update_session_by_job_id(job_id, status="failed", failure_stage=stage, error=error, video_path=str(video_path))
        _event("weiyun_publish_retry_failed", request_id=request_id, session_hash="job-index", render_job_id=job_id, status="failed", stage=stage, error=error)
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": stage, "share_url": existing_url, "error": error}
    share_url = (shared.data or {}).get("short_url") or (shared.data or {}).get("share_url") if shared.success else None
    if not shared.success or not _valid_weiyun_share_url(share_url):
        stage = "weiyun_share"
        error = (shared.error if not shared.success else None) or "Weiyun share tool returned no valid share URL"
        update_session_by_job_id(job_id, status="failed", failure_stage=stage, error=error, video_path=str(video_path))
        _event("weiyun_publish_retry_failed", request_id=request_id, session_hash="job-index", render_job_id=job_id, status="failed", stage=stage, error=error)
        return {"success": False, "render_job_id": job_id, "status": "failed", "stage": stage, "share_url": existing_url, "error": error}
    update_session_by_job_id(job_id, status="published", failure_stage=None, error=None, share_url=share_url, video_path=str(video_path))
    _event("weiyun_publish_retry_completed", request_id=request_id, session_hash="job-index", project_id=project, batch_id=batch_id, render_job_id=job_id, status="published")
    return {"success": True, "render_job_id": job_id, "status": "published", "stage": None, "share_url": share_url, "error": None}


@mcp.tool()
def get_render_status(render_job_id: str) -> dict[str, Any]:
    """Poll the progress of a video render dispatched by create_remotion_video_share.

    Returns the render job's current status and result. ``status`` is one of:
    ``queued``, ``rendering``, ``rendered``, ``uploading``, ``published``,
    ``failed``. When ``status`` is ``published`` the ``share_url`` field holds
    the Weiyun share link; when ``failed`` the ``stage`` field names the
    failing pipeline stage.
    """
    media = get_media_job(render_job_id, session_hash=session_hash(get_mcp_session_id()))
    if media:
        return {
            "success": True,
            "render_job_id": render_job_id,
            "job_id": render_job_id,
            "job_type": media.get("job_type"),
            "status": media.get("status"),
            "stage": media.get("current_stage"),
            "progress": media.get("progress"),
            "error": media.get("error_message"),
            "project_id": media.get("project_id"),
            "video_path": media.get("video_path"),
            "share_url": media.get("result_url"),
            "executor": media.get("executor"),
            "executor_job_id": media.get("executor_job_id"),
            "executor_worker_id": media.get("executor_worker_id"),
            "updated_at": media.get("updated_at"),
        }
    state = find_session_by_job_id(render_job_id)
    if not state:
        return {"success": False, "error": f"No render job found for render_job_id '{render_job_id}'"}
    status = state.get("status")
    queue_position = state.get("queue_position")
    queue_depth = state.get("queue_depth")
    if status == "rendering" and state.get("render_phase") == "queued_for_slot":
        status = "queued"
        live_position, live_depth = fair_render_queue_snapshot(render_job_id)
        if live_position is not None:
            queue_position, queue_depth = live_position, live_depth
    return {
        "success": True,
        "render_job_id": render_job_id,
        "status": status,
        "stage": state.get("failure_stage"),
        "error": state.get("error"),
        "render_phase": state.get("render_phase"),
        "queue_position": queue_position,
        "queue_depth": queue_depth,
        "batch_id": state.get("batch_id"),
        "project_id": state.get("project_id"),
        "video_path": state.get("video_path"),
        "share_url": state.get("share_url"),
        "updated_at": state.get("updated_at"),
    }


@mcp.tool()
def s3_upload(
    video_path: str,
    visibility: str = "public",
    project_id: Optional[str] = None,
    object_key: Optional[str] = None,
    expire_seconds: Optional[int] = None,
    make_download_page: bool = False,
    additional_files: Optional[list[str]] = None,
    page_title: Optional[str] = None,
    platform_label: str = "s3",
) -> S3UploadResult:
    """Upload a rendered video to an S3-compatible object store.

    Returns a public permanent link or a time-limited pre-signed GET URL,
    and optionally builds a standalone HTML download page for multi-file
    delivery.  Configuration is read from the server environment variables
    (S3_ENDPOINT_URL, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET, etc.); these
    must be set in the OpenMontage ``.env`` before calling.
    """
    tool = registry.get("s3_upload")
    if tool is None:
        return S3UploadResult(success=False, error="s3_upload tool is not registered")

    if visibility not in ("public", "private"):
        return S3UploadResult(success=False, error="visibility must be 'public' or 'private'")

    inputs: dict[str, Any] = {
        "video_path": video_path,
        "visibility": visibility,
        "project_id": project_id,
        "object_key": object_key,
        "make_download_page": make_download_page,
        "page_title": page_title,
        "platform_label": platform_label,
    }
    if expire_seconds is not None:
        inputs["expire_seconds"] = expire_seconds
    if additional_files is not None:
        inputs["additional_files"] = additional_files

    result = tool.execute(inputs)
    data = result.data or {}
    return S3UploadResult(
        success=result.success,
        url=data.get("url"),
        object_key=data.get("object_key"),
        bucket=data.get("bucket"),
        visibility=data.get("visibility"),
        expires_at=data.get("expires_at"),
        download_page_url=data.get("download_page_url"),
        uploaded_files=data.get("uploaded_files") or [],
        publish_log=data.get("publish_log") or {},
        error=result.error,
    )


@mcp.tool()
def dry_run_tool(
    tool_name: str,
    inputs: dict[str, Any],
) -> DryRunResult:
    """Preflight check for a tool execution — shows estimated cost and runtime.

    Does NOT execute the tool. Use this before execute_tool for paid operations
    to understand what the call will cost and how long it will take.
    """
    tool = registry.get(tool_name)
    if tool is None:
        return DryRunResult(
            tool=tool_name,
            estimated_cost_usd=0.0,
            estimated_runtime_seconds=0.0,
            status="not_found",
            would_execute=False,
        )

    info = tool.dry_run(inputs)
    return DryRunResult(
        tool=info["tool"],
        estimated_cost_usd=info["estimated_cost_usd"],
        estimated_runtime_seconds=info["estimated_runtime_seconds"],
        status=info["status"],
        would_execute=info["would_execute"],
    )


# ---------------------------------------------------------------------------
# Pipeline tools
# ---------------------------------------------------------------------------


@mcp.tool()
def list_pipelines() -> list[str]:
    """List all available pipeline manifest names.

    Each pipeline is a complete production workflow (e.g. 'animated-explainer',
    'cinematic', 'talking-head'). Use get_pipeline to load details.
    """
    return pipeline_loader.list_pipelines()


@mcp.tool()
def get_pipeline(name: str) -> dict[str, Any]:
    """Load and return a full pipeline manifest by name.

    The manifest defines stages, tools, review criteria, and approval gates.
    Pipeline names include: animated-explainer, animation, avatar-spokesperson,
    cinematic, clip-factory, documentary-montage, hybrid, localization-dub,
    podcast-repurpose, screen-demo, talking-head, framework-smoke.
    """
    try:
        manifest = pipeline_loader.load_pipeline(name)
        return manifest
    except FileNotFoundError as e:
        return {"error": str(e)}


@mcp.tool()
def get_pipeline_stages(pipeline_name: str) -> list[str]:
    """Get the ordered list of stages for a specific pipeline.

    Returns stage names in execution order (e.g. research -> proposal -> script
    -> scene_plan -> assets -> edit -> compose -> publish).
    """
    try:
        manifest = pipeline_loader.load_pipeline(pipeline_name)
        return pipeline_loader.get_stage_order(manifest)
    except FileNotFoundError:
        return []


# ---------------------------------------------------------------------------
# Checkpoint tools
# ---------------------------------------------------------------------------


def _pipeline_dir() -> Path:
    return _PROJECT_ROOT / "pipeline"


@mcp.tool()
def read_checkpoint(
    project_id: str,
    stage: str,
) -> CheckpointData | dict[str, str]:
    """Read a checkpoint for a specific project and stage.

    Returns the checkpoint data including artifacts, status, and timestamps.
    If no checkpoint exists, returns an error message.
    """
    cp = ckpt.read_checkpoint(_pipeline_dir(), project_id, stage)
    if cp is None:
        return {"error": f"No checkpoint found for project '{project_id}' stage '{stage}'"}
    return CheckpointData(
        version=cp.get("version", "1.0"),
        project_id=cp.get("project_id", project_id),
        pipeline_type=cp.get("pipeline_type", "unknown"),
        stage=cp.get("stage", stage),
        status=cp.get("status", "unknown"),
        timestamp=cp.get("timestamp"),
        artifacts=cp.get("artifacts", {}),
        human_approval_required=cp.get("human_approval_required", False),
        human_approved=cp.get("human_approved", False),
        error=cp.get("error"),
    )


@mcp.tool()
def get_latest_checkpoint(project_id: str) -> CheckpointData | dict[str, str]:
    """Get the most recent checkpoint for a project.

    Useful for checking pipeline progress — returns the last completed
    or in-progress stage with its artifacts.
    """
    cp = ckpt.get_latest_checkpoint(_pipeline_dir(), project_id)
    if cp is None:
        return {"error": f"No checkpoints found for project '{project_id}'"}
    return CheckpointData(
        version=cp.get("version", "1.0"),
        project_id=cp.get("project_id", project_id),
        pipeline_type=cp.get("pipeline_type", "unknown"),
        stage=cp.get("stage", "unknown"),
        status=cp.get("status", "unknown"),
        timestamp=cp.get("timestamp"),
        artifacts=cp.get("artifacts", {}),
        human_approval_required=cp.get("human_approval_required", False),
        human_approved=cp.get("human_approved", False),
        error=cp.get("error"),
    )


@mcp.tool()
def get_pipeline_status(
    project_id: str,
    pipeline_type: Optional[str] = None,
) -> PipelineStatus:
    """Get pipeline progress: completed stages and the next stage to run.

    Returns a structured status showing which stages are done, what's next,
    and the latest checkpoint data. Use this to decide what to execute next.
    """
    if pipeline_type is None:
        pipeline_type = pipeline_loader.get_default_pipeline_name()
    completed = ckpt.get_completed_stages(_pipeline_dir(), project_id, pipeline_type)
    next_stage = ckpt.get_next_stage(_pipeline_dir(), project_id, pipeline_type)
    latest = ckpt.get_latest_checkpoint(_pipeline_dir(), project_id)

    latest_data = None
    if latest:
        latest_data = CheckpointData(
            version=latest.get("version", "1.0"),
            project_id=latest.get("project_id", project_id),
            pipeline_type=latest.get("pipeline_type", "unknown"),
            stage=latest.get("stage", "unknown"),
            status=latest.get("status", "unknown"),
            timestamp=latest.get("timestamp"),
            artifacts=latest.get("artifacts", {}),
            human_approval_required=latest.get("human_approval_required", False),
            human_approved=latest.get("human_approved", False),
            error=latest.get("error"),
        )

    return PipelineStatus(
        project_id=project_id,
        pipeline_type=pipeline_type,
        completed_stages=completed,
        next_stage=next_stage,
        latest_checkpoint=latest_data,
    )


@mcp.tool()
def write_checkpoint(
    project_id: str,
    stage: str,
    status: str,
    artifacts: dict[str, Any],
    pipeline_type: Optional[str] = None,
    style_playbook: Optional[str] = None,
    checkpoint_policy: str = "guided",
    human_approval_required: bool = False,
    human_approved: bool = False,
    review: Optional[dict[str, Any]] = None,
    cost_snapshot: Optional[dict[str, Any]] = None,
    error: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Write a checkpoint to persist pipeline state.

    Called after each stage completes to save artifacts and state.
    The project resumes from the last checkpoint if interrupted.
    """
    try:
        if pipeline_type is None:
            pipeline_type = pipeline_loader.get_default_pipeline_name()
        path = ckpt.write_checkpoint(
            _pipeline_dir(),
            project_id,
            stage,
            status,
            artifacts,
            pipeline_type=pipeline_type,
            style_playbook=style_playbook,
            checkpoint_policy=checkpoint_policy,
            human_approval_required=human_approval_required,
            human_approved=human_approved,
            review=review,
            cost_snapshot=cost_snapshot,
            error=error,
            metadata=metadata,
        )
        return {"success": True, "path": str(path)}
    except Exception as e:
        return {"success": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------------------
# Publish-tier tools (rsync, export bundle)
# ---------------------------------------------------------------------------


@mcp.tool()
def rsync_upload_artifact(
    source_path: str,
    remote_name: Optional[str] = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload a rendered artifact to a public server via SSH/rsync.

    Configuration is read from .env (RSYNC_* variables).
    Returns remote_path and download_url when RSYNC_PUBLIC_BASE_URL is set.
    """
    tool = registry.get("rsync_upload_artifact")
    if tool is None:
        return {"success": False, "error": "rsync_upload_artifact tool is not registered"}
    result = tool.execute({
        "source_path": source_path,
        "remote_name": remote_name,
        "dry_run": dry_run,
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


@mcp.tool()
def export_bundle(
    video_path: str,
    project_name: Optional[str] = None,
    chapters: Optional[list[dict[str, Any]]] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Package a rendered video with metadata into a self-contained export bundle.

    Writes a schema-valid publish_log with status='exported'.
    """
    tool = registry.get("export_bundle")
    if tool is None:
        return {"success": False, "error": "export_bundle tool is not registered"}
    result = tool.execute({
        "video_path": video_path,
        "project_name": project_name,
        "chapters": chapters,
        "metadata": metadata,
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


@mcp.tool()
async def weiyun_upload(
    video_path: str,
    target_dir: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    """Upload a rendered video to Tencent Weiyun (腾讯微云) via the MCP-token flow.

    Token-based upload (no QR-code login / cookies needed). Reads
    WEIYUN_MCP_TOKEN from the server environment (.env). Returns the Weiyun
    file_id and filename on success. Configure the token in the OpenMontage
    `.env` before calling. This is the token-based counterpart to the
    token-based Weiyun upload tool.
    """
    tool = registry.get("weiyun_upload")
    if tool is None:
        return {"success": False, "error": "weiyun_upload tool is not registered"}
    result = await _run_tool_sync(tool, {
        "video_path": video_path,
        "target_dir": target_dir,
        "overwrite": overwrite,
        "mcp_session_id": get_mcp_session_id(),
    })
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


@mcp.tool()
async def weiyun_gen_share_link(
    file_list: list[str] | None = None,
    dir_list: list[str] | None = None,
    share_name: str = "",
    passwd: str = "",
) -> dict[str, Any]:
    """Generate a shareable link for files in Tencent Weiyun (腾讯微云).

    At least one of ``file_list`` or ``dir_list`` is required — pass a
    non-empty list of Weiyun ``file_id`` strings or directory keys to share.
    Returns a short URL that can be shared. Configure ``WEIYUN_MCP_TOKEN``
    in ``.env`` before calling.
    """
    # Use None defaults (not []) — mutable defaults are shared across calls
    # and silently mask missing arguments. Validate at the wrapper layer so
    # the contract violation is reported back to the caller (agent / client)
    # instead of bubbling up from the inner tool's check.
    file_list = list(file_list or [])
    dir_list = list(dir_list or [])
    if not file_list and not dir_list:
        return {
            "success": False,
            "error": "file_list or dir_list is required to generate a share link",
        }
    tool = registry.get("weiyun_share_link")
    if tool is None:
        return {"success": False, "error": "weiyun_share_link tool is not registered"}
    inputs: dict[str, Any] = {"mcp_session_id": get_mcp_session_id()}
    if file_list:
        inputs["file_list"] = file_list
    if dir_list:
        inputs["dir_list"] = dir_list
    if share_name:
        inputs["share_name"] = share_name
    if passwd:
        inputs["passwd"] = passwd
    result = await _run_tool_sync(tool, inputs)
    return {"success": result.success, "data": result.data, "artifacts": result.artifacts, "error": result.error}


# ---------------------------------------------------------------------------
# Bearer token auth
# ---------------------------------------------------------------------------


def _load_mcp_token() -> Optional[str]:
    """Load the MCP API token from the MCP_API_TOKEN env var (or .env file).

    registry._load_dotenv() already loads .env into os.environ, so this usually
    resolves on the first try. Reading the .env file directly is a fallback for
    cases where dotenv was not applied.
    """
    token = os.environ.get("MCP_API_TOKEN")
    if token:
        return token.strip()
    env_path = _PROJECT_ROOT / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("MCP_API_TOKEN="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except OSError:
            pass
    return None


class BearerTokenAuthMiddleware:
    """ASGI middleware enforcing `Authorization: Bearer <token>` on every HTTP request.

    Returns 401 with a WWW-Authenticate challenge when the header is missing or
    wrong. Comparison uses hmac.compare_digest to avoid timing attacks.
    """

    def __init__(self, app, token: str):
        self.app = app
        self._expected = token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        # Extract client info for logging
        client_host = ""
        client_port = ""
        if scope.get("client"):
            client_host = scope["client"][0]
            client_port = scope["client"][1]

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        method = scope.get("method", "GET")

        # Browser login/callback and user-scoped web APIs authenticate with the
        # server-side HttpOnly session cookie, not the shared MCP bearer token.
        # Keep this exception narrowly scoped to /web; MCP and SSE remain
        # bearer-protected.
        if path == "/web" or path.startswith("/web/"):
            return await self.app(scope, receive, send)

        # Log request with method and path
        auth_present = bool(headers.get(b"authorization", b"").startswith(b"Bearer "))
        session_header_present, request_session = _get_session_header_from_scope(scope)
        request_id = headers.get(b"x-request-id", b"").decode("ascii", errors="ignore").strip() or uuid.uuid4().hex
        _log.info("Request: %s %s from %s:%s auth=%s session_hash=%s request_id=%s", method, path, client_host, client_port, "YES" if auth_present else "NO", session_hash(request_session), request_id)

        # Read and log request body for POST to /mcp
        body = b""
        if method == "POST" and path == "/mcp":
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    break
                body += message.get("body", b"")
                if not message.get("more_body", False):
                    break
            _log.info("Body received from %s:%s bytes=%d session_hash=%s request_id=%s", client_host, client_port, len(body), session_hash(request_session), request_id)
            # Re-dispatch body by wrapping receive
            async def _receive():
                return {"type": "http.request", "body": body, "more_body": False}
            scope["_body_consumed"] = True
        else:
            _receive = receive

        if _has_duplicate_security_headers(scope):
            _log.warning("400 Bad Request: duplicate security header")
            return await self._reject_bad_request(
                scope, _receive, send, "Duplicate security header."
            )

        provided = headers.get(b"authorization", b"")
        if not provided.startswith(b"Bearer "):
            _log.warning("401 Unauthorized: Missing Bearer token from %s:%s", client_host, client_port)
            return await self._reject(scope, _receive, send)

        token = provided[len(b"Bearer "):].strip()
        if not hmac.compare_digest(token, self._expected):
            # Track token identity via SHA-256 prefix so 401 patterns (e.g.
            # client token drift from 192.168.20.172) can be diagnosed without
            # leaking raw token bytes into log files.
            _log.warning(
                "401 Unauthorized: Invalid token from %s:%s token_hash=%s token_len=%d",
                client_host, client_port,
                hashlib.sha256(token).hexdigest()[:16],
                len(token),
            )
            return await self._reject(scope, _receive, send)

        _log.info("Auth OK: %s:%s", client_host, client_port)
        if session_header_present and request_session is None:
            _log.warning("400 Bad Request: malformed Mcp-Session-Id")
            return await self._reject_bad_request(
                scope, _receive, send, "Malformed Mcp-Session-Id header."
            )

        # User attribution is accepted only with a dedicated vclaw HMAC
        # assertion.  Possession of the ordinary shared MCP bearer token must
        # never let a direct caller forge X-VClaw-User-Id.
        raw_uid = headers.get(b"x-vclaw-user-id", b"")
        raw_assertion = headers.get(_ASSERTION_HEADER, b"")
        uid_token = None
        if request_session and not raw_uid and not raw_assertion:
            registered = _principal_registry.lookup(request_session)
            if registered is not None and registered.kind == "user":
                _log.warning("401 Unauthorized: user session assertion missing")
                return await self._reject(scope, _receive, send)
        if raw_uid or raw_assertion:
            if not raw_uid or not raw_assertion:
                _log.warning("401 Unauthorized: incomplete vclaw user assertion")
                return await self._reject(scope, _receive, send)
            sanitized = _verify_vclaw_assertion(
                raw_uid, raw_assertion, method=method, path=path,
                session_id=request_session or "", body=body,
            )
            if sanitized is None:
                _log.warning("401 Unauthorized: invalid vclaw user assertion")
                return await self._reject(scope, _receive, send)
            uid_token = _user_id_ctx.set(sanitized)
            _log.info("vclaw user id attached: %s...", sanitized[:8])
            # Phase B — durable session→principal registry. We bind only when
            # a session id and a verified user assertion are both present.
            if request_session:
                try:
                    _principal_registry.bind(
                        request_session,
                        _Principal(kind="user", principal_id=sanitized, tenant_id=None),
                    )
                except _PrincipalOwnerConflict:
                    # The session id is already owned by a different
                    # principal; never allow the assertion to retarget it.
                    _user_id_ctx.reset(uid_token)
                    return await self._reject_session_owner_conflict(scope, _receive, send)
                except (ValueError, _PrincipalNotFound):
                    _log.warning("principal registry bind skipped session_hash=%s",
                                 session_hash(request_session))
        session_id = request_session
        session_token = set_mcp_session_id(session_id)
        request_token = set_mcp_request_id(request_id)
        initialize_token = _initialize_request_ctx.set(
            bool(
                not request_session
                and method == "POST"
                and path == "/mcp"
                and isinstance(body, bytes)
                and _is_initialize_body(body)
            )
        )
        try:
            return await self.app(scope, _receive, send)
        finally:
            _initialize_request_ctx.reset(initialize_token)
            reset_mcp_request_id(request_token)
            reset_mcp_session_id(session_token)
            if uid_token is not None:
                _user_id_ctx.reset(uid_token)

    @staticmethod
    async def _reject(scope, receive, send):
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "unauthorized", "message": "Missing or invalid Bearer token."},
            status_code=401,
            headers={"WWW-Authenticate": 'Bearer realm="openmontage-mcp"'},
        )
        await response(scope, receive, send)

    @staticmethod
    async def _reject_bad_request(scope, receive, send, message: str):
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {"error": "bad_request", "message": message}, status_code=400
        )
        await response(scope, receive, send)

    @staticmethod
    async def _reject_session_owner_conflict(scope, receive, send):
        """403 for an ``Mcp-Session-Id`` already owned by another principal.

        403 rather than 401: the Bearer token *was* valid, the caller is
        simply not the owner of the session it is addressing. The body names
        no principal so the response cannot be used to probe who owns a
        given session id.
        """
        from starlette.responses import JSONResponse

        response = JSONResponse(
            {
                "error": "session_owner_conflict",
                "message": "This Mcp-Session-Id is bound to a different user. "
                           "Start a new MCP session.",
            },
            status_code=403,
        )
        await response(scope, receive, send)


# ---------------------------------------------------------------------------
# SSE progress endpoint
# ---------------------------------------------------------------------------
# Clients subscribe to live render progress for a job over Server-Sent Events.
# The render worker publishes coarse (render/upload/share) and frame-level
# (Remotion percentage) events onto the bus in lib.render_progress; this
# handler drains them and forwards each as an SSE ``data:`` frame. A snapshot
# of the current session state is sent first so a late subscriber (or one that
# joined after the job started) sees where things stand immediately. The route
# is mounted on the inner Starlette app, so it inherits the Bearer auth layer.

async def render_progress_sse(request: "Request"):
    """Stream live render progress for ``render_job_id`` as Server-Sent Events."""
    from starlette.responses import StreamingResponse

    job_id = request.path_params.get("job_id", "")
    q = subscribe(job_id)
    state = find_session_by_job_id(job_id)
    media = get_media_job(job_id) if state is None else None

    async def event_generator():
        # Initial snapshot so clients joining mid-flight get current state.
        if state:
            snapshot_status = state.get("status")
            snapshot_position = state.get("queue_position")
            snapshot_depth = state.get("queue_depth")
            if snapshot_status == "rendering" and state.get("render_phase") == "queued_for_slot":
                snapshot_status = "queued"
                live_position, live_depth = fair_render_queue_snapshot(job_id)
                if live_position is not None:
                    snapshot_position, snapshot_depth = live_position, live_depth
            snap = progress_event(
                job_id,
                phase="snapshot",
                status=snapshot_status,
                stage=state.get("failure_stage"),
                error=state.get("error"),
                render_phase=state.get("render_phase"),
                queue_position=snapshot_position,
                queue_depth=snapshot_depth,
                share_url=state.get("share_url"),
                video_path=state.get("video_path"),
            )
        elif media:
            snap = progress_event(
                job_id,
                phase="snapshot",
                status=media.get("status"),
                stage=media.get("current_stage"),
                error=media.get("error_message"),
                percent=media.get("progress"),
                share_url=media.get("result_url"),
                video_path=media.get("video_path"),
                job_type=media.get("job_type"),
            )
        else:
            snap = progress_event(
                job_id, phase="snapshot", status="unknown",
                message=f"No render job found for '{job_id}'",
            )
        yield f"data: {json.dumps(snap, ensure_ascii=False)}\n\n"

        try:
            while True:
                try:
                    ev = await asyncio.to_thread(q.get, 1.0)
                except queue.Empty:
                    # heartbeat keeps nginx/proxy from closing an idle stream
                    yield ": keep-alive\n\n"
                    continue
                yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if ev.get("status") in ("published", "failed"):
                    yield f"data: {json.dumps(progress_event(job_id, phase='done', status=ev.get('status')), ensure_ascii=False)}\n\n"
                    break
        finally:
            unsubscribe(job_id, q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx proxy buffering for SSE
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Startup queue drain (restart recovery for waiting jobs)
# ---------------------------------------------------------------------------
def _drain_queued_jobs(requeued_ids: list[str]) -> int:
    """Re-dispatch render jobs that were waiting for a slot at restart time.

    ``recover_orphans_and_rebuild_index`` leaves ``queued_for_slot`` sessions
    intact and returns their job ids here. Each job's durable record (written
    by ``save_job_record`` at dispatch) carries the exact kwargs needed to
    re-run the pipeline, so we simply spawn a fresh background thread per job.
    Jobs whose record is missing are marked failed (cannot be re-run).
    """
    if not requeued_ids:
        return 0
    drained = 0
    requeued = {job_id for job_id in requeued_ids if job_id}
    records = all_job_records()
    ordered_ids = [job_id for job_id in records if job_id in requeued]
    ordered_ids.extend(job_id for job_id in requeued_ids if job_id and job_id not in records)
    for job_id in ordered_ids:
        if not job_id:
            continue
        record = records.get(job_id) or load_job_record(job_id)
        if not record:
            _log.warning("Cannot re-dispatch job %s: no durable record; marking failed", job_id)
            try:
                fail_job_by_id(job_id, stage="orphaned",
                               error="render job record lost on restart; please retry")
            except Exception as exc:  # noqa: BLE001
                _log.warning("fail_job_by_id(%s) failed: %s", job_id, exc)
            continue
        # The re-dispatched thread re-enters the queue and re-sets render_phase
        # via its slot_waiting/slot_acquired events, so no pre-clearing needed.
        queue_ready = threading.Event()
        threading.Thread(
            target=_run_render_job,
            kwargs={**record, "_queue_ready_event": queue_ready},
            daemon=True,
        ).start()
        # Preserve persisted submission order across restart: do not launch
        # the next worker until this one has re-entered the fair gate.
        queue_ready.wait(timeout=10)
        drained += 1
        _log.info("Re-dispatched waiting render job %s after restart", job_id)
    return drained


def _http_keep_alive_seconds() -> int:
    """Keep Streamable HTTP connections alive beyond normal status polling.

    Uvicorn defaults to five seconds, exactly matching FrameFlow's polling
    interval. Under CPU load this creates a race where the server closes an
    idle connection just as Go reuses it for a POST, which surfaces as
    ``connection reset by peer`` even though the render continues normally.
    """
    raw = os.environ.get("MCP_HTTP_KEEP_ALIVE_SECONDS", "30")
    try:
        return min(300, max(10, int(raw)))
    except ValueError:
        _log.warning("Invalid MCP_HTTP_KEEP_ALIVE_SECONDS=%r; using 30", raw)
        return 30


# ---------------------------------------------------------------------------
# Voicebox reverse-proxy (/voicebox/mcp/{path} → voicebox :17493/mcp/{path})
# ---------------------------------------------------------------------------
# Multiplexes voicebox's FastMCP server onto the OpenMontage :8900 origin so
# clients only need one upstream + one Bearer credential. Bearer auth is
# enforced at :8900 by BearerTokenAuthMiddleware; this proxy strips the
# Authorization header before forwarding because voicebox uses
# X-Voicebox-Client-Id for identity and rejects unknown auth. Loopback hop to
# voicebox's own 127.0.0.1:17493 is trusted because :8900 already gated entry.
# SSE is preserved by streaming the upstream response back unbuffered.

_VOICEBOX_MAX_BODY_BYTES = 256 * 1024 * 1024  # 256 MB ASGI-layer cap
_VOICEBOX_HOP_BY_HOP = frozenset({
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailers", b"transfer-encoding", b"upgrade", b"content-length",
    b"host",
})


async def _voicebox_proxy_send_json(send, status: int, payload: dict) -> None:
    """Emit a minimal JSON error response at the ASGI layer."""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ],
    })
    await send({
        "type": "http.response.body",
        "body": body,
        "more_body": False,
    })


class _VoiceboxProxyApp:
    """ASGI reverse-proxy class wrapper.

    Starlette's Route treats plain functions as request endpoints (``f(request)``),
    not raw ASGI apps. Wrapping the proxy in a class instance forces the ASGI
    ``(scope, receive, send)`` dispatch path so we can stream the request body
    with our own 256 MB cap.
    """

    async def __call__(self, scope, receive, send):
        return await _voicebox_proxy_handler(scope, receive, send)


async def _voicebox_proxy_handler(scope, receive, send):
    """ASGI reverse-proxy: forward /voicebox/mcp/{path} → voicebox :17493."""
    if scope["type"] != "http":
        return  # Lifespan / websocket passthrough (none expected on this route)

    inbound_path = scope.get("path", "") or ""
    if inbound_path.startswith("/voicebox"):
        suffix = inbound_path[len("/voicebox"):]
    else:
        suffix = inbound_path

    # FastMCP requires a trailing slash on the /mcp mount itself.
    if suffix == "/mcp":
        suffix = "/mcp/"

    # Read VOICEBOX_UPSTREAM_URL lazily so voicebox restarts are picked up
    # without a process restart on the OpenMontage side.
    upstream_base = (
        os.environ.get("VOICEBOX_UPSTREAM_URL", "http://127.0.0.1:17493")
        .rstrip("/")
    )
    upstream_url = f"{upstream_base}{suffix}"

    # Stream-read the request body with the ASGI-layer 256 MB cap.
    body = bytearray()
    while True:
        message = await receive()
        if message["type"] == "http.disconnect":
            break
        chunk = message.get("body", b"") or b""
        if chunk:
            body.extend(chunk)
        if len(body) > _VOICEBOX_MAX_BODY_BYTES:
            _log.warning(
                "voicebox_proxy: 413 payload_too_large path=%s bytes=%d",
                inbound_path, len(body),
            )
            await _voicebox_proxy_send_json(send, 413, {
                "error": "payload_too_large",
                "max_bytes": _VOICEBOX_MAX_BODY_BYTES,
            })
            return
        if not message.get("more_body", False):
            break

    # Build outbound headers: strip Authorization, set Accept, ensure
    # X-Voicebox-Client-Id, preserve Mcp-Session-Id.
    inbound_headers = scope.get("headers") or []
    outbound_headers: list[tuple[bytes, bytes]] = []
    has_accept = False
    has_client_id = False
    for name, value in inbound_headers:
        lname = name.lower()
        if lname == b"authorization":
            # Voicebox uses X-Voicebox-Client-Id; :8900 already authenticated.
            continue
        if lname == b"accept":
            has_accept = True
        if lname == b"x-voicebox-client-id":
            has_client_id = True
        if lname in _VOICEBOX_HOP_BY_HOP:
            continue
        outbound_headers.append((name, value))

    if not has_client_id:
        outbound_headers.append((b"x-voicebox-client-id", b"voicebox-relay"))
    if not has_accept:
        outbound_headers.append(
            (b"accept", b"application/json, text/event-stream")
        )

    method = scope.get("method", "GET")
    client = scope.get("client")
    client_str = f"{client[0]}:{client[1]}" if client else "unknown"

    # Forward via httpx with streaming response so SSE stays live.
    timeout = httpx.Timeout(connect=10.0, read=None, write=30.0, pool=10.0)
    upstream_resp = None
    try:
        async with httpx.AsyncClient(timeout=timeout) as hx:
            upstream_req = hx.build_request(
                method=method,
                url=upstream_url,
                headers=outbound_headers,
                content=bytes(body),
            )
            upstream_resp = await hx.send(upstream_req, stream=True)

            # Forward response headers, stripping hop-by-hop.
            response_headers: list[tuple[bytes, bytes]] = []
            for name, value in upstream_resp.headers.raw:
                if name.lower() in _VOICEBOX_HOP_BY_HOP:
                    continue
                response_headers.append((name, value))

            await send({
                "type": "http.response.start",
                "status": upstream_resp.status_code,
                "headers": response_headers,
            })

            # Stream upstream body back unchanged (preserves SSE framing).
            async for chunk in upstream_resp.aiter_raw():
                if not chunk:
                    continue
                await send({
                    "type": "http.response.body",
                    "body": chunk,
                    "more_body": True,
                })
    except httpx.RequestError as exc:
        _log.warning(
            "voicebox_proxy: 502 upstream_unreachable client=%s url=%s err=%s",
            client_str, upstream_url, exc,
        )
        await _voicebox_proxy_send_json(send, 502, {
            "error": "upstream_unreachable",
            "upstream": upstream_base,
            "detail": str(exc),
        })
        return
    finally:
        if upstream_resp is not None:
            try:
                await upstream_resp.aclose()
            except Exception:
                pass

    await send({
        "type": "http.response.body",
        "body": b"",
        "more_body": False,
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "gen-token":
        print(secrets.token_urlsafe(32))
        sys.exit(0)

    transport = sys.argv[1] if len(sys.argv) > 1 else "streamable-http"
    _log.info("Starting OpenMontage MCP server on port %d (transport=%s)", int(os.environ.get("MCP_PORT", "8900")), transport)
    _log.info("%s tools discovered; provider availability is checked lazily", len(_discovered))

    _api_token = _load_mcp_token()
    if _api_token:
        _log.info("Bearer token auth ENABLED — clients must send 'Authorization: Bearer <MCP_API_TOKEN>'")
    else:
        _log.warning("MCP_API_TOKEN is not set — server is running WITHOUT authentication.")
        _log.warning("Do NOT expose port 8900 to the public internet until you set a token.")
        _log.warning("Generate one with:  python mcp_server.py gen-token")

    # Recover orphaned renders left mid-flight by a previous crash/restart and
    # rebuild the job→session index from disk (also self-heals a corrupted or
    # missing index). Must run before the server starts accepting traffic so
    # get_render_status stays correct and O(1).
    try:
        stats = recover_orphans_and_rebuild_index()
        _log.info("Orphan recovery: %d session(s) marked failed, %d job(s) indexed, %d waiting job(s) to re-dispatch",
                  stats["orphaned"], stats["indexed"], stats.get("requeued", 0))
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("Orphan recovery failed (continuing startup): %s", exc)
        stats = {}

    try:
        recovered_media = recover_media_jobs()
        if recovered_media:
            _log.info("Media job recovery: %d interrupted job(s) marked failed", recovered_media)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("Media job recovery failed (continuing startup): %s", exc)

    # Re-dispatch render jobs that were merely *waiting for a slot* (not actively
    # rendering) when the process last died. Their durable job records let us
    # rebuild the exact pipeline without double-rendering in-flight work.
    try:
        drained = _drain_queued_jobs(stats.get("_requeued_ids", []))
        if drained:
            _log.info("Re-dispatched %d waiting render job(s) after restart", drained)
    except Exception as exc:  # pragma: no cover - defensive
        _log.warning("Queue drain failed (continuing startup): %s", exc)

    if transport == "streamable-http":
        import uvicorn
        keep_alive_seconds = _http_keep_alive_seconds()
        _log.info("Streamable HTTP keep-alive timeout: %ds", keep_alive_seconds)
        app = mcp.streamable_http_app()
        # Web login is mounted on the same origin as MCP so a reverse proxy
        # only needs one upstream. Its routes enforce the browser session
        # cookie and never expose raw provider credentials.
        app.router.routes.append(build_web_mount(default_user_store(_PROJECT_ROOT)))
        # Live render-progress stream (SSE). Mounted on the inner app so it
        # inherits the Bearer auth middleware applied just below.
        app.router.add_route(
            "/render-progress/{job_id}", render_progress_sse, methods=["GET"]
        )
        # Voicebox MCP reverse-proxy. Multiplexes the voicebox FastMCP server
        # (loopback :17493) onto the OpenMontage :8900 origin at
        # /voicebox/mcp/{path}. Bearer auth is enforced by
        # BearerTokenAuthMiddleware just below; this handler strips the
        # Authorization header before forwarding (voicebox uses
        # X-Voicebox-Client-Id instead), and trusts the loopback hop because
        # :8900 already gated the entry. SSE is preserved by streaming the
        # upstream response back unbuffered.
        app.router.add_route(
            "/voicebox/mcp/{path:path}",
            _VoiceboxProxyApp(),
            methods=["GET", "POST", "DELETE"],
        )
        if _api_token:
            app = BearerTokenAuthMiddleware(app, _api_token)
        if sys.platform == "win32":
            # Windows lacks AF_UNIX; uvicorn's socket.fromfd(fd, AF_UNIX)
            # crashes there. Let uvicorn bind directly via host/port.
            config = uvicorn.Config(
                app, host="0.0.0.0", port=int(os.environ.get("MCP_PORT", "8900")),
                timeout_keep_alive=keep_alive_seconds,
            )
        else:
            import socket
            # Dual-stack socket: IPv6 + IPv4 via IPV6_V6ONLY=0
            sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _mcp_port = int(os.environ.get("MCP_PORT", "8900"))
            sock.bind(("::", _mcp_port))
            sock.listen(2048)
            config = uvicorn.Config(
                app, fd=sock.fileno(), timeout_keep_alive=keep_alive_seconds,
            )
        server = uvicorn.Server(config)

        async def _serve_with_health():
            # 默认 executor 健康自愈监控（与本循环同跑）：探测到 to_thread
            # 卡死即替换默认 executor，避免 upload_asset_chunk 等静默挂起。
            _start_executor_health_monitor()
            await server.serve()

        asyncio.run(_serve_with_health())
    else:
        mcp.run(transport=transport)
