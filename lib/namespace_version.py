"""Namespace-version feature flag for the legacy → v2 migration window.

Why this exists
---------------

Phase C finalised ``projects/users/<namespace_key>/<project_id>/...`` as the
canonical per-principal layout (``namespace_key`` = HMAC_SHA256(secret,
principal_id)[:16].hex, 32 hex chars). Real deployments have years of
pre-Phase-C data on disk under ``projects/users/<raw_openid>/...`` — the v1
``tools/external/claude_video.py`` and the web/BFF side still emit raw openid
strings into ``users/<openid>/...``.

Phase D does two operations on this layout:

1. **Migrate** the legacy ``<raw_openid>`` directories to the HMAC
   ``<namespace_key>`` form via ``scripts/migrate_users_to_namespace_key.py``.
2. **Enforce** the new layout — refuse new writes that would land in v1
   space — once the migration is trusted.

The enforcement has to be gradual. A hard switch from "accept anything" to
"reject anything that isn't v2" breaks every deployment that hasn't finished
its migration. This module is the graduated gate:

* ``legacy`` (off, default unless ``OPENMONTAGE_NAMESPACE_VERSION`` is set):
  new writes always target v2; reads consult v2 first and then v1. This is
  the only mode during the rollout window.

* ``canary``: the deterministic bucket remains available for rollout
  telemetry, while the filesystem contract is safe for every pair: writes
  target v2 and reads fall back to v1 during migration.

* ``v2-only``: every ``ProjectWorkspace`` lands under the HMAC layout; an
  attempted legacy path (caller passing a ``<raw_openid>`` instead of a
  ``<namespace_key>``) raises ``NamespaceVersionError``. This is the
  end-state of Phase D — once it's on, the migration script becomes
  mandatory for any environment that still has v1 directories.

Contract
--------

1. ``current_namespace_version()`` reads the env var once per call and
   returns the parsed ``NamespaceVersion`` enum value. There is no module-
   level cache — tests frequently want to swap it without re-importing the
   module. The cache, if any, must live one layer up.

2. ``canary_bucket(principal_id, project_id)`` returns ``True`` for the ~10%
   of pairs that fall into the v2 bucket. The bucket is *stable across
   restarts* (uses ``hashlib.blake2b`` with a fixed digest size, not Python's
   ``hash()``, which is salted per process and would flip a project between
   v1 and v2 on every restart).

3. ``resolve_workspace_root(...)`` returns the *list* of candidate workspace
   roots the caller should consult in order. ``legacy`` and ``canary`` return
   ``[v2_root, v1_root]``; ``v2-only`` returns ``[v2_root]``. The ordering
   matters: it is the "preferred write target" first and "fallback read
   target" second.

4. Unknown env var values fall back to ``legacy`` with a single WARNING,
   mirroring ``lib/principal_registry._load_secret``'s pattern. A typo'd
   ``OPENMONTAGE_NAMESPACE_VERSION=v2only`` (no dash) must not silently
   behave like ``v2-only`` — a typo is more dangerous than an overly-
   permissive mode.
"""
from __future__ import annotations

import enum
import hashlib
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Final, List, Optional

_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enum + env var
# ---------------------------------------------------------------------------

_ENV_NAMESPACE_VERSION: "Final[str]" = "OPENMONTAGE_NAMESPACE_VERSION"


class NamespaceVersion(str, enum.Enum):
    """The three modes the feature flag supports.

    Inheriting from ``str`` makes accidental JSON serialisation round-trip
    cleanly (``json.dumps(NamespaceVersion.V2_ONLY) == '"v2-only"'``) so a
    future audit log can embed the mode without an extra conversion step.

    The string values are the canonical env var spellings — keep them in
    lock-step with ``_ENV_NAMESPACE_VERSION`` docs.
    """

    LEGACY = "legacy"
    V2_ONLY = "v2-only"
    CANARY = "canary"


# Default mode when the env var is unset or misspelled. ``legacy`` is the
# only safe default during the rollout: a fresh install with no v1 data
# still runs cleanly because ``legacy`` prefers v2 for new writes anyway.
DEFAULT_NAMESPACE_VERSION: "Final[NamespaceVersion]" = NamespaceVersion.LEGACY


# Canary: 1-in-N ratio. 10% = 10. Stable across the module.
_CANARY_BUCKET_MODULUS: "Final[int]" = 10


class NamespaceVersionError(ValueError):
    """Raised when an operation is incompatible with the current mode.

    A subclass of ``ValueError`` so existing code paths that catch the
    validator's generic ``ValueError`` keep working; the distinct type
    exists so a Phase D caller (or audit log) can tell the policy
    enforcement apart from a programmer-level bad-input error.

    Distinct from ``lib.project_workspace.WorkspaceErrorError`` — the
    *project workspace* error is about path-containment violations
    (resolved path escapes ``self.root``); the *namespace version* error
    is about layout-version policy (v2-only mode refuses to write v1).
    """


# ---------------------------------------------------------------------------
# Env-var parsing
# ---------------------------------------------------------------------------


def _coerce(raw: Optional[str]) -> NamespaceVersion:
    """Parse a raw env-var value into a ``NamespaceVersion``.

    Empty / None / unknown values fall back to ``DEFAULT_NAMESPACE_VERSION``
    (``legacy``). The fallback emits a one-shot WARNING per process so
    production deployments don't silently land on the default because of a
    typo.
    """
    if raw is None:
        return DEFAULT_NAMESPACE_VERSION
    candidate = raw.strip()
    if not candidate:
        return DEFAULT_NAMESPACE_VERSION
    # ``NamespaceVersion(value)`` raises ``ValueError`` for unknown strings
    # — we treat that exactly like a typo and fall back.
    try:
        return NamespaceVersion(candidate)
    except ValueError:
        if not getattr(_coerce, "_warned", False):
            _log.warning(
                "OPENMONTAGE_NAMESPACE_VERSION=%r is not one of "
                "['legacy', 'v2-only', 'canary']; falling back to %r. "
                "Fix the env var to silence this warning.",
                raw,
                DEFAULT_NAMESPACE_VERSION.value,
            )
            _coerce._warned = True  # type: ignore[attr-defined]
        return DEFAULT_NAMESPACE_VERSION


def current_namespace_version() -> NamespaceVersion:
    """Return the active namespace version, read from the env on every call.

    No module-level cache: tests frequently monkeypatch
    ``OPENMONTAGE_NAMESPACE_VERSION`` mid-test, and any caching layer
    above this function would mask the patch. The cost of re-reading
    ``os.environ`` is negligible.
    """
    return _coerce(os.environ.get(_ENV_NAMESPACE_VERSION))


# ---------------------------------------------------------------------------
# Canary bucket — stable across processes
# ---------------------------------------------------------------------------


def canary_bucket(principal_id: str, project_id: str) -> bool:
    """Return ``True`` when ``(principal_id, project_id)`` falls into the v2 bucket.

    Uses BLAKE2b (128-bit digest) so the result is stable across processes
    and Python versions — Python's built-in ``hash()`` salts its output
    per process, which would move a project between buckets on every
    restart. The digest is hex-decoded into an integer and taken modulo
    ``_CANARY_BUCKET_MODULUS``; 0 means "this pair is in the v2 bucket".

    The combined key is ``f"{principal_id}::{project_id}"`` with a
    non-empty separator so ``("a", "bc")`` and ``("ab", "c")`` hash to
    distinct buckets (collapsing them would silently bias canary
    selection toward ``principal_id``).

    Empty / None inputs short-circuit to ``False`` (legacy bucket) because
    we never want a sanity-failure to land a request in the v2 bucket by
    accident.
    """
    if not isinstance(principal_id, str) or not isinstance(project_id, str):
        return False
    if not principal_id or not project_id:
        return False
    combined = f"{principal_id}::{project_id}".encode("utf-8")
    digest = hashlib.blake2b(combined, digest_size=16).digest()
    # 128-bit unsigned integer from the digest bytes.
    bucket = int.from_bytes(digest, byteorder="big", signed=False) % _CANARY_BUCKET_MODULUS
    return bucket == 0


# ---------------------------------------------------------------------------
# Layout computation — the one-stop helper a caller uses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class NamespaceLayout:
    """The candidate workspace roots a caller should consult in priority order.

    ``v2_roots`` is the canonical v2 layout (HMAC namespace_key). ``v1_roots``
    is the legacy layout (raw ``principal_id``). The ``mode`` field records
    which ``NamespaceVersion`` produced this layout so callers can include
    it in audit logs without re-reading the env var.

    ``preferred`` is the first root in the candidate list — the one new
    writes should target. ``fallback`` is the second; reads may consult it
    if the preferred root is empty. Both are convenience accessors; the
    authoritative list is ``candidates``.
    """

    mode: NamespaceVersion
    v2_root: Path
    v1_root: Path
    candidates: tuple[Path, ...]

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """Roots allowed for reads, in v2-first priority order.

    ``candidates`` is deliberately also the read order. Keeping this
        as a named property prevents callers from accidentally using the
        preferred write root as the only read root during the migration
        window.
        """
        return self.candidates

    @property
    def preferred(self) -> Path:
        return self.candidates[0]

    @property
    def fallback(self) -> Optional[Path]:
        return self.candidates[1] if len(self.candidates) > 1 else None

    def existing_root(self) -> Optional[Path]:
        """Return the first candidate that exists on disk, or ``None``.

        Useful for read paths that want to consult the layout in priority
        order without writing anywhere. Both candidate roots are checked
        in order; a non-empty v2 layout takes precedence even in ``legacy``
        mode because it's the post-migration target.
        """
        for candidate in self.candidates:
            if candidate.is_dir():
                return candidate
        return None


def resolve_workspace_layout(
    *,
    principal_id: str,
    project_id: str,
    projects_root: Path,
    kind_dir: str = "users",
    namespace_key: Optional[str] = None,
    version: Optional[NamespaceVersion] = None,
) -> NamespaceLayout:
    """Compute the candidate workspace roots for a principal + project pair.

    Parameters
    ----------
    principal_id:
        The authenticated ``Principal.principal_id`` (raw openid or service id).
    project_id:
        The sanitised project id (already passed ``sanitize_project_id``).
    projects_root:
        The ``PROJECTS_DIR`` (i.e. ``projects/``). The caller is
        responsible for passing the right root; this function does NOT
        touch ``lib.paths.PROJECTS_DIR`` so it stays unit-testable without
        a workspace fixture.
    kind_dir:
        The bucket under ``projects_root`` for the principal's kind —
        typically ``"users"`` or ``"services"``. Defaults to ``"users"``;
        ``ProjectWorkspace.for_principal`` overrides it for service
        principals.
    namespace_key:
        The pre-computed ``namespace_key`` (32 hex chars). When omitted,
        the function falls back to ``principal_id`` for v1 layout and
        expects the caller to pass an HMAC-derived key for v2. The
        production caller is always ``ProjectWorkspace`` which has the
        key in hand, so the omission path is for tests that want to
        inspect layout decisions in isolation.
    version:
        Override the active ``NamespaceVersion`` (defaults to reading
        the env var via ``current_namespace_version()``). Useful for
        tests that want to force a mode without monkeypatching
        ``os.environ``.

    Returns
    -------
    NamespaceLayout
        The dataclass containing both candidate roots, the mode, and the
        candidates tuple in priority order.

    Notes
    -----
    The function is **pure**: it never touches the filesystem and never
    raises on missing directories. Whether to write to the preferred or
    the fallback is a project-specific policy decision; this function
    only encodes the mode-driven ordering.
    """
    if version is None:
        version = current_namespace_version()

    v2_key = namespace_key or ""
    # NOTE: ``kind_dir`` is supplied by the caller (ProjectWorkspace reads
    # ``principal.kind`` and picks `"users"` or `"services"`). This avoids
    # the duplicated-prefix bug where ``projects/users`` was passed twice
    # (once as ``projects_dir`` and again as the literal bucket).
    v1_root = projects_root / kind_dir / principal_id / project_id
    v2_root = projects_root / kind_dir / v2_key / project_id

    # Compare by value, rather than enum identity.  A few long-lived
    # processes (and pytest) reload this module while other modules still
    # hold an enum member from the previous module incarnation.
    version_value = getattr(version, "value", version)
    if version_value == NamespaceVersion.V2_ONLY.value:
        # No fallback — refuse to even acknowledge a v1 layout.
        candidates: tuple[Path, ...] = (v2_root,)
    elif version_value == NamespaceVersion.CANARY.value:
        # Canary controls observability/rollout policy, not the write
        # destination: all new writes are v2.  Legacy is a read fallback
        # for the non-v2-only window, including canary's v1 bucket.
        candidates = (v2_root, v1_root)
    else:  # legacy
        # Always try v2 first; fall back to v1. This means even on a
        # pre-migration deployment, *new* writes land under the v2
        # layout as soon as the caller passes a namespace_key. The
        # order is the rationale: writes prefer the new layout so the
        # migration script can be incremental (new writes start in v2;
        # migration only touches the leftover v1 directories).
        candidates = (v2_root, v1_root)

    return NamespaceLayout(
        mode=version,
        v2_root=v2_root,
        v1_root=v1_root,
        candidates=candidates,
    )


__all__ = [
    "NamespaceVersion",
    "NamespaceVersionError",
    "NamespaceLayout",
    "DEFAULT_NAMESPACE_VERSION",
    "current_namespace_version",
    "canary_bucket",
    "resolve_workspace_layout",
]
