"""Per-principal workspace abstraction (Phase C of user isolation).

Why this exists
---------------

Phase B (``lib.principal_registry``) introduced a durable
``session_id → Principal`` mapping so FastMCP tools running in a
per-session background task can look up the authenticated principal. The
filesystem layout, however, was still wired straight to
``projects/<project_id>/...`` — every reader and writer used the raw id and
collided with any other user using the same id. The single principal that
lands two assets with the same project_id overwrites the other user's
assets without the system noticing.

Phase C introduces ``ProjectWorkspace``: a frozen dataclass that holds a
``Principal`` plus a project_id and exposes the canonical per-principal
sub-paths (assets / artifacts / renders / checkpoints / upload_state).
Every writer and reader now computes its path from a workspace object so
the per-principal namespace is uniform across the codebase:

    projects/users/<namespace_key>/<project_id>/assets/
    projects/users/<namespace_key>/<project_id>/artifacts/
    projects/users/<namespace_key>/<project_id>/renders/
    projects/users/<namespace_key>/<project_id>/checkpoints/
    projects/users/<namespace_key>/.uploads/<upload_id>.json
    projects/services/<namespace_key>/<project_id>/...

``namespace_key`` is *not* recomputed here — the registry's ``Principal``
already carries the authoritative value (HMAC of the secret and the
principal_id), and a hand-rolled re-computation could disagree after a
secret rotation. The workspace reads it and only it.

Contract
--------

1. ``project_id`` is sanitised via ``sanitize_project_id`` (allow-list
   ``[a-zA-Z0-9._-]{1,64}``); a rejected id raises ``ValueError`` because
   a bad project id is a tool-contract error, not a soft header failure.
2. ``for_current_principal(project_id)`` calls ``current_principal()`` and
   lets any ``PrincipalNotFound`` propagate untouched — callers can rely
   on the registry's exception type for the 401/403 decision. We do NOT
   swallow it: a missing principal means "no principal = no workspace".
3. ``resolve(relative)`` is the only sanctioned way to land a
   user-controlled path component onto a workspace path. It calls
   ``Path.resolve()`` so symlinks and ``..`` components are evaluated
   against the on-disk tree, then refuses anything that escapes
   ``self.root``. This is the single security boundary below the
   tool-level input validation.
4. ``upload_state`` lives next to the per-user project tree (under
   ``.uploads/``) instead of the old shared ``projects/.uploads/``. A
   per-user upload_id namespace means an attacker can no longer resume a
   victim's chunked upload just by guessing the upload_id — they would
   also need to bind to the victim's MCP session.
5. Session-state files (``<digest>.json``, ``.job_index.json``, ...) stay
   under ``projects/.mcp_sessions/`` and are the registry's problem. A
   workspace exposes ``session_state`` only for tools that need a single
   path to publish to.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final, Optional, Union

# Reference `lib.paths.PROJECTS_DIR` via the module so test fixtures that
# monkeypatch ``lib.paths.PROJECTS_DIR`` actually take effect on this
# module too. Importing the constant directly (``from lib.paths import
# PROJECTS_DIR``) would copy the binding at import time and tests would
# patch the wrong object — the path-computation bug would be silent
# because production code only runs against the real PROJECTS_DIR.
from lib import paths as _lib_paths
from lib.namespace_version import (
    NamespaceLayout,
    NamespaceVersion,
    NamespaceVersionError,
    current_namespace_version,
    resolve_workspace_layout,
)
from lib.principal_registry import Principal, PrincipalNotFound
from lib.principal_sanitize import sanitize_project_id

PathLike = Union[str, Path]

# Sub-directory names inside the workspace. Kept as module constants so a
# future rename only touches one place and tests can import the strings.
_ASSETS_DIRNAME: Final[str] = "assets"
_ARTIFACTS_DIRNAME: Final[str] = "artifacts"
_RENDERS_DIRNAME: Final[str] = "renders"
_CHECKPOINTS_DIRNAME: Final[str] = "checkpoints"
_UPLOADS_DIRNAME: Final[str] = ".uploads"
_SESSION_STATE_DIRNAME: Final[str] = ".mcp_sessions"


class WorkspaceErrorError(ValueError):
    """Raised when ``resolve()`` rejects a path that escapes ``self.root``.

    A subclass of ``ValueError`` so existing code paths that already catch
    ``ValueError`` from path validators keep working unchanged. The
    distinct type exists so the test layer (and any future code that wants
    a precise 403 response) can tell the boundary violation apart from a
    generic input validation failure.
    """


@dataclass(frozen=True)
class ProjectWorkspace:
    """Per-principal, per-project workspace path bundle.

    Frozen because the layout is derived from inputs at construction time;
    re-computing it on mutation would be a security footgun. The dataclass
    holds only ``Path`` instances so it can be hashed, copied, and compared
    like any value object.

    Phase D additions:
    * ``mode`` — the active ``NamespaceVersion`` (``legacy`` / ``v2-only`` /
      ``canary``) so an audit log can record the mode without re-reading the
      env var.
    * ``candidates`` — the priority-ordered list of layout roots the factory
      considered. ``root`` is always ``candidates[0]`` (the preferred write
      target). Legacy and canary modes yield ``[v2_root, v1_root]`` so an
      unmigrated deployment can still serve reads from the v1 tree while new
      writes land under the v2 layout; ``v2-only`` mode yields ``[v2_root]`` only
      — a missing v2 layout is treated as a fresh workspace, not a
      candidate-for-fallback case.
    """

    principal: Principal
    project_id: str
    # Projects root parent (e.g. ``projects/users/<ns>/<project_id>``).
    # Always equals ``candidates[0]`` — the preferred write target under
    # the current ``NamespaceVersion``. Reads against ``root`` only see the
    # preferred layout; use ``existing_root()`` or ``candidates`` for the
    # full priority list.
    root: Path
    assets: Path
    artifacts: Path
    renders: Path
    checkpoints: Path
    # Per-principal session state dir (the registry's authoritative store
    # lives at ``projects/.mcp_sessions/principals.db``; this attribute is
    # a per-principal hook for tools that need a namespace-scoped write
    # target in the future — Phase C keeps the placeholder so the contract
    # stays stable).
    session_state: Path
    # Per-PRINCIPAL (NOT per-project) upload scratch (chunked uploads).
    # Path layout: ``projects/users/<namespace_key>/.uploads/<upload_id>.json``
    # — at the same level as individual project directories, not nested
    # inside them. A user's two in-flight uploads for project_A and
    # project_B share one upload-id namespace (uuid4 collisions are
    # negligible) but stay fully isolated from every other user's
    # uploads. Storing per-principal instead of per-project means an
    # attacker who guesses an upload_id must also forge the principal
    # binding — the state file simply does not exist in their namespace.
    upload_state: Path
    # Phase D additions — see class docstring.
    mode: NamespaceVersion
    # Full priority-ordered list of candidate roots. ``len(candidates)``
    # is 1 in ``v2-only`` mode and 2 during legacy/canary migration. The
    # tuple is read-only via dataclass+frozen.
    candidates: tuple[Path, ...] = ()

    # ------------------------------------------------------------------ #
    # Factories
    # ------------------------------------------------------------------ #

    @staticmethod
    def for_current_principal(
        project_id: str,
        version: Optional[NamespaceVersion] = None,
    ) -> "ProjectWorkspace":
        """Build a workspace for the principal bound to this MCP session.

        Delegates to ``mcp_server.current_principal()`` so the same
        fast-path / registry lookup chain applies. ``PrincipalNotFound``
        propagates untouched — callers can catch it for a 403/401
        response without ``ProjectWorkspace`` second-guessing the auth
        decision.

        ``version`` overrides ``OPENMONTAGE_NAMESPACE_VERSION`` for this
        call only. Production callers should leave it ``None`` and let
        the env var drive the policy.

        The ``mcp_server`` import is deferred to call time so this module
        stays import-safe from contexts that have not loaded the FastMCP
        server (tests, the voicebox_tts smoke runner, etc.). Importing
        ``mcp_server`` at module load would also create a circular
        dependency: ``mcp_server`` -> ``lib.principal_registry`` ->
        (no further lib deps), and ``mcp_server`` is what defines the
        ``_user_id_ctx`` ContextVar that ``current_principal`` reads.
        """
        from mcp_server import current_principal  # lazy; see docstring
        principal = current_principal()
        return ProjectWorkspace.for_principal(principal, project_id, version=version)

    @staticmethod
    def for_principal(
        principal: Principal,
        project_id: str,
        version: Optional[NamespaceVersion] = None,
    ) -> "ProjectWorkspace":
        """Build a workspace for an explicit ``Principal`` + project id.

        Used directly by tests (where ``current_principal()`` has no
        session context) and by the chunked-upload tools that already
        have a principal in hand. ``project_id`` is sanitised here — a
        rejected id raises ``ValueError`` and the workspace is never
        half-built.

        Phase D ``version`` arg: when omitted, the active ``NamespaceVersion``
        is read from ``OPENMONTAGE_NAMESPACE_VERSION``. The factory
        always consults ``lib.namespace_version.resolve_workspace_layout``
        so the legacy / v2-only / canary branching lives in one place
        rather than being spread across callers.

        Mode enforcement:

        * ``v2-only`` — the principal's ``namespace_key`` must be a valid
          32-hex string. A missing key (e.g. an unauthenticated bind)
          raises ``NamespaceVersionError``; callers should treat this the
          same way they treat a ``PrincipalNotFound`` — the request
          cannot reach disk.
        * ``canary`` — writes still require the v2 key; reads retain the v1
          fallback for every bucket while migration is in progress.
        * ``legacy`` — the principal's ``namespace_key`` is preferred when
          present; the v1 layout is a fallback for pre-migration
          deployments. A missing key still works because ``legacy`` has
          a fallback candidate.
        """
        clean_id = sanitize_project_id(project_id)
        if clean_id is None:
            raise ValueError(
                f"invalid project_id for ProjectWorkspace: {project_id!r} "
                "(allowed: [A-Za-z0-9][A-Za-z0-9._-]{{0,63}})"
            )

        # Per-principal sub-tree under ``projects/<users|services>/``.
        # ``principal.kind`` decides which top-level bucket — service
        # principals land in ``projects/services/`` and never leak into a
        # user namespace even if they share a principal_id with a user.
        kind_dir = "users" if principal.kind == "user" else "services"
        # NOTE: must read PROJECTS_DIR via the module so test fixtures that
        # monkeypatch ``lib.paths.PROJECTS_DIR`` are observed here. See the
        # import block at the top of this file.
        # We pass ``PROJECTS_DIR`` itself to ``resolve_workspace_layout``
        # and let it add the ``kind_dir`` — passing ``PROJECTS_DIR /
        # kind_dir`` here would cause the layout to add ``kind_dir`` again
        # and produce ``projects/users/users/...``.
        projects_root = _lib_paths.PROJECTS_DIR.resolve()

        # Phase D: resolve the candidate layout through the namespace-version
        # policy. ``namespace_key`` is sourced from the principal — the
        # registry is the authoritative derivation, so a hand-rolled
        # re-computation cannot disagree after a secret rotation.
        active_version = version if version is not None else current_namespace_version()
        layout = resolve_workspace_layout(
            principal_id=principal.principal_id,
            project_id=clean_id,
            projects_root=projects_root,
            kind_dir=kind_dir,
            namespace_key=principal.namespace_key,
            version=active_version,
        )

        # Namespace roots are security boundaries, not merely convenient
        # path prefixes.  Resolving a symlink here would turn an Alice
        # logical root into Bob's real root, after which a realpath-based
        # containment check could incorrectly authorize Bob's files.
        for candidate in layout.candidates:
            namespace_root = candidate.parent
            if namespace_root.is_symlink() or candidate.is_symlink():
                raise WorkspaceErrorError(
                    "symlinked principal or project namespace root is not allowed"
                )

        # ``v2-only`` (and the v2-bucket half of canary) refuses to
        # acknowledge a missing namespace_key. A missing key typically
        # means an upstream caller forgot to bind a Principal through
        # the registry — refuse loudly rather than silently falling
        # back to a v1 layout.
        if (
            getattr(layout.mode, "value", layout.mode)
            == NamespaceVersion.V2_ONLY.value
            and not principal.namespace_key
        ):
            raise NamespaceVersionError(
                "v2-only mode requires a non-empty namespace_key; "
                "the principal has none (unauthenticated bind?)"
            )

        # Preferred root drives ``root`` so existing callers (and the
        # symlink-aware resolve() boundary) keep working unchanged.
        # ``candidates`` is the full priority list so callers that want
        # the legacy fallback can iterate it explicitly.
        root = layout.preferred.resolve()
        assets = (root / _ASSETS_DIRNAME).resolve()
        artifacts = (root / _ARTIFACTS_DIRNAME).resolve()
        renders = (root / _RENDERS_DIRNAME).resolve()
        checkpoints = (root / _CHECKPOINTS_DIRNAME).resolve()
        # Per-principal session state dir. We do not write session state
        # here today — the registry owns ``projects/.mcp_sessions/`` —
        # but a tool may publish namespace-scoped session artifacts in the
        # future (e.g. project-local audit logs). Exposing the path now
        # means the field is stable when those tools land.
        session_state = (root / _SESSION_STATE_DIRNAME).resolve()
        # Upload scratch is at the PRINCIPAL root, not under any one
        # project — that way an upload_id lookup doesn't need to know
        # which project it belongs to. The final asset DOES land under
        # the per-project assets/ tree (the state file records the
        # originating project_id and ``complete`` re-derives the
        # destination workspace from it).
        principal_root = layout.preferred.parent.resolve()
        upload_state = (principal_root / _UPLOADS_DIRNAME).resolve()

        return ProjectWorkspace(
            principal=principal,
            project_id=clean_id,
            root=root,
            assets=assets,
            artifacts=artifacts,
            renders=renders,
            checkpoints=checkpoints,
            session_state=session_state,
            upload_state=upload_state,
            mode=layout.mode,
            candidates=tuple(c.resolve() for c in layout.candidates),
        )

    # ------------------------------------------------------------------ #
    # Path resolution — the single security boundary below tool input
    # validation. ``resolve`` MUST be the only way a tool computes an
    # absolute path inside this workspace; any string-format concatenation
    # in the codebase that reaches disk without going through here is a
    # bug.
    # ------------------------------------------------------------------ #

    def resolve(self, relative: PathLike) -> Path:
        """Resolve a relative path under ``self.root``.

        The returned path is always absolute and is guaranteed to live
        under ``self.root`` — symlinks, ``..`` components, and absolute
        inputs are all folded through ``Path.resolve()`` first so an
        attacker cannot escape via a symlink planted elsewhere in the
        workspace. Raises ``WorkspaceErrorError`` (a ``ValueError``) on
        any escape attempt.

        Two-stage check:
          1. Normalize: reject non-strings / non-Paths, reject empty
             strings, reject absolute inputs early so the error message
             names the right offense.
          2. Symlink-aware: call ``Path.resolve(strict=False)`` so a
             missing file doesn't raise — but if it does point outside
             the workspace we reject. ``strict=False`` matters for the
             common case where a tool is about to create the file.
        """
        if relative is None:
            raise WorkspaceErrorError("cannot resolve a None path")
        if isinstance(relative, str):
            stripped = relative.strip()
            if not stripped:
                raise WorkspaceErrorError("cannot resolve an empty relative path")
            rel_path = Path(stripped)
        elif isinstance(relative, Path):
            rel_path = relative
        else:
            raise WorkspaceErrorError(
                f"relative path must be str or Path, got {type(relative).__name__}"
            )
        # Reject absolute / parent-traversal early so the error message is
        # specific. ``Path.is_absolute()`` covers POSIX (``/foo``) and
        # Windows (``C:\foo``, ``\\server\share``) prefixes.
        if rel_path.is_absolute():
            raise WorkspaceErrorError(
                f"relative path must not be absolute: {relative!r}"
            )
        # Build the candidate without resolving, then resolve the FULL
        # path (not just the relative part) so symlinks inside the
        # workspace are evaluated against their real targets.
        candidate = (self.root / rel_path).resolve(strict=False)
        root_resolved = self.root.resolve(strict=False)
        try:
            candidate.relative_to(root_resolved)
        except ValueError as exc:
            raise WorkspaceErrorError(
                f"path {relative!r} resolves outside workspace root "
                f"({candidate} not under {root_resolved})"
            ) from exc
        return candidate

    # ------------------------------------------------------------------ #
    # Convenience — make the dataclass self-documenting in repr/equality
    # and cheap to compare across calls.
    # ------------------------------------------------------------------ #

    def __post_init__(self) -> None:
        # Defense in depth: a caller could construct
        # ``ProjectWorkspace(...)`` with a raw project_id bypassing
        # ``for_principal`` (frozen=True blocks mutation but does not
        # block passing arbitrary strings in __init__). We re-sanitise
        # so the invariant "project_id is clean" cannot be violated.
        clean = sanitize_project_id(self.project_id)
        if clean is None:
            raise ValueError(
                f"ProjectWorkspace constructed with invalid project_id: "
                f"{self.project_id!r}"
            )
        if not isinstance(self.principal, Principal):
            raise ValueError(
                f"ProjectWorkspace.principal must be a Principal, "
                f"got {type(self.principal).__name__}"
            )
        # ``mode`` must be one of the three enum members; a hand-constructed
        # workspace cannot smuggle in a non-enum value (and ``v2-only`` is
        # only safe when the candidates list still contains a valid v2 root).
        # Compare by canonical string value instead of ``isinstance`` so a
        # module reload in a long-lived worker/test process cannot leave a
        # valid mode rejected as an enum from another module incarnation.
        if getattr(self.mode, "value", self.mode) not in {
            NamespaceVersion.LEGACY.value,
            NamespaceVersion.V2_ONLY.value,
            NamespaceVersion.CANARY.value,
        }:
            raise ValueError(
                f"ProjectWorkspace.mode must be a NamespaceVersion, "
                f"got {type(self.mode).__name__}"
            )
        if (
            getattr(self.mode, "value", self.mode)
            == NamespaceVersion.V2_ONLY.value
            and not self.principal.namespace_key
        ):
            raise NamespaceVersionError(
                "v2-only mode requires a non-empty namespace_key; "
                "the principal has none (unauthenticated bind?)"
            )
        # ``candidates`` invariant: at least one root, and ``root`` is the
        # first one. A caller that builds a workspace bypassing the factory
        # must respect this or ``root`` becomes a lie relative to ``candidates``.
        if not self.candidates:
            raise ValueError(
                "ProjectWorkspace.candidates must contain at least one root"
            )
        if self.root != self.candidates[0]:
            raise ValueError(
                "ProjectWorkspace.root must equal candidates[0] (the "
                "preferred layout root); "
                f"root={self.root} candidates[0]={self.candidates[0]}"
            )

    # ------------------------------------------------------------------ #
    # Phase D convenience — look up the on-disk root across candidates.
    # ------------------------------------------------------------------ #

    def existing_root(self) -> Optional[Path]:
        """Return the first ``candidates`` entry that exists on disk, else ``None``.

        Read paths (legacy migration tools, Backlot discovery, rollback
        drill) need to consult whichever layout the deployment actually
        has data under. ``root`` only reflects the *preferred* write
        target; ``existing_root()`` walks the candidates in priority
        order and returns the first hit.

        ``Path.is_dir()`` (not ``exists()``) — a stale file at the
        candidate path that is not a directory is treated as a miss
        because the workspace contract is a directory, not a file.
        """
        for candidate in self.candidates:
            if candidate.is_dir():
                return candidate
        return None

    @property
    def read_roots(self) -> tuple[Path, ...]:
        """All on-disk roots this principal may read, v2 before legacy.

        Writes must use ``root``.  Read paths should use this property (or
        ``resolve_read``) so an unmigrated v1 project remains readable while
        never widening access beyond this principal's two authenticated
        namespace roots.
        """
        return self.candidates

    def resolve_read(self, relative: PathLike) -> Path:
        """Resolve a relative path against v2, then the legacy root.

        The first existing file wins.  If no candidate exists, return the
        v2 path so callers can report a normal not-found result.  The same
        input validation and symlink-aware containment check as ``resolve``
        is applied independently to every candidate.
        """
        errors: list[Exception] = []
        for candidate_root in self.read_roots:
            # Avoid constructing a second dataclass merely to apply the
            # boundary check; candidate roots are already derived from the
            # authenticated principal.  Normalize the path exactly as
            # ``resolve`` does below.
            try:
                if relative is None:
                    raise WorkspaceErrorError("cannot resolve a None path")
                if isinstance(relative, str):
                    stripped = relative.strip()
                    if not stripped:
                        raise WorkspaceErrorError("cannot resolve an empty relative path")
                    rel_path = Path(stripped)
                elif isinstance(relative, Path):
                    rel_path = relative
                else:
                    raise WorkspaceErrorError(
                        f"relative path must be str or Path, got {type(relative).__name__}"
                    )
                if rel_path.is_absolute():
                    raise WorkspaceErrorError(
                        f"relative path must not be absolute: {relative!r}"
                    )
                resolved = (candidate_root / rel_path).resolve(strict=False)
                root_resolved = candidate_root.resolve(strict=False)
                resolved.relative_to(root_resolved)
            except (ValueError, WorkspaceErrorError) as exc:
                errors.append(exc)
                continue
            if resolved.is_file():
                return resolved
        if errors and len(errors) == len(self.read_roots):
            raise errors[0]
        return self.resolve(relative)


__all__ = [
    "ProjectWorkspace",
    "WorkspaceErrorError",
    "PrincipalNotFound",  # re-export so callers don't need a second import
    "NamespaceVersion",
    "NamespaceVersionError",
    "NamespaceLayout",  # re-exported from lib.namespace_version for callers
]
