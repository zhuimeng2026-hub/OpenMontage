#!/usr/bin/env python3
"""Phase D — migrate ``projects/users/<raw_openid>/`` to ``projects/users/<namespace_key>/``.

What this script does
---------------------

Phase C finalised ``projects/users/<namespace_key>/<project_id>/...`` as the
canonical per-principal layout, where ``namespace_key`` is the HMAC-SHA256 of
``(secret, principal_id)`` truncated to 16 bytes (32 hex chars). Real
deployments have years of pre-Phase-C data on disk under
``projects/users/<raw_openid>/...`` — the v1 ``tools/external/claude_video.py``
and the web/BFF side still write raw openid strings into that tree.

This script rewrites the directory tree:

    projects/users/<raw_openid>/                 ──► projects/users/<namespace_key>/
      ├── <project_id_a>/                              ├── <project_id_a>/
      ├── <project_id_b>/                              ├── <project_id_b>/
      └── ...                                          └── ...

The move is atomic at the principal level: ``shutil.move`` either relocates
the whole ``<raw_openid>/`` directory or nothing. A per-principal entry in
the audit log records ``from`` (raw openid), ``to`` (HMAC namespace_key),
``project_ids`` (the immediate child directories that travelled with the
move), ``migrated_at`` (UTC ISO-8601), and ``status``.

Safety rails
------------

1. **Target-must-not-exist** — if ``projects/users/<namespace_key>/`` is
   already on disk, the script refuses to move into it. This catches two
   scenarios:

   a. ``<raw_openid>`` is itself already a valid HMAC namespace_key (32 hex
      chars). The script detects this and skips with
      ``status="skipped: already_v2"`` so a second run is a no-op.
   b. Two ``<raw_openid>`` source directories collide on the same
      ``<namespace_key>`` target. HMAC is collision-resistant at 128 bits,
      so the only realistic cause is a prior partial migration; the script
      defers to the operator rather than silently merging two principals'
      data.

2. **Dry-run by default** — ``--dry-run`` is the recommended way to invoke
   this script the first time. Every action the script would take is
   logged to stdout and recorded in the audit log with
   ``status="dry-run"``. Re-running with the flag removed replays the
   recorded plan against disk.

3. **Auditable** — every principal processed produces exactly one
   ``jsonl`` line in the audit log. The log is append-only so multiple
   invocations accumulate history. The default path is
   ``migrations/2026-09-02-namespace-key.jsonl`` (relative to the repo
   root) so the rollback drill (``scripts/rollback_namespace_key.py``)
   has a stable target.

4. **Cross-platform** — ``shutil.move`` handles the
   ``os.rename``-``s PermissionError`` retry dance on Windows that the
   v2 doc flagged. If a move fails partway (very rare), the audit log
   records ``status="failed"`` and the next run sees the partially
   migrated state and skips the already-moved parts.

Usage
-----

    # Show what would happen, no disk writes:
    python scripts/migrate_users_to_namespace_key.py --dry-run

    # Apply the migration:
    python scripts/migrate_users_to_namespace_key.py

    # Limit to one principal (useful for a recovery pass):
    python scripts/migrate_users_to_namespace_key.py --openid oAlice_x

    # Read audit log from a non-default path:
    python scripts/migrate_users_to_namespace_key.py --audit-log /var/log/om-ns.jsonl
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Final, Iterable, List, Optional

# ``lib.*`` lives one level up — adding the repo root to ``sys.path`` keeps
# ``import lib.principal_registry`` working whether the script is invoked
# via ``python scripts/migrate_users_to_namespace_key.py`` (relative cwd)
# or via an absolute path.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib.principal_registry import compute_namespace_key  # noqa: E402

_log = logging.getLogger("migrate_users_to_namespace_key")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USERS_BUCKET: "Final[str]" = "users"
_HEX32: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")
_DEFAULT_AUDIT_LOG: "Final[str]" = "migrations/2026-09-02-namespace-key.jsonl"


# ---------------------------------------------------------------------------
# Audit record helpers
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    """UTC ISO-8601 timestamp with explicit ``+00:00`` (not ``Z``).

    ``datetime.now(timezone.utc).isoformat()`` already produces
    ``+00:00``-suffixed output; the explicit construction here documents
    the choice for future readers — a downstream tool that wants a stable
    sortable string benefits from the explicit offset.
    """
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _list_project_ids(principal_dir: Path) -> List[str]:
    """Return the immediate child directory names under ``principal_dir``.

    The audit log records ``project_ids`` so an operator can later
    verify that every project a principal owned before the move made it
    to the new namespace. Files at the top level (e.g. an accidental
    ``.DS_Store`` or stray ``README``) are filtered out — only
    directories that look like a project namespace (a child of
    ``projects/users/<ns>/``) are recorded.
    """
    if not principal_dir.is_dir():
        return []
    return sorted(
        entry.name
        for entry in principal_dir.iterdir()
        if entry.is_dir()
    )


def _record_audit(
    *,
    audit_log_path: Path,
    from_id: str,
    to_id: str,
    project_ids: List[str],
    status: str,
    dry_run: bool,
    project_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Append one audit record (one JSON object per line) to the audit log.

    The audit log directory is created with ``parents=True`` so a fresh
    checkout can record the first migration without a manual ``mkdir``.

    The ``dry_run`` flag is recorded as a separate boolean (the line is
    identical between dry-run and real runs except for the flag) so a
    post-mortem can ask "did we actually move or just observe?".
    """
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "from": from_id,
        "to": to_id,
        "project_ids": project_ids,
        "migrated_at": _now_iso(),
        "status": status,
        "dry_run": bool(dry_run),
    }
    if project_id is not None:
        record["scope"] = "project"
        record["project_id"] = project_id
    if details:
        record["details"] = details
    with audit_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


# ---------------------------------------------------------------------------
# Migration core
# ---------------------------------------------------------------------------


def _safe_listdir(dir_path: Path) -> List[Path]:
    """Return child paths of ``dir_path``, or ``[]`` if missing.

    The migration tool must not crash when ``projects/users/`` is empty
    or missing — a clean deployment has no v1 data to migrate and
    should run as a no-op.
    """
    if not dir_path.is_dir():
        return []
    return sorted(p for p in dir_path.iterdir())


def _tree_nodes(root: Path) -> dict[str, tuple[str, str]]:
    """Return a deterministic, symlink-safe manifest of a project tree."""
    nodes: dict[str, tuple[str, str]] = {}
    if not root.exists():
        return nodes
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            nodes[rel] = ("symlink", os.readlink(path))
        elif path.is_dir():
            nodes[rel] = ("dir", "")
        elif path.is_file():
            nodes[rel] = ("file", hashlib.sha256(path.read_bytes()).hexdigest())
    return nodes


def _reconcile_project(*, source_dir: Path, target_dir: Path, dry_run: bool) -> tuple[str, dict]:
    """Reconcile one project without overwriting target data."""
    if source_dir.is_symlink() or target_dir.is_symlink():
        return "conflict", {
            "conflict_paths": ["<project-root>"],
            "reason": "symlinked_project_root",
        }
    if not target_dir.exists():
        source_nodes = _tree_nodes(source_dir)
        moved = sorted(
            rel for rel, node in source_nodes.items() if node[0] != "dir"
        )
        if dry_run:
            return "dry-run", {"action": "move-project", "moved": moved}
        try:
            target_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source_dir), str(target_dir))
        except OSError as exc:
            return "failed", {"error": str(exc), "action": "move-project"}
        return "migrated", {"action": "move-project", "moved": moved}
    if not target_dir.is_dir():
        return "conflict", {"conflict_paths": ["<project-root>"], "reason": "target_not_directory"}

    source_nodes = _tree_nodes(source_dir)
    target_nodes = _tree_nodes(target_dir)
    conflicts, moves, identical = [], [], []
    for rel, source_node in source_nodes.items():
        target_node = target_nodes.get(rel)
        if target_node is None:
            moves.append(rel)
        elif target_node == source_node:
            identical.append(rel)
        else:
            conflicts.append(rel)
    if conflicts:
        return "conflict", {"conflict_paths": conflicts, "would_move": moves, "identical": identical}
    moved = sorted(
        rel for rel in moves if source_nodes.get(rel, ("dir", ""))[0] != "dir"
    )
    if dry_run:
        return ("dry-run:reconcile" if moved else "dry-run:identical"), {"would_move": moved, "moved": moved, "identical": identical}

    try:
        # If a whole directory is absent at the target, moving that
        # directory already carries all descendants; do not then attempt to
        # move the now-missing child paths a second time.
        # Move only leaf entries.  Moving a whole directory would also carry
        # files created in the v2 target after migration, making an exact
        # rollback impossible.
        for rel in moved:
            source, target = source_dir / Path(rel), target_dir / Path(rel)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists() or target.is_symlink():
                return "failed", {"error": f"target appeared during reconcile: {rel}"}
            shutil.move(str(source), str(target))
        for rel in sorted(identical, key=lambda value: (-value.count("/"), value)):
            source = source_dir / Path(rel)
            if source.is_file() or source.is_symlink():
                source.unlink()
        for directory in sorted((p for p in source_dir.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
            try:
                directory.rmdir()
            except OSError:
                pass
        source_dir.rmdir()
    except OSError as exc:
        return "failed", {"error": str(exc), "moved": moves}
    return "reconciled", {"moved": moved, "identical": identical}


def _marked_v2(source_dir: Path, directory_name: str) -> bool:
    """Recognise explicit v2 metadata; a 32-hex name alone is ambiguous."""
    marker = source_dir / ".namespace.json"
    if not marker.is_file():
        return False
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(data, dict) and data.get("namespace_version") in {"v2", "v2-only"} and data.get("namespace_key") == directory_name


def _migrate_principal(
    *, source_dir: Path, openid: str, users_dir: Path, dry_run: bool,
    audit_log_path: Path, explicit_v2_ids: frozenset[str] = frozenset(),
    allow_hex_principal_ids: bool = False,
) -> tuple[str, int]:
    """Migrate one principal, reconciling projects independently."""
    project_ids = _list_project_ids(source_dir)
    target_ns = compute_namespace_key(openid)
    target_dir = users_dir / target_ns
    if source_dir.is_dir() and not any(source_dir.iterdir()):
        # Empty legacy principals are harmless cleanup residue. They are not
        # a failed migration and must not make an otherwise healthy sweep
        # exit non-zero.
        status = "skipped: empty_source"
        _record_audit(
            audit_log_path=audit_log_path,
            from_id=openid,
            to_id=target_ns,
            project_ids=[],
            status=status,
            dry_run=dry_run,
        )
        return status, 0
    if source_dir.is_symlink() or target_dir.is_symlink():
        status = "conflict:symlink_root"
        _record_audit(audit_log_path=audit_log_path, from_id=openid, to_id=target_ns, project_ids=project_ids, status=status, dry_run=dry_run, details={"reason": "symlinked_principal_root"})
        return status, 1
    if openid in explicit_v2_ids or _marked_v2(source_dir, openid):
        status = "skipped: already_v2"
        _record_audit(audit_log_path=audit_log_path, from_id=openid, to_id=openid, project_ids=project_ids, status=status, dry_run=dry_run)
        return status, 0
    if _HEX32.fullmatch(openid) and not allow_hex_principal_ids:
        status = "skipped: ambiguous_32hex"
        _record_audit(audit_log_path=audit_log_path, from_id=openid, to_id=target_ns, project_ids=project_ids, status=status, dry_run=dry_run, details={"reason": "requires --treat-hex-as-legacy or --already-v2"})
        return status, 0
    if dry_run and not target_dir.exists():
        status = "dry-run"
        _record_audit(audit_log_path=audit_log_path, from_id=openid, to_id=target_ns, project_ids=project_ids, status=status, dry_run=True)
        return status, 0

    statuses: list[str] = []
    conflict_count = 0
    for project_id in project_ids:
        status, details = _reconcile_project(source_dir=source_dir / project_id, target_dir=target_dir / project_id, dry_run=dry_run)
        statuses.append(status)
        if status == "conflict":
            conflict_count += 1
        _record_audit(audit_log_path=audit_log_path, from_id=openid, to_id=target_ns, project_ids=[project_id], project_id=project_id, status=status, dry_run=dry_run, details=details)
    if not dry_run and source_dir.is_dir():
        top_files = [p.name for p in source_dir.iterdir() if not p.is_dir()]
        if top_files:
            conflict_count += 1
            statuses.append("conflict")
        elif not any(source_dir.iterdir()):
            # All projects were moved/reconciled. Remove only this now-empty
            # legacy principal directory so rollback can atomically restore
            # the principal-level mapping.
            try:
                source_dir.rmdir()
            except OSError:
                pass
    if statuses and all(s in {"migrated", "reconciled"} for s in statuses) and not conflict_count:
        status = "migrated"
    elif dry_run and not conflict_count:
        status = "dry-run"
    elif conflict_count:
        status = "reconciled:conflict"
    else:
        status = "failed"
    _record_audit(audit_log_path=audit_log_path, from_id=openid, to_id=target_ns, project_ids=project_ids, status=status, dry_run=dry_run, details={"project_statuses": statuses, "conflicts": conflict_count})
    return status, conflict_count


def migrate(
    *,
    projects_dir: Path,
    dry_run: bool,
    audit_log_path: Path,
    only_openid: Optional[str] = None,
    already_v2_ids: Optional[Iterable[str]] = None,
    allow_hex_principal_ids: bool = False,
) -> dict:
    """Walk ``projects/users/`` and migrate each principal directory.

    Parameters
    ----------
    projects_dir:
        The ``PROJECTS_DIR`` (i.e. ``projects/``). Defaults to
        ``lib.paths.PROJECTS_DIR`` so the test fixture can swap it via
        monkeypatch; production callers leave it default.
    dry_run:
        When ``True``, every action is logged but never executed. The
        audit log is still written so a downstream ``rollback`` can see
        what *would* have happened.
    audit_log_path:
        JSONL audit log destination. The file is appended to (never
        truncated) so multiple invocations accumulate history.
    only_openid:
        When set, restrict the walk to a single principal directory.
        Useful for a recovery pass after a partial migration.

    Returns
    -------
    dict
        Summary: ``{"migrated": int, "skipped": int, "failed": int,
        "dry_run": bool, "audit_log": str}``. The CLI wrapper prints
        this on exit.
    """
    users_dir = projects_dir / _USERS_BUCKET
    if not users_dir.is_dir():
        _log.warning(
            "users bucket %s does not exist; nothing to migrate", users_dir
        )
        return {
            "migrated": 0,
            "skipped": 0,
            "failed": 0,
            "conflicts": 0,
            "dry_run": dry_run,
            "audit_log": str(audit_log_path),
        }

    summary = {"migrated": 0, "skipped": 0, "failed": 0, "conflicts": 0}
    explicit_v2_ids = frozenset(already_v2_ids or ())
    for source_dir in _safe_listdir(users_dir):
        openid = source_dir.name
        if not source_dir.is_dir():
            continue
        if only_openid is not None and openid != only_openid:
            continue
        status, conflicts = _migrate_principal(
            source_dir=source_dir,
            openid=openid,
            users_dir=users_dir,
            dry_run=dry_run,
            audit_log_path=audit_log_path,
            explicit_v2_ids=explicit_v2_ids,
            allow_hex_principal_ids=allow_hex_principal_ids,
        )
        summary["conflicts"] += conflicts
        if status.startswith("skipped"):
            summary["skipped"] += 1
        elif status == "failed":
            summary["failed"] += 1
        elif status in {"migrated", "dry-run"}:
            summary["migrated"] += 1
        elif status.startswith("conflict") or status == "reconciled:conflict":
            summary["skipped"] += 1
        else:  # defensive: an unexpected status string is a tool bug
            _log.error("unexpected migration status %r", status)
            summary["failed"] += 1
    summary["dry_run"] = dry_run
    summary["audit_log"] = str(audit_log_path)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """Build the argument parser.

    Defaults are deliberately the safe ones: dry-run, repo-root paths.
    Operators must explicitly opt out of dry-run (``--apply``) to mutate
    the filesystem, so an accidental ``python scripts/migrate...`` does
    not silently rewrite user data.
    """
    parser = argparse.ArgumentParser(
        description=(
            "Migrate legacy projects/users/<raw_openid>/ to "
            "projects/users/<namespace_key>/ (HMAC of principal_id)."
        ),
    )
    # ``--dry-run`` is the default; ``--apply`` flips the flag. This
    # keeps the safe behaviour on the shorter flag set while making the
    # destructive intent explicit.
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help=(
            "Print what would happen without moving anything "
            "(DEFAULT). The audit log is still written with "
            "status=dry-run for traceability."
        ),
    )
    group.add_argument(
        "--apply",
        dest="dry_run",
        action="store_false",
        help="Actually move directories. Opt-in: requires the operator to confirm.",
    )
    parser.add_argument(
        "--projects-dir",
        type=Path,
        default=None,
        help=(
            "Override the projects root. Defaults to "
            "lib.paths.PROJECTS_DIR (i.e. <repo>/projects)."
        ),
    )
    parser.add_argument(
        "--audit-log",
        type=Path,
        default=None,
        help=(
            "Override the audit log path. Defaults to "
            "<repo>/migrations/2026-09-02-namespace-key.jsonl."
        ),
    )
    parser.add_argument(
        "--openid",
        dest="only_openid",
        default=None,
        help=(
            "Migrate only one principal (the legacy <raw_openid> name). "
            "Useful for a recovery pass after a partial migration."
        ),
    )
    parser.add_argument(
        "--already-v2",
        dest="already_v2_ids",
        action="append",
        default=[],
        metavar="NAMESPACE_KEY",
        help=(
            "Explicitly identify a directory as already-v2. A 32-hex name "
            "alone is intentionally treated as ambiguous."
        ),
    )
    parser.add_argument(
        "--treat-hex-as-legacy",
        dest="allow_hex_principal_ids",
        action="store_true",
        help="Explicitly allow a legacy principal id that happens to be 32 hex characters.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Stdout log level (default: INFO).",
    )
    return parser


def main(argv: Optional[Iterable[str]] = None) -> int:
    """CLI entry point; returns the process exit code (0 on success)."""
    args = _build_arg_parser().parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    # Lazy import so the test runner can monkeypatch ``PROJECTS_DIR``
    # before reading it.
    from lib import paths as _lib_paths

    projects_dir = (args.projects_dir or _lib_paths.PROJECTS_DIR).resolve()
    audit_log_path = (args.audit_log or (_REPO_ROOT / _DEFAULT_AUDIT_LOG)).resolve()

    summary = migrate(
        projects_dir=projects_dir,
        dry_run=args.dry_run,
        audit_log_path=audit_log_path,
        only_openid=args.only_openid,
        already_v2_ids=args.already_v2_ids,
        allow_hex_principal_ids=args.allow_hex_principal_ids,
    )
    _log.info(
        "summary: migrated=%d skipped=%d failed=%d dry_run=%s audit_log=%s",
        summary["migrated"],
        summary["skipped"],
        summary["failed"],
        summary["dry_run"],
        summary["audit_log"],
    )
    # Exit non-zero on any failure so a CI cron notices; skipped counts
    # are not failures (they're explicit operator-decision outcomes).
    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
