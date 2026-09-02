"""Phase B integration tests for the durable session→principal registry.

These tests live in ``tests/integration`` because they exercise the public
API (``bind``/``lookup``/``require``/``unbind``) end-to-end against a real
SQLite database, mirroring the style of ``test_bearer_user_id.py`` — which
also tests a Phase 3 API against real ASGI plumbing but stops short of
spinning up the FastMCP server. The voicebox probe fixtures are skipped via
``MCP_TEST_SKIP_VOICEBOX_FIXTURES=1``; we don't talk to voicebox here.

Each test gets a fresh SQLite file via the ``registry_db`` autouse fixture
so a regression in upsert semantics can't leak across tests. The fixture
also resets the module's thread-local connection between tests so the
Windows WAL files get released and a test runner that cleans up tmp dirs
doesn't trip on a still-open handle.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import types
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Optional

# Skip voicebox fixtures (this module is unit/integration against SQLite, not
# voicebox REST). Must be set before the conftest fixtures import anything.
os.environ.setdefault("MCP_TEST_SKIP_VOICEBOX_FIXTURES", "1")

import pytest

# Same Windows fcntl shim test_bearer_user_id.py uses; keep in lock-step.
if "fcntl" not in sys.modules:
    sys.modules["fcntl"] = types.ModuleType("fcntl")

import lib.principal_registry as pr  # noqa: E402
from lib.principal_registry import (  # noqa: E402
    Principal,
    PrincipalNotFound,
    PrincipalOwnerConflict,
    compute_namespace_key,
    configure,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_db(tmp_path: Path) -> Path:
    """Point the registry at a fresh ``tmp_path/principals.db`` for one test.

    The fixture also closes any cached connection *before* the tmp dir is
    handed back so SQLite WAL sidecars (``.db-wal``/``.db-shm``) are
    released on Windows. The module's ``configure()`` opens a new
    connection lazily on the next ``_get_conn()`` call.
    """
    db_path = tmp_path / "principals.db"
    # Defensive: a previous test in the same thread may have cached a
    # connection to a different file. Clearing it before ``configure``
    # guarantees the new DB is the one being written to.
    pr._reset_for_tests()
    configure(db_path)
    yield db_path
    # Teardown: close the connection so the tmp_path TempDir cleanup is
    # not blocked on Windows by an open SQLite handle.
    pr._reset_for_tests()


# ---------------------------------------------------------------------------
# 1. bind → lookup round trip
# ---------------------------------------------------------------------------


def test_bind_then_lookup_round_trip(registry_db: Path) -> None:
    """The simplest proof: write a binding, read it back, all fields match."""
    sid = "sess-round-trip-1"
    p = Principal(kind="user", principal_id="alice_42", tenant_id=None)
    pr.bind(sid, p)

    got = pr.lookup(sid)
    assert got is not None
    assert got.kind == "user"
    assert got.principal_id == "alice_42"
    assert got.tenant_id is None
    # namespace_key is a deterministic function of principal_id; the
    # round-trip must produce the same value the producer saw.
    assert got.namespace_key == p.namespace_key
    # Different fields produce equality on the dataclass.
    assert got == p


# ---------------------------------------------------------------------------
# 2. lookup of unknown session → None
# ---------------------------------------------------------------------------


def test_lookup_unknown_session_returns_none(registry_db: Path) -> None:
    """A session id that was never bound must yield None, not raise.

    Tools use ``lookup`` defensively during the handshake window before
    bind has run; this asserts the no-bound-state path doesn't blow up.
    """
    assert pr.lookup("never-bound-session") is None
    # Even after a different session is bound, the unrelated id stays None.
    pr.bind("sess-other", Principal(kind="user", principal_id="bob_99"))
    assert pr.lookup("never-bound-session") is None


def test_lookup_returns_none_for_invalid_session_id(registry_db: Path) -> None:
    """Sanitiser rejects → lookup must collapse to None, not raise.

    A bad session id (CR/LF, oversized, empty) must never produce a
    fragment of the registry; this guards against a typo that would
    otherwise become a SQLite parameter binding error.
    """
    bad_inputs = ["", " ", "has\nnewline", "x" * 300, "\r\ninjected", None]
    for raw in bad_inputs:
        assert pr.lookup(raw) is None, f"unexpected hit for {raw!r}"


# ---------------------------------------------------------------------------
# 3. require unknown → PrincipalNotFound
# ---------------------------------------------------------------------------


def test_require_unknown_raises(registry_db: Path) -> None:
    """``require`` is the strict variant. Tools that *need* the principal
    call this; missing binding is an error not a None."""
    with pytest.raises(PrincipalNotFound):
        pr.require("never-bound-session")

    # Sanity: after binding, require returns the same Principal.
    sid = "sess-strict"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42"))
    p = pr.require(sid)
    assert p.principal_id == "alice_42"


def test_require_invalid_session_id_raises_sanitizer_error(registry_db: Path) -> None:
    """A bad session id short-circuits to ``None`` in ``lookup``, and
    therefore raises ``PrincipalNotFound`` in ``require``.  The
    sanitiser is the single gate — there's no separate validation path.
    """
    with pytest.raises(PrincipalNotFound):
        pr.require("")


# ---------------------------------------------------------------------------
# 4. unbind removes the binding
# ---------------------------------------------------------------------------


def test_unbind_removes_binding(registry_db: Path) -> None:
    sid = "sess-unbind"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42"))
    assert pr.lookup(sid) is not None

    pr.unbind(sid)
    assert pr.lookup(sid) is None
    with pytest.raises(PrincipalNotFound):
        pr.require(sid)

    # Idempotent: a second unbind is a no-op, not an error.
    pr.unbind(sid)
    pr.unbind(sid)


def test_unbind_invalid_session_id_raises(registry_db: Path) -> None:
    """``bind`` rejects invalid ids explicitly; ``unbind`` does the same to
    keep the symmetric contract — an invalid id is a programming error,
    not a silently-swallowed race."""
    with pytest.raises(ValueError):
        pr.unbind("")
    with pytest.raises(ValueError):
        pr.unbind("bad\nid")


# ---------------------------------------------------------------------------
# 5. namespace_key stable across processes
# ---------------------------------------------------------------------------


def test_namespace_key_is_deterministic_across_processes(registry_db: Path) -> None:
    """The whole point of HMAC(secret, principal_id) is that two processes
    with the same secret always agree on the key. Spawn a fresh Python
    subprocess (no shared state, no module cache) and verify it computes
    the same 32-hex value we do for the same principal id.
    """
    principal_id = "alice_crossproc"
    in_process = compute_namespace_key(principal_id)

    script = (
        "from lib.principal_registry import compute_namespace_key;"
        "import sys, types;"
        "sys.modules.setdefault('fcntl', types.ModuleType('fcntl'));"
        f"print(compute_namespace_key({principal_id!r}))"
    )
    # Use sys.executable so the subprocess is the same interpreter.
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=str(Path(__file__).resolve().parent.parent.parent),
    )
    sub_process = result.stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{32}", sub_process), f"subprocess output malformed: {result.stdout!r}"
    assert sub_process == in_process, (
        f"HMAC output diverged between processes: in={in_process} sub={sub_process}"
    )


# ---------------------------------------------------------------------------
# 6. different principals → different keys
# ---------------------------------------------------------------------------


def test_namespace_key_differs_for_different_principals(registry_db: Path) -> None:
    """HMAC is injective-enough-for-our-purpose at 128 bits: any two
    distinct principal_ids must produce different keys. Collisions here
    would collapse two namespaces onto each other."""
    keys = {compute_namespace_key(f"user_{i}_abc") for i in range(16)}
    assert len(keys) == 16, "HMAC(secret, pid) is not collision-free at 128 bits"
    # A separate principal with the same length still differs (sanity).
    assert compute_namespace_key("alice_1") != compute_namespace_key("alice_2")


# ---------------------------------------------------------------------------
# 7. namespace_key is exactly 32 hex chars
# ---------------------------------------------------------------------------


def test_namespace_key_uses_32_hex_chars(registry_db: Path) -> None:
    """The spec pins the key at 16 bytes = 32 lowercase hex characters.
    Both the dataclass-computed field and the standalone helper must
    obey this shape."""
    p = Principal(kind="user", principal_id="shape_test")
    assert len(p.namespace_key) == 32
    assert re.fullmatch(r"[0-9a-f]{32}", p.namespace_key), p.namespace_key
    assert compute_namespace_key("shape_test") == p.namespace_key


# ---------------------------------------------------------------------------
# 8. service principal supported (Phase B contract covers both kinds)
# ---------------------------------------------------------------------------


def test_service_principal_kind_is_persisted(registry_db: Path) -> None:
    """``Principal(kind=...)`` accepts both ``user`` and ``service`` per the
    v2 spec. The ``kind`` flows through ``bind`` → row → ``lookup``
    unchanged so Phase C's workspace code can branch on it.
    """
    sid = "sess-service"
    pr.bind(sid, Principal(kind="service", principal_id="svc-build-runner-1"))
    got = pr.lookup(sid)
    assert got is not None
    assert got.kind == "service"
    assert got.principal_id == "svc-build-runner-1"
    # Same secret/pid → same key (kind does not contribute to the key).
    assert got.namespace_key == compute_namespace_key("svc-build-runner-1")


# ---------------------------------------------------------------------------
# 9. invalid session id rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, bad_sid",
    [
        ("empty", ""),
        ("whitespace_only", "   "),
        ("tab_only", "\t"),
        ("crlf_injection", "abc\r\ndef"),
        ("lf_only", "abc\ndef"),
        ("overlong_257", "a" * 257),
        ("too_long_500", "x" * 500),
        ("all_caps_overlong", "M" * 300),
    ],
)
def test_bind_rejects_invalid_session_id(
    registry_db: Path, label: str, bad_sid: str
) -> None:
    """``bind`` rejects bad session ids with ``ValueError``. The contract
    is "raise, don't silently drop" — a silent drop would let a hostile
    header create an undetected binding into the wrong namespace."""
    p = Principal(kind="user", principal_id="alice_42")
    with pytest.raises(ValueError):
        pr.bind(bad_sid, p)


def test_bind_accepts_session_id_at_length_boundary(registry_db: Path) -> None:
    """A 256-char session id is the documented max and must be accepted."""
    sid = "s" * 256
    pr.bind(sid, Principal(kind="user", principal_id="alice_42"))
    assert pr.lookup(sid) is not None


# ---------------------------------------------------------------------------
# 10. invalid principal_id rejected
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "label, bad_pid",
    [
        ("crlf_injection", "abc\r\ndef"),
        ("path_traversal_dotdot", "../../etc/passwd"),
        ("path_traversal_slash", "alice/bob"),
        ("backslash", "alice\\bob"),
        ("space", "alice bob"),
        ("too_long_129", "a" * 129),
        ("too_long_500", "u" * 500),
        ("unicode_full_width", "ａlice"),
        ("empty_after_strip", "   "),
    ],
)
def test_bind_rejects_invalid_principal_id(
    registry_db: Path, label: str, bad_pid: str
) -> None:
    """``bind`` refuses to persist a bad principal id. Two paths:

    1. ``Principal(...)`` rejects the bad id in ``__post_init__`` — most
       callers see this and never produce a Principal in the first place.
    2. ``bind()`` re-validates as defense in depth: a hand-built Principal
       that bypassed ``__post_init__`` still cannot be written.
    """
    # Path 1 — the normal one. The Principal dataclass refuses.
    with pytest.raises(ValueError):
        Principal(kind="user", principal_id=bad_pid)

    # Path 2 — bypass the dataclass guard to confirm ``bind`` is the second
    # gate. ``frozen=True`` blocks ``p.attr = ...`` but ``object.__setattr__``
    # sneaks past it; we need this escape because a real attacker who
    # already controls the Python process would have other options, so the
    # defense is about honest-bug protection, not hostile-process protection.
    bypassed = Principal.__new__(Principal)  # type: ignore[call-arg]
    object.__setattr__(bypassed, "kind", "user")
    object.__setattr__(bypassed, "principal_id", bad_pid)
    object.__setattr__(bypassed, "tenant_id", None)
    object.__setattr__(bypassed, "namespace_key", "0" * 32)
    with pytest.raises(ValueError):
        pr.bind("valid-session", bypassed)


def test_path_separator_chars_are_rejected_at_principal_layer(
    registry_db: Path,
) -> None:
    """Dot segments and separators cannot reach legacy fallback paths."""
    for bad in (".", "..", "alice/bob", "alice\\bob"):
        with pytest.raises(ValueError):
            Principal(kind="user", principal_id=bad)

    # Spaces stay rejected (whitespace was never in charset).
    with pytest.raises(ValueError):
        Principal(kind="user", principal_id="alice bob")


# ---------------------------------------------------------------------------
# 11. tenant_id preserved
# ---------------------------------------------------------------------------


def test_tenant_id_preserved_through_lookup(registry_db: Path) -> None:
    """Phase B accepts ``tenant_id=str`` even though no header feeds it
    yet. Confirm it survives bind/lookup so future tenant-aware code
    can rely on the column."""
    sid = "sess-with-tenant"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42", tenant_id="tenant-7"))
    got = pr.lookup(sid)
    assert got is not None
    assert got.tenant_id == "tenant-7"


def test_tenant_id_can_be_none(registry_db: Path) -> None:
    sid = "sess-no-tenant"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42", tenant_id=None))
    got = pr.lookup(sid)
    assert got is not None
    assert got.tenant_id is None


# ---------------------------------------------------------------------------
# 12. concurrent bind → no race
# ---------------------------------------------------------------------------


def test_concurrent_bind_no_race(registry_db: Path) -> None:
    """50 threads each bind a unique session. After the dust settles we
    must be able to lookup every one of them and find the matching
    principal. Catches a missing journal_mode=WAL (writers would block
    each other or error out) or a forgotten index."""
    n = 50
    sids = [f"sess-conc-{i:03d}-{uuid.uuid4().hex[:8]}" for i in range(n)]

    def worker(sid: str) -> str:
        p = Principal(kind="user", principal_id=f"user_{sid}")
        pr.bind(sid, p)
        return sid

    with ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(worker, s) for s in sids]
        # surface any bind-time exception
        for fut in as_completed(futures):
            fut.result()

    for sid in sids:
        got = pr.lookup(sid)
        assert got is not None, f"missing binding for {sid}"
        assert got.principal_id == f"user_{sid}"
        # Same key the producer saw — round-trip is exact under WAL.
        assert got.namespace_key == compute_namespace_key(f"user_{sid}")


# ---------------------------------------------------------------------------
# 13. TTL is recorded but not enforced
# ---------------------------------------------------------------------------


def test_ttl_not_enforced_yet(registry_db: Path) -> None:
    """Phase B writes ``expires_at`` so Phase D can sweep it without a
    schema change, but no enforcement happens here. Verify the column is
    populated with a forward date AND a row whose ``expires_at`` is in
    the past is still served by lookup.
    """
    sid_fresh = "sess-fresh"
    pr.bind(sid_fresh, Principal(kind="user", principal_id="alice_42"))
    row = pr._query_one(
        "SELECT bound_at, expires_at FROM principal_bindings WHERE session_id = ?",
        (sid_fresh,),
    )
    assert row is not None
    assert row["bound_at"] and row["expires_at"]
    # TTL is one day (86400s) — the future timestamp is at least 23h59m
    # ahead, the past is at most 1s in the future of binding.
    from datetime import datetime, timezone
    bound = datetime.fromisoformat(row["bound_at"])
    expires = datetime.fromisoformat(row["expires_at"])
    delta = (expires - bound).total_seconds()
    assert 86399 <= delta <= 86401, f"unexpected TTL span: {delta}s"

    # Now write a row whose expires_at is in the past and confirm it is
    # still served. Phase D can introduce ``WHERE expires_at > now``; until
    # then we expect ``lookup`` to be permissive.
    conn = pr._connect()
    cur = conn.execute(
        "INSERT INTO principal_bindings "
        "(session_id, kind, principal_id, tenant_id, namespace_key, bound_at, expires_at) "
        "VALUES (?, 'user', 'bob_99', NULL, ?, '2000-01-01T00:00:00+00:00', '2000-01-02T00:00:00+00:00')",
        ("sess-expired", compute_namespace_key("bob_99")),
    )
    conn.commit()
    got = pr.lookup("sess-expired")
    assert got is not None, "expired binding must still resolve in Phase B"
    assert got.principal_id == "bob_99"


# ---------------------------------------------------------------------------
# 14. default secret → warning log (one-time)
# ---------------------------------------------------------------------------


def test_default_secret_emits_warning(registry_db: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A missing ``OPENMONTAGE_PRINCIPAL_HASH_SECRET`` must fire a
    WARNING so production deployments notice. Reset the cached secret
    so the test re-exercises the fallback branch even after a previous
    test triggered it.
    """
    # Reset the module-level cache so we re-enter _load_secret()
    # (otherwise a stale ``None`` from an earlier "this is the secret"
    # code path could mask the warning).
    pr._secret_value = None
    pr._secret_warned = False

    # Guarantee the env var is unset for this test. ``monkeypatch`` would
    # be cleaner, but pytest's ``caplog`` interacts cleanly with manual
    # save/restore.
    saved = os.environ.pop("OPENMONTAGE_PRINCIPAL_HASH_SECRET", None)
    try:
        with caplog.at_level("WARNING", logger="lib.principal_registry"):
            secret = pr._load_secret()
        assert secret == pr._DEFAULT_HASH_SECRET.encode("utf-8")
        assert any(
            "OPENMONTAGE_PRINCIPAL_HASH_SECRET" in rec.message
            for rec in caplog.records
        ), f"expected warning; got {[r.message for r in caplog.records]}"
    finally:
        if saved is not None:
            os.environ["OPENMONTAGE_PRINCIPAL_HASH_SECRET"] = saved
        # Reset again so subsequent tests don't see a primed cache.
        pr._secret_value = None
        pr._secret_warned = False


# ---------------------------------------------------------------------------
# 15. bind is insert-if-absent — the session owner is immutable
# ---------------------------------------------------------------------------


def test_bind_rejects_rebind_to_a_different_principal(registry_db: Path) -> None:
    """A different owner must be refused, and the stored row untouched.

    ``bind`` used to be a blind upsert that overwrote
    ``kind``/``principal_id``/``namespace_key``, so anyone who guessed or
    replayed a session id could retarget it at their own namespace and take
    the victim's session over. The v2 contract makes the owner immutable:
    ``alice_42`` -> ``alice_42_v2`` is a *different* principal_id and must
    raise, not "re-authenticate".
    """
    sid = "sess-rebind"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42"))
    assert pr.lookup(sid).principal_id == "alice_42"

    with pytest.raises(PrincipalOwnerConflict):
        pr.bind(sid, Principal(kind="user", principal_id="alice_42_v2"))

    # Never overwrite: the original owner and its namespace_key survive.
    got = pr.lookup(sid)
    assert got.principal_id == "alice_42"
    assert got.namespace_key == compute_namespace_key("alice_42")


def test_bind_rejects_rebind_that_only_changes_kind(registry_db: Path) -> None:
    """``kind`` is part of the owner identity, not a mutable attribute.

    ``ProjectWorkspace`` buckets on ``kind`` (``projects/users/`` vs
    ``projects/services/``), so flipping it would silently move the session
    into a different sub-tree even though the principal_id matched.
    """
    sid = "sess-kind-flip"
    pr.bind(sid, Principal(kind="user", principal_id="shared_id_1"))
    with pytest.raises(PrincipalOwnerConflict):
        pr.bind(sid, Principal(kind="service", principal_id="shared_id_1"))
    assert pr.lookup(sid).kind == "user"


def test_bind_same_owner_is_idempotent_renewal(registry_db: Path) -> None:
    """Re-binding the *same* owner is a renewal, not an error.

    vclaw re-sends ``X-VClaw-User-Id`` on every request of a session, so
    ``bind`` runs many times per session and must stay idempotent. The
    renewal may refresh ``tenant_id`` / ``namespace_key`` / ``expires_at``;
    ``bound_at`` stays pinned to the first bind because it is the audit
    anchor for when this owner took the session.
    """
    sid = "sess-renew"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42"))
    first = pr._query_one(
        "SELECT bound_at, expires_at, tenant_id FROM principal_bindings "
        "WHERE session_id = ?",
        (sid,),
    )
    assert first is not None
    assert first["tenant_id"] is None

    # Sleep a hair so a refreshed expires_at is distinguishable from the
    # first one (ISO strings carry microseconds, but be explicit).
    time.sleep(0.01)
    # Same owner, now carrying a tenant_id — must not raise.
    pr.bind(sid, Principal(kind="user", principal_id="alice_42", tenant_id="tenant-7"))

    got = pr.lookup(sid)
    assert got is not None
    assert got.principal_id == "alice_42"
    assert got.kind == "user"
    # Renewable field picked up.
    assert got.tenant_id == "tenant-7"

    second = pr._query_one(
        "SELECT bound_at, expires_at FROM principal_bindings WHERE session_id = ?",
        (sid,),
    )
    assert second is not None
    # bound_at is immutable; expires_at was renewed.
    assert second["bound_at"] == first["bound_at"]
    assert second["expires_at"] > first["expires_at"]

    # And it stays idempotent over repeated calls.
    for _ in range(3):
        pr.bind(sid, Principal(kind="user", principal_id="alice_42", tenant_id="tenant-7"))
    assert pr.lookup(sid).principal_id == "alice_42"


def test_bind_after_unbind_allows_a_new_owner(registry_db: Path) -> None:
    """Immutability is scoped to a *live* binding.

    Once the row is gone (``unbind``, or Phase D's expiry sweeper), the
    session id is free again — otherwise a recycled session id would be
    permanently unusable.
    """
    sid = "sess-recycle"
    pr.bind(sid, Principal(kind="user", principal_id="alice_42"))
    pr.unbind(sid)
    pr.bind(sid, Principal(kind="user", principal_id="bob_99"))
    assert pr.lookup(sid).principal_id == "bob_99"


def test_concurrent_bind_same_session_different_owners_has_one_winner(
    registry_db: Path,
) -> None:
    """The owner check is atomic — a race cannot produce an overwrite.

    16 threads bind the same session id with 16 distinct principals. Exactly
    one must win; every loser must raise ``PrincipalOwnerConflict``, and the
    stored owner must be one of the contenders (never a torn mix of two).
    """
    sid = "sess-race"
    principals = [f"racer_{i:02d}" for i in range(16)]

    def worker(pid: str) -> Optional[str]:
        try:
            pr.bind(sid, Principal(kind="user", principal_id=pid))
            return pid
        except PrincipalOwnerConflict:
            return None

    with ThreadPoolExecutor(max_workers=len(principals)) as ex:
        results = [f.result() for f in [ex.submit(worker, p) for p in principals]]

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly one winner, got {winners}"
    stored = pr.lookup(sid)
    assert stored is not None
    assert stored.principal_id == winners[0]
    assert stored.namespace_key == compute_namespace_key(winners[0])


# ---------------------------------------------------------------------------
# 16. Existing-schema migration and restart-safe key rotation
# ---------------------------------------------------------------------------


def test_existing_schema_is_upgraded_idempotently(registry_db: Path) -> None:
    """A database created before ``key_version`` must remain writable."""
    pr._reset_for_tests()
    conn = sqlite3.connect(registry_db)
    conn.execute(
        "CREATE TABLE principal_bindings ("
        "session_id TEXT PRIMARY KEY, kind TEXT NOT NULL, principal_id TEXT NOT NULL, "
        "tenant_id TEXT, namespace_key TEXT NOT NULL, bound_at TEXT NOT NULL, "
        "expires_at TEXT NOT NULL)"
    )
    old_key = compute_namespace_key("legacy_user")
    conn.execute(
        "INSERT INTO principal_bindings VALUES (?, 'user', ?, NULL, ?, ?, ?)",
        ("legacy-session", "legacy_user", old_key,
         "2000-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # The first bind must trigger ALTER TABLE before it references the column.
    configure(registry_db)
    pr.bind("new-session", Principal(kind="user", principal_id="new_user"))
    columns = {
        row[1] for row in pr._connect().execute("PRAGMA table_info(principal_bindings)")
    }
    assert "key_version" in columns
    assert pr.lookup("legacy-session").principal_id == "legacy_user"

    # Re-opening the same file must not attempt a non-idempotent migration.
    pr._reset_for_tests()
    configure(registry_db)
    pr.bind("new-session-2", Principal(kind="user", principal_id="new_user_2"))
    assert pr.lookup("new-session-2").principal_id == "new_user_2"


def test_rotation_keeps_old_namespace_and_accepts_new_rows(
    registry_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart with current+old secrets resolves both generations."""
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "rotation-old")
    monkeypatch.delenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", raising=False)
    monkeypatch.delenv("OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION", raising=False)
    pr._secret_value = None
    old = Principal(kind="user", principal_id="rotating_user")
    pr.bind("old-session", old)

    # Simulate a process restart with a new active key while retaining the old
    # material explicitly for rows that have not yet been migrated.
    pr._reset_for_tests()
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "rotation-new")
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", "rotation-old")
    pr._secret_value = None
    got_old = pr.lookup("old-session")
    assert got_old is not None
    assert got_old.namespace_key == old.namespace_key
    assert got_old.key_version == 1

    new = Principal(kind="user", principal_id="new_user_after_rotation")
    pr.bind("new-session", new)
    got_new = pr.lookup("new-session")
    assert got_new is not None
    assert got_new.namespace_key == new.namespace_key
    assert got_new.namespace_key != old.namespace_key


def test_rotation_without_old_secret_fails_closed(
    registry_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "before-rotation")
    monkeypatch.delenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", raising=False)
    pr._secret_value = None
    old = Principal(kind="user", principal_id="unrecoverable_user")
    pr.bind("old-session", old)

    pr._reset_for_tests()
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "after-rotation")
    pr._secret_value = None
    assert pr.lookup("old-session") is None


def test_versioned_rotation_uses_explicit_old_key_id(
    registry_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "v1-material")
    monkeypatch.delenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", raising=False)
    monkeypatch.delenv("OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION", raising=False)
    pr._secret_value = None
    old = Principal(kind="user", principal_id="versioned_user")
    pr.bind("versioned-old-session", old)

    pr._reset_for_tests()
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET", "v2-material")
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_SECRET_OLD", "v1-material")
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION", "2")
    monkeypatch.setenv("OPENMONTAGE_PRINCIPAL_HASH_KEY_VERSION_OLD", "1")
    pr._secret_value = None
    got_old = pr.lookup("versioned-old-session")
    assert got_old is not None
    assert got_old.namespace_key == old.namespace_key
    assert got_old.key_version == 1

    new = Principal(kind="user", principal_id="versioned_new_user")
    assert new.key_version == 2
    assert new.namespace_key == compute_namespace_key("versioned_new_user")


def test_principal_rejects_unknown_kind_and_padding(registry_db: Path) -> None:
    with pytest.raises(ValueError):
        Principal(kind="admin", principal_id="alice")
    with pytest.raises(ValueError):
        Principal(kind="user", principal_id=" alice")
    with pytest.raises(ValueError):
        Principal(kind="user", principal_id=".")
    with pytest.raises(ValueError):
        Principal(kind="user", principal_id="..")
