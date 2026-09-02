"""Durable ``session_id → Principal`` registry (Phase B of user isolation).

Why this exists
---------------

Phase 3 of ``docs/user-isolation-via-mcp-session.md`` set a per-request
ContextVar (``current_user_id``) inside ``BearerTokenAuthMiddleware``.  FastMCP
Streamable HTTP runs every tool in a per-session *background* task, so the
ContextVar set in the per-request task is invisible to the tool — Phase 3 alone
cannot enforce per-user isolation.

Phase B adds a durable registry keyed by ``Mcp-Session-Id`` so the tool can
look up the principal in its own task. The ContextVar stays as a fast-path
cache; this SQLite-backed store is the authoritative source across task
boundaries and process restarts.

Storage
-------

SQLite file at ``projects/.mcp_sessions/principals.db`` (single file; the
parent ``projects/.mcp_sessions/`` already exists and is shared with
``workbuddy_session`` for session digests). One table, indexed by session id;
startup performs an idempotent additive migration for columns introduced by
newer builds. TTL is written (``expires_at``) but not enforced in this phase.

Multi-worker safety
-------------------

``sqlite3.connect(..., check_same_thread=False)`` keeps a per-thread connection
in ``threading.local`` so different ASGI workers (one thread per request on
Starlette's default executor) can hit the same file without serialising
through a single lock.

Cross-process safety on POSIX uses the same fcntl-flocks pattern as
``lib/workbuddy_session._flock_for``. Windows fcntl is a stub there; here we
compensate with a ``sqlite3.OperationalError('database is locked')`` retry
loop with exponential backoff (up to 5 attempts). Brief locks under contention
are tolerable; persistent locks are not (we surface the error after 5
attempts).

namespace_key
-------------

``namespace_key = HMAC_SHA256(OPENMONTAGE_PRINCIPAL_HASH_SECRET, principal_id)``,
truncated to the first 16 bytes = 32 hex characters. The output is pure hash
material: no external input, no sanitize needed on the key itself. We still
whitelist ``principal_id`` at bind time so a hostile header can't smuggle a
path-traversal component.

The secret is read once at module import. If the env var is missing we fall
back to a stable default so dev / CI installations still work, but we emit a
single ``WARNING`` log on first read so production deployments are reminded to
rotate it via the env var.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Final, Literal, Optional

from lib.principal_sanitize import (
    MAX_SESSION_ID_LEN,
    sanitize_principal_id,
    sanitize_session_id,
)


_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

ROOT: Final[Path] = Path(__file__).resolve().parent.parent
STATE_DIR: Path = ROOT / "projects" / ".mcp_sessions"
# ``DB_PATH`` is rebindable (tests point the module at a tmp path via
# ``configure()``) so it is not declared ``Final`` despite the obvious
# intent. Production code never reassigns it.
DB_PATH: Path = STATE_DIR / "principals.db"


# ---------------------------------------------------------------------------
# Secret + namespace_key
# ---------------------------------------------------------------------------

_DEFAULT_HASH_SECRET = "openmontage-principal-namespace-v2"
_ENV_SECRET = "OPENMONTAGE_PRINCIPAL_HASH_SECRET"
_ENV_OLD_SECRET = "OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD"
_ENV_KEY_VERSION = "OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION"
_ENV_OLD_KEY_VERSION = "OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION_OLD"

_secret_lock = threading.Lock()
_secret_value: Optional[bytes] = None
_secret_warned: bool = False


def _load_secret() -> bytes:
    """Read the HMAC secret from env; fall back to a stable default with one warning.

    The active key is deliberately cached. A process restart is the rotation
    boundary; configure the old key in ``_ENV_OLD_SECRET`` until old rows have
    been migrated.
    """
    global _secret_value, _secret_warned
    with _secret_lock:
        if _secret_value is not None:
            return _secret_value
        raw = os.environ.get(_ENV_SECRET)
        if raw:
            _secret_value = raw.encode("utf-8")
        else:
            _secret_value = _DEFAULT_HASH_SECRET.encode("utf-8")
            if not _secret_warned:
                _log.warning(
                    "OPENMONTAGE_PRINCIPAL_HASH_SECRET is not set; falling back to a "
                    "stable default. Set the env var in production so namespace_keys "
                    "do not collide across deployments sharing the same default."
                )
                _secret_warned = True
        return _secret_value


def _load_old_secret() -> Optional[bytes]:
    """Return the explicitly configured previous HMAC key, if any.

    There is no implicit/default old key: a missing old secret makes a row
    unverifiable and therefore fail closed.
    """
    raw = os.environ.get(_ENV_OLD_SECRET)
    return raw.encode("utf-8") if raw else None


def _key_version_from_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


# ---------------------------------------------------------------------------
# namespace_key versioning and rotation
# ---------------------------------------------------------------------------
#
# The v1 namespace_key is ``HMAC_SHA256(secret, principal_id)[:16].hex``.
# ``key_version`` identifies the active derivation generation. The derivation
# remains HMAC-SHA256 for every generation; changing the secret is therefore
# a valid rotation without rewriting existing rows. During a rolling restart,
# lookup verifies old rows with ``OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD``.

_NAMESPACE_KEY_VERSION = 1


def current_key_version() -> int:
    """Return the active key version, defaulting to the legacy value 1."""
    return _key_version_from_env(_ENV_KEY_VERSION, _NAMESPACE_KEY_VERSION)


def old_key_version() -> int:
    """Return the configured previous key version.

    A secret-only rotation keeps the old rows at the active version. When the
    active key id changes, old rows conventionally carry the preceding id.
    Set ``OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION_OLD`` to remove ambiguity.
    """
    active = current_key_version()
    return _key_version_from_env(_ENV_OLD_KEY_VERSION, max(1, active - 1) if active > 1 else active)


def _compute_namespace_key_with_secret(principal_id: str, secret: bytes) -> str:
    digest = hmac.new(secret, principal_id.encode("utf-8"), hashlib.sha256).digest()
    return digest[:16].hex()


def compute_namespace_key(
    principal_id: str, *, key_version: Optional[int] = None
) -> str:
    """Return the 32-hex-char namespace key for ``principal_id``.

    The default uses the active key version. Explicit versions are metadata
    used to resolve rows; key material is selected by the active secret.
    """
    sanitized = sanitize_principal_id(principal_id)
    if sanitized is None or sanitized != principal_id:
        raise ValueError(f"invalid principal_id: {principal_id!r}")
    principal_id = sanitized
    if key_version is None:
        key_version = current_key_version()
    if not isinstance(key_version, int) or isinstance(key_version, bool) or key_version <= 0:
        raise ValueError("key_version must be a positive integer")
    return _compute_namespace_key_with_secret(principal_id, _load_secret())


# ---------------------------------------------------------------------------
# Principal dataclass
# ---------------------------------------------------------------------------


class PrincipalNotFound(LookupError):
    """Raised by ``require()`` when no binding exists for a session id."""


class PrincipalOwnerConflict(Exception):
    """Raised by ``bind()`` when the session is already owned by someone else.

    The owner of a binding is **immutable**: once ``session_id`` is bound to a
    principal, only that same principal (same ``kind`` *and* ``principal_id``)
    may renew it. A bind carrying a different owner is a session-hijack
    signal — ``bind`` refuses it, logs it, and leaves the stored row untouched.

    Derives straight from ``Exception`` rather than ``ValueError`` /
    ``OSError`` / ``LookupError`` on purpose: several callers already wrap
    registry calls in broad ``except ValueError`` blocks, and a security
    refusal must never be swallowed by one of them silently.
    """


@dataclass(frozen=True)
class Principal:
    """Authenticated identity bound to one MCP session.

    ``namespace_key`` is derived from ``principal_id`` alone (``HMAC(secret,
    principal_id)``); the field is intentionally not part of ``__init__`` so a
    caller cannot pass a key that disagrees with the principal — a constructed
    ``Principal`` is always self-consistent.
    """

    kind: Literal["user", "service"]
    principal_id: str
    tenant_id: Optional[str] = None
    namespace_key: str = field(init=False, repr=True, compare=False)
    key_version: int = field(init=False, repr=True, compare=False, default=_NAMESPACE_KEY_VERSION)

    def __post_init__(self) -> None:
        if self.kind not in ("user", "service"):
            raise ValueError(f"invalid Principal kind: {self.kind!r}")
        if not isinstance(self.principal_id, str):
            raise ValueError("principal_id must be a string")
        # Defense in depth: principal_id must already be sanitised if it came
        # from the network. The Principal dataclass is also used in test code
        # with hand-crafted values, so the same check runs here too.
        sanitized = sanitize_principal_id(self.principal_id)
        if sanitized is None or sanitized != self.principal_id:
            raise ValueError(f"invalid principal_id for Principal: {self.principal_id!r}")
        if self.tenant_id is not None and not isinstance(self.tenant_id, str):
            raise ValueError(f"tenant_id must be str or None, got {type(self.tenant_id).__name__}")
        # frozen=True blocks __setattr__; bypass for the auto-computed field.
        object.__setattr__(self, "key_version", current_key_version())
        object.__setattr__(self, "namespace_key", compute_namespace_key(
            sanitized, key_version=self.key_version
        ))


# ---------------------------------------------------------------------------
# SQLite plumbing
# ---------------------------------------------------------------------------

# Thread-local connection slot so check_same_thread=False stays correct per
# thread (a single connection shared across threads would still serialise
# implicitly under the GIL, but SQLite's own thread-affinity checks are
# active without the flag).
_tls = threading.local()
# Single mutex around connection setup to avoid two threads racing on
# CREATE TABLE / PRAGMA when the DB file is freshly created.
_setup_lock = threading.Lock()
_setup_done = False


def _connect(db_path: Optional[Path] = None) -> sqlite3.Connection:
    # Resolve the path inside the function so ``configure()`` can swap the
    # DB file at test time without re-importing the module. ``DB_PATH`` is
    # looked up by name each call (it's a module global, not a default arg)
    # so the rebinding in ``configure`` takes effect immediately.
    target = db_path if db_path is not None else DB_PATH
    # sqlite3.connect does not create parent directories.  Ensure this happens
    # before opening the file so a fresh deployment cannot fail before schema
    # initialization gets a chance to run.
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(target),
        timeout=10.0,                # wait up to 10s before raising "locked"
        check_same_thread=False,
        isolation_level=None,        # autocommit; we manage transactions explicitly
    )
    conn.row_factory = sqlite3.Row
    return conn


def _get_conn() -> sqlite3.Connection:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.execute("SELECT 1").fetchone()
            return conn
        except sqlite3.ProgrammingError:
            # Connection was closed under us (e.g. after a fork or pragma reset)
            _tls.conn = None
            conn = None
    conn = _connect()
    _tls.conn = conn
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Idempotent CREATE TABLE; race-safe via the module-level setup lock."""
    global _setup_done
    if _setup_done:
        return
    delay = 0.02
    for attempt in range(_LOCKED_MAX_ATTEMPTS):
        try:
            # Hold the in-process lock only for one schema attempt.  A second
            # process may own SQLite's file lock; releasing this mutex while
            # backing off lets other local connections make progress too.
            with _setup_lock:
                if _setup_done:
                    return
                STATE_DIR.mkdir(parents=True, exist_ok=True)
                for pragma in (
                    "PRAGMA journal_mode=WAL",
                    "PRAGMA synchronous=NORMAL",
                    "PRAGMA foreign_keys=ON",
                    """
                    CREATE TABLE IF NOT EXISTS principal_bindings (
                        session_id    TEXT PRIMARY KEY,
                        kind          TEXT NOT NULL CHECK (kind IN ('user', 'service')),
                        principal_id  TEXT NOT NULL,
                        tenant_id     TEXT,
                        namespace_key TEXT NOT NULL,
                        key_version   INTEGER NOT NULL DEFAULT 1,
                        bound_at      TEXT NOT NULL,
                        expires_at    TEXT NOT NULL
                    )
                    """,
                ):
                    conn.execute(pragma)
                columns = {
                    row[1] for row in conn.execute("PRAGMA table_info(principal_bindings)")
                }
                if "key_version" not in columns:
                    try:
                        conn.execute(
                            "ALTER TABLE principal_bindings ADD COLUMN "
                            "key_version INTEGER NOT NULL DEFAULT 1"
                        )
                    except sqlite3.OperationalError as exc:
                        if "duplicate column name" not in str(exc).lower():
                            raise
                conn.execute(
                    "CREATE INDEX IF NOT EXISTS idx_principal_bindings_principal "
                    "ON principal_bindings (principal_id)"
                )
                _setup_done = True
                return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == _LOCKED_MAX_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 0.5)


_LOCKED_MAX_ATTEMPTS = 5


def _execute_with_retry(stmt: str, params: tuple[Any, ...]) -> None:
    """Run a write with retries on ``OperationalError("database is locked")``.

    The two other SQLite OperationalError flavours (syntax error, schema
    corruption) are re-raised immediately — only transient lock contention
    benefits from retry.
    """
    delay = 0.02
    for attempt in range(_LOCKED_MAX_ATTEMPTS):
        try:
            conn = _get_conn()
            conn.execute(stmt, params)
            return
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == _LOCKED_MAX_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 0.5)


def _query_one(stmt: str, params: tuple[Any, ...]) -> Optional[sqlite3.Row]:
    """Read with the same retry-on-lock semantics for symmetry."""
    delay = 0.02
    for attempt in range(_LOCKED_MAX_ATTEMPTS):
        try:
            conn = _get_conn()
            return conn.execute(stmt, params).fetchone()
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == _LOCKED_MAX_ATTEMPTS - 1:
                raise
            time.sleep(delay)
            delay = min(delay * 2.0, 0.5)
    return None  # unreachable; the loop returns/raises


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# Public TTL constant — kept in lock-step with the vclaw session timeout. The
# 24h figure is the documented Phase B default; not enforced here, only
# written so the field is populated and Phase D can sweep it without a schema
# change.
BINDING_TTL_SECONDS: Final[int] = 24 * 60 * 60


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def bind(session_id: str, principal: Principal) -> None:
    """Insert-if-absent binding for ``session_id``; the owner is immutable.

    Three outcomes:

    1. No row for ``session_id`` → insert it.
    2. Row exists with the **same** owner (``kind`` *and* ``principal_id``) →
       idempotent renewal. ``tenant_id`` / ``expires_at`` are refreshed, while
       the stored ``namespace_key`` / ``key_version`` remain immutable so an
       existing session stays attached to its original namespace across key
       rotation. ``bound_at`` keeps the original binding time —
       it is the audit anchor for *when this owner took the session*.
    3. Row exists with a **different** owner → ``PrincipalOwnerConflict``.
       The stored row is never modified.

    Case 3 used to be an ``ON CONFLICT DO UPDATE`` that overwrote
    ``kind``/``principal_id``/``namespace_key``, which let anybody who
    guessed (or replayed) a session id retarget it at their own namespace
    and silently take over the victim's session — the exact opposite of the
    v2 immutable-owner contract.

    The owner check is pushed into the SQL ``ON CONFLICT ... WHERE`` clause so
    it is atomic: SQLite skips the UPDATE when the predicate is false, so two
    concurrent binds with different owners cannot interleave into an
    overwrite. The follow-up ``SELECT`` only decides whether to *report* the
    refusal — by then the row is already guaranteed untouched.

    Both ``session_id`` and ``principal.principal_id`` are validated against
    their sanitiser; a rejected value raises ``ValueError``. ``expires_at``
    is recorded as ``now + BINDING_TTL_SECONDS`` but never enforced here —
    Phase D owns enforcement.
    """
    sanitized_sid = sanitize_session_id(session_id)
    if sanitized_sid is None:
        raise ValueError(f"invalid session_id for bind: {session_id!r}")
    if principal.kind not in ("user", "service"):
        raise ValueError(f"invalid principal kind for bind: {principal.kind!r}")
    # Principal.__post_init__ already validates principal_id; we re-check
    # here so calling bind(Principal(...)) with a programmatically-built
    # Principal (e.g. from a test) that bypassed the dataclass __post_init__
    # still cannot smuggle a bad id.
    if sanitize_principal_id(principal.principal_id) is None:
        raise ValueError(f"invalid principal_id for bind: {principal.principal_id!r}")

    now = _now_iso()
    expires = (datetime.now(timezone.utc) + timedelta(seconds=BINDING_TTL_SECONDS)).isoformat()
    _execute_with_retry(
        "INSERT INTO principal_bindings "
        "(session_id, kind, principal_id, tenant_id, namespace_key, key_version, bound_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(session_id) DO UPDATE SET "
        "tenant_id = excluded.tenant_id, "
        "expires_at = excluded.expires_at "
        # The owner columns are absent from SET *and* guarded here: when the
        # predicate is false SQLite performs no update at all, so a foreign
        # principal can neither retarget the row nor renew its TTL.
        "WHERE principal_bindings.principal_id = excluded.principal_id "
        "AND principal_bindings.kind = excluded.kind",
        (
            sanitized_sid,
            principal.kind,
            principal.principal_id,
            principal.tenant_id,
            principal.namespace_key,
            principal.key_version,
            now,
            expires,
        ),
    )
    # A refused DO UPDATE is silent in SQLite, so read the stored owner back to
    # tell "inserted / renewed" apart from "refused".
    stored = _query_one(
        "SELECT kind, principal_id, tenant_id, namespace_key, key_version FROM principal_bindings "
        "WHERE session_id = ?",
        (sanitized_sid,),
    )
    if stored is not None and (
        stored["principal_id"] != principal.principal_id
        or stored["kind"] != principal.kind
    ):
        # Log the namespace_keys (HMAC digests, safe to persist) and a
        # fingerprint of the session id rather than the raw id, which is
        # bearer-grade material.
        _log.error(
            "SECURITY: refused rebind of an already-owned MCP session "
            "(session_fp=%s owner_kind=%s owner_ns=%s requester_kind=%s "
            "requester_ns=%s); session owner is immutable",
            hashlib.sha256(sanitized_sid.encode("utf-8")).hexdigest()[:16],
            stored["kind"],
            stored["namespace_key"],
            principal.kind,
            principal.namespace_key,
        )
        raise PrincipalOwnerConflict(
            f"session is already bound to a different principal "
            f"(stored kind={stored['kind']!r}, requested kind={principal.kind!r}); "
            "the session owner is immutable"
        )


def lookup(session_id: str) -> Optional[Principal]:
    """Return the ``Principal`` bound to ``session_id``, or ``None``.

    Invalid session ids short-circuit to ``None`` so callers (e.g. handshake
    paths) can do ``if lookup(sid) is None: skip``.
    """
    sanitized_sid = sanitize_session_id(session_id)
    if sanitized_sid is None:
        return None
    row = _query_one(
        "SELECT kind, principal_id, tenant_id, namespace_key, key_version "
        "FROM principal_bindings WHERE session_id = ?",
        (sanitized_sid,),
    )
    if row is None:
        return None
    stored_key = row["namespace_key"]
    try:
        stored_version = int(row["key_version"])
        if stored_version <= 0 or not isinstance(stored_key, str):
            return None
        principal_id = row["principal_id"]
        active_version = current_key_version()
        candidates = []
        if stored_version == active_version:
            candidates.append(_load_secret())
        old_secret = _load_old_secret()
        if old_secret is not None and stored_version == old_key_version():
            candidates.append(old_secret)
        # A version mismatch is intentional fail-closed behavior. It prevents
        # an old key from being accepted for a row that explicitly belongs to
        # another generation.
        valid = any(
            hmac.compare_digest(
                _compute_namespace_key_with_secret(principal_id, secret), stored_key
            )
            for secret in candidates
        )
    except (TypeError, ValueError, UnicodeError):
        valid = False
    if not valid:
        _log.error(
            "SECURITY: stored namespace_key does not match re-derived key for "
            "session=%s; refusing to surface mismatched principal",
            sanitized_sid[:8],
        )
        return None
    try:
        p = Principal(
            kind=row["kind"],
            principal_id=principal_id,
            tenant_id=row["tenant_id"],
        )
    except (TypeError, ValueError):
        return None
    # The row's namespace is authoritative after successful verification. Do
    # not replace it with a key derived from the currently active secret: this
    # is what keeps old sessions and their existing project trees readable
    # during and after a restart-based rotation.
    object.__setattr__(p, "namespace_key", stored_key)
    object.__setattr__(p, "key_version", stored_version)
    return p


def require(session_id: str) -> Principal:
    """Like ``lookup`` but raises ``PrincipalNotFound`` when missing."""
    p = lookup(session_id)
    if p is None:
        raise PrincipalNotFound(
            f"no principal bound for session_id={session_id!r}"
        )
    return p


def unbind(session_id: str) -> None:
    """Delete the binding for ``session_id`` (idempotent).

    Used by tests and by Phase D's expiry sweeper. Missing rows are not an
    error. Invalid session ids raise ``ValueError`` to keep ``unbind``
    symmetric with ``bind``.
    """
    sanitized_sid = sanitize_session_id(session_id)
    if sanitized_sid is None:
        raise ValueError(f"invalid session_id for unbind: {session_id!r}")
    _execute_with_retry(
        "DELETE FROM principal_bindings WHERE session_id = ?",
        (sanitized_sid,),
    )


# ---------------------------------------------------------------------------
# Phase B integration helpers (re-exported by mcp_server.py for tools)
# ---------------------------------------------------------------------------


def get_mcp_session_id_from_scope(scope: dict) -> Optional[str]:
    """Read ``Mcp-Session-Id`` from an ASGI scope, return the sanitized form.

    Returns ``None`` when the header is absent or fails sanitisation — both
    legitimate (handshake before the server issues an id) and hostile values
    collapse to the same ``None`` so callers cannot distinguish "no header"
    from "bad header".
    """
    if not isinstance(scope, dict):
        return None
    headers = scope.get("headers") or []
    for raw_key, raw_value in headers:
        if not isinstance(raw_key, bytes) or not isinstance(raw_value, bytes):
            continue
        if raw_key.lower() != b"mcp-session-id":
            continue
        try:
            decoded = raw_value.decode("ascii")
        except UnicodeDecodeError:
            return None
        return sanitize_session_id(decoded)
    return None


def get_mcp_session_header_from_scope(scope: dict) -> tuple[bool, Optional[str]]:
    """Return ``(present, sanitized value)`` for the session header.

    Unlike ``get_mcp_session_id_from_scope``, this preserves presence so an
    invalid/malformed header cannot be mistaken for the legitimate
    pre-initialize request with no session id.
    """
    if not isinstance(scope, dict):
        return False, None
    for raw_key, raw_value in scope.get("headers") or []:
        if not isinstance(raw_key, bytes) or raw_key.lower() != b"mcp-session-id":
            continue
        if not isinstance(raw_value, bytes):
            return True, None
        try:
            decoded = raw_value.decode("ascii")
        except UnicodeDecodeError:
            return True, None
        return True, sanitize_session_id(decoded)
    return False, None


# ---------------------------------------------------------------------------
# Test seam — point the registry at a different DB file
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Drop the module-level singletons so tests can swap the DB path.

    Closes any cached connection on the current thread (other threads keep
    their own — the registry is per-thread anyway). WAL-sidecar files
    (``-wal``, ``-shm``) live next to the DB and follow the file handle,
    so closing the connection releases them.
    """
    global _setup_done
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _tls.conn = None
    _setup_done = False


def configure(db_path: Path) -> None:
    """Reconfigure the module to use a different database file.

    Intended only for tests — production code should rely on the default
    ``projects/.mcp_sessions/principals.db`` location. Always closes any
    prior connection so the previous DB file is released (matters on
    Windows, where WAL sidecar files keep the handle busy until closed).
    """
    global DB_PATH, STATE_DIR
    _reset_for_tests()
    STATE_DIR = db_path.parent
    DB_PATH = db_path
    STATE_DIR.mkdir(parents=True, exist_ok=True)
