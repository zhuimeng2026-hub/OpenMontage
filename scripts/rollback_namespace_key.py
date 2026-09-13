#!/usr/bin/env python3
"""Phase D — reverse a previous ``migrate_users_to_namespace_key.py`` run.

What this script does
---------------------

The forward migration renames ``projects/users/<raw_openid>/`` to
``projects/users/<namespace_key>/`` (HMAC of the openid) and writes one
JSON line per principal to ``migrations/2026-09-02-namespace-key.jsonl``.
This script reads that audit log and reverses the rename:

    projects/users/<namespace_key>/   ──► projects/users/<raw_openid>/

The audit log is the *only* source of truth for the reverse mapping — a
real rollback cannot depend on recomputing the HMAC (the secret might
have rotated) or on filenames that were modified after migration. The
script trusts the ``from``/``to`` columns recorded by the forward pass.

Safety rails
------------

1. **Target-must-not-exist** — same rule as the forward pass. If
   ``projects/users/<raw_openid>/`` is already on disk, the script
   refuses to clobber it. The most likely cause is a partial rollback
   that crashed mid-way; the operator decides how to merge.

2. **Source-must-exist** — the namespace_key directory must still be
   present. If it isn't, a previous rollback (or a manual deletion) got
   there first and there's nothing to undo.

3. **Status filtering** — by default only ``status="migrated"`` lines
   drive a rename. ``dry-run``, ``skipped:``, and ``failed`` records
   are ignored because they never produced a directory movement.

4. **Append-only audit** — the rollback writes a parallel
   ``migrations/2026-09-02-namespace-key.rollback.jsonl`` so the
   operation is itself auditable. ``--audit-log`` overrides both the
   forward and rollback log paths so an operator can keep them
   side-by-side if desired.

5. **Dry-run by default** — same default as the forward script. The
   audit log records ``status="dry-run"`` for every would-be move so
   the operator can re-run with ``--apply`` once they're confident.

Usage
-----

    # Show what would happen:
    python scripts/rollback_namespace_key.py --dry-run

    # Apply the rollback (destructive; opt-in):
    python scripts/rollback_namespace_key.py --apply

    # Use a non-default audit log:
    python scripts/rollback_namespace_key.py \\
        --audit-log /var/log/om-ns.jsonl

    # Roll back a single principal:
    python scripts/rollback_namespace_key.py --to oAlice_x
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import logging
import os
import shutil
import sys
import re
from pathlib import Path
from typing import Final, Iterable, List, Optional

# Same sys.path bootstrap as the forward script — keeps the script
# runnable from any cwd without ``PYTHONPATH`` exports.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from lib import paths as _lib_paths  # noqa: E402
from lib.principal_sanitize import sanitize_principal_id, sanitize_project_id  # noqa: E402

_log = logging.getLogger("rollback_namespace_key")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USERS_BUCKET: "Final[str]" = "users"
_DEFAULT_AUDIT_LOG: "Final[str]" = "migrations/2026-09-02-namespace-key.jsonl"
_DEFAULT_ROLLBACK_LOG: "Final[str]" = "migrations/2026-09-02-namespace-key.rollback.jsonl"
# Only lines with these values drive a rename; everything else is
# recorded but no-ops. ``skipped`` and ``failed`` lines never produced
# a move in the forward pass, so reversing them would either be a
# no-op (target doesn't exist) or a corrupt operation (forward-pass
# never finished).
_ROLLBACKABLE_STATUSES: Final[frozenset[str]] = frozenset({"migrated"})
_HEX32: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{32}$")


def _safe_audit_ids(users_dir: Path, raw_id: str, namespace_id: str) -> tuple[Path, Path]:
    """Validate audit IDs before they become filesystem path components."""
    if sanitize_principal_id(raw_id) != raw_id:
        raise ValueError("audit record contains an invalid principal id")
    if _HEX32.fullmatch(namespace_id) is None or namespace_id != namespace_id.lower():
        raise ValueError("audit record contains an invalid namespace key")
    safe_root = users_dir.resolve(strict=False)
    raw_path = (users_dir / raw_id).resolve(strict=False)
    namespace_path = (users_dir / namespace_id).resolve(strict=False)
    for path in (raw_path, namespace_path):
        if path.parent != safe_root or path.name not in {raw_id, namespace_id}:
            raise ValueError("audit record path escapes users directory")
    return raw_path, namespace_path


# ---------------------------------------------------------------------------
# Audit log reading
# ---------------------------------------------------------------------------


def _read_audit(audit_log_path: Path) -> List[dict]:
    """Read every JSON line from ``audit_log_path``.

    Missing file → ``[]`` (a clean rollback is a no-op on a missing
    forward log). Malformed lines are logged and skipped so one bad
    record does not abort the entire run — the operator can fix the
    line manually and re-run.

    Returns the raw ``dict`` objects so the caller can introspect
    ``status`` / ``from`` / ``to`` directly. The schema is documented
    in ``scripts/migrate_users_to_namespace_key.py``.
    """
    if not audit_log_path.is_file():
        _log.warning("audit log %s does not exist; nothing to roll back", audit_log_path)
        return []
    records: List[dict] = []
    with audit_log_path.open("r", encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            stripped = raw.strip()
            if not stripped:
                continue
            try:
                record = json.loads(stripped)
                if not isinstance(record, dict):
                    _log.error(
                        "skipping non-object audit line %d in %s",
                        line_no,
                        audit_log_path,
                    )
                    records.append({"__invalid_audit_record__": True})
                else:
                    records.append(record)
            except json.JSONDecodeError as exc:
                _log.error(
                    "skipping malformed line %d in %s: %s",
                    line_no,
                    audit_log_path,
                    exc,
                )
    return records


def _record_rollback(
    *,
    rollback_log_path: Path,
    from_id: str,
    to_id: str,
    project_ids: List[str],
    status: str,
    dry_run: bool,
    project_id: Optional[str] = None,
    details: Optional[dict] = None,
) -> None:
    """Append one rollback audit record.

    Mirror of the forward script's ``_record_audit`` — same JSON
    schema (``from`` / ``to`` / ``project_ids`` / ``migrated_at`` /
    ``status`` / ``dry_run``) so a post-mortem tool can read both
    files without schema-mapping logic. ``migrated_at`` is renamed
    in spirit (it's "rolled_at") but the field name stays for
    schema parity with the forward log.
    """
    rollback_log_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "from": from_id,
        "to": to_id,
        "project_ids": project_ids,
        "migrated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "status": status,
        "dry_run": bool(dry_run),
    }
    if project_id is not None:
        record["scope"] = "project"
        record["project_id"] = project_id
    if details:
        record["details"] = details
    with rollback_log_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        fh.write("\n")


def _safe_relative_entries(entries: object) -> list[str]:
    """Validate the exact leaf paths recorded by the forward migration."""
    if not isinstance(entries, list):
        raise ValueError("migration audit has no safe moved-entry list")
    safe: list[str] = []
    for entry in entries:
        if not isinstance(entry, str) or not entry or entry in {".", ".."}:
            raise ValueError("migration audit contains an invalid moved entry")
        path = Path(entry)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("migration audit contains an escaping moved entry")
        safe.append(path.as_posix())
    return safe


def _has_symlinked_parent(root: Path, rel: str) -> bool:
    """Return whether a moved entry would traverse a symlinked directory."""
    current = root
    parts = Path(rel).parts
    for part in parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False


def _rollback_project(
    *,
    users_dir: Path,
    from_id: str,
    to_id: str,
    project_id: str,
    moved_entries: list[str],
    dry_run: bool,
    rollback_log_path: Path,
) -> str:
    """Move only entries this migration actually moved back to v1."""
    if sanitize_project_id(project_id) != project_id:
        status = "failed"
        _record_rollback(
            rollback_log_path=rollback_log_path, from_id=to_id, to_id=from_id,
            project_ids=[project_id], status=status, dry_run=dry_run,
            project_id=project_id,
        )
        return status
    # Callers pass the forward mapping reversed: ``from_id`` is the v2
    # namespace (source on rollback), ``to_id`` is the legacy id (destination).
    source_dir = users_dir / to_id / project_id
    target_dir = users_dir / from_id / project_id
    if source_dir.is_symlink() or target_dir.is_symlink():
        status = "failed"
        _record_rollback(
            rollback_log_path=rollback_log_path, from_id=from_id, to_id=to_id,
            project_ids=[project_id], status=status, dry_run=dry_run,
            project_id=project_id,
            details={"error": "symlinked project root"},
        )
        return status
    if not moved_entries:
        status = "skipped: no_moved_entries"
        _record_rollback(
            rollback_log_path=rollback_log_path, from_id=from_id, to_id=to_id,
            project_ids=[project_id], status=status, dry_run=dry_run,
            project_id=project_id,
        )
        return status
    if not target_dir.is_dir():
        status = "skipped: source_missing"
        _record_rollback(
            rollback_log_path=rollback_log_path, from_id=from_id, to_id=to_id,
            project_ids=[project_id], status=status, dry_run=dry_run,
            project_id=project_id,
        )
        return status
    if dry_run:
        status = "dry-run"
        _record_rollback(
            rollback_log_path=rollback_log_path, from_id=from_id, to_id=to_id,
            project_ids=[project_id], status=status, dry_run=True,
            project_id=project_id, details={"moved": moved_entries},
        )
        return status

    try:
        for rel in moved_entries:
            if _has_symlinked_parent(target_dir, rel) or _has_symlinked_parent(source_dir, rel):
                raise OSError(f"migrated entry traverses a symlink: {rel}")
            source = target_dir / Path(rel)
            destination = source_dir / Path(rel)
            if not source.is_file() and not source.is_symlink():
                raise OSError(f"migrated entry is missing: {rel}")
            if destination.exists() or destination.is_symlink():
                raise OSError(f"rollback target appeared: {rel}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(destination))
        # Remove only empty structural directories left in the v2 project;
        # never remove a directory containing post-migration data.
        for directory in sorted(
            (p for p in target_dir.rglob("*") if p.is_dir()),
            key=lambda p: len(p.parts), reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass
        try:
            target_dir.rmdir()
            target_dir.parent.rmdir()
        except OSError:
            pass
    except OSError as exc:
        status = "failed"
        _record_rollback(
            rollback_log_path=rollback_log_path, from_id=to_id, to_id=from_id,
            project_ids=[project_id], status=status, dry_run=False,
            project_id=project_id, details={"error": str(exc)},
        )
        return status

    status = "migrated"
    _record_rollback(
        rollback_log_path=rollback_log_path, from_id=from_id, to_id=to_id,
        project_ids=[project_id], status=status, dry_run=False,
        project_id=project_id, details={"moved": moved_entries},
    )
    return status


# ---------------------------------------------------------------------------
# Rollback core
# ---------------------------------------------------------------------------


def _rollback_one(
    *,
    users_dir: Path,
    from_id: str,
    to_id: str,
    project_ids: List[str],
    dry_run: bool,
    rollback_log_path: Path,
) -> str:
    """Reverse one principal's rename.

    Returns the ``status`` field ("migrated" / "dry-run" /
    "skipped: source_missing" / "skipped: target_exists" / "failed").
    The script's ``main`` aggregates these into the exit summary.
    """
    source_dir = users_dir / from_id
    target_dir = users_dir / to_id

    if not source_dir.is_dir():
        status = "skipped: source_missing"
        _log.warning(
            "[skip-source-missing] %s does not exist; nothing to roll back",
            source_dir,
        )
        _record_rollback(
            rollback_log_path=rollback_log_path,
            from_id=from_id,
            to_id=to_id,
            project_ids=project_ids,
            status=status,
            dry_run=dry_run,
        )
        return status

    if target_dir.exists():
        status = "skipped: target_exists"
        _log.warning(
            "[skip-target-exists] %s already exists; refusing to clobber "
            "(a previous partial rollback or a re-creation likely)",
            target_dir,
        )
        _record_rollback(
            rollback_log_path=rollback_log_path,
            from_id=from_id,
            to_id=to_id,
            project_ids=project_ids,
            status=status,
            dry_run=dry_run,
        )
        return status

    if dry_run:
        status = "dry-run"
        _log.info(
            "[dry-run] WOULD MOVE %s -> %s (projects: %s)",
            source_dir,
            target_dir,
            project_ids,
        )
        _record_rollback(
            rollback_log_path=rollback_log_path,
            from_id=from_id,
            to_id=to_id,
            project_ids=project_ids,
            status=status,
            dry_run=True,
        )
        return status

    try:
        shutil.move(str(source_dir), str(target_dir))
    except OSError as exc:
        status = "failed"
        _log.error(
            "[failed] could not move %s -> %s: %s",
            source_dir,
            target_dir,
            exc,
        )
        _record_rollback(
            rollback_log_path=rollback_log_path,
            from_id=from_id,
            to_id=to_id,
            project_ids=project_ids,
            status=status,
            dry_run=False,
        )
        return status

    status = "migrated"
    _log.info(
        "[rolled-back] %s -> %s (projects: %s)",
        source_dir,
        target_dir,
        project_ids,
    )
    _record_rollback(
        rollback_log_path=rollback_log_path,
        from_id=from_id,
        to_id=to_id,
        project_ids=project_ids,
        status=status,
        dry_run=False,
    )
    return status


def rollback(
    *,
    projects_dir: Path,
    audit_log_path: Path,
    rollback_log_path: Path,
    dry_run: bool,
    only_to: Optional[str] = None,
) -> dict:
    """Read the forward audit log and reverse each ``status=migrated`` line.

    Parameters mirror the forward script's ``migrate``:
    ``projects_dir`` defaults to ``lib.paths.PROJECTS_DIR`` so the
    test fixture can monkeypatch it. ``only_to`` filters to a single
    namespace_key (the forward log's ``to`` column) — useful for a
    recovery pass after a partial rollback.

    Returns
    -------
    dict
        ``{"migrated": int, "skipped": int, "failed": int, "dry_run": bool,
        "audit_log": str, "rollback_log": str}`` for the CLI summary.
    """
    users_dir = projects_dir / _USERS_BUCKET
    records = _read_audit(audit_log_path)

    summary = {"migrated": 0, "skipped": 0, "failed": 0}
    project_mappings = {
        (record.get("from"), record.get("to"))
        for record in records
        if isinstance(record, dict) and record.get("scope") == "project"
    }
    counted_mappings: set[tuple[str, str]] = set()
    for record in records:
        if record.get("__invalid_audit_record__"):
            summary["failed"] += 1
            continue
        status = record.get("status")
        from_id = record.get("from")
        to_id = record.get("to")
        if not isinstance(from_id, str) or not isinstance(to_id, str):
            _log.error("record missing from/to: %r", record)
            summary["failed"] += 1
            continue
        if only_to is not None and to_id != only_to:
            continue

        # Per-project records carry the exact entries that were moved. The
        # principal-level summary is evidence only when project records are
        # present; never fall back to moving an entire namespace root.
        if record.get("scope") != "project":
            if (from_id, to_id) in project_mappings:
                continue
            if status not in _ROLLBACKABLE_STATUSES:
                continue
            try:
                _safe_audit_ids(users_dir, from_id, to_id)
            except ValueError as exc:
                _log.error("invalid audit mapping %r -> %r: %s", from_id, to_id, exc)
                audit_status = "failed: invalid_audit_path"
            else:
                _log.error("cannot rollback principal record without project entries: %r -> %r", from_id, to_id)
                audit_status = "failed: missing_project_entries"
            _record_rollback(
                rollback_log_path=rollback_log_path,
                from_id=from_id,
                to_id=to_id,
                project_ids=[],
                status=audit_status,
                dry_run=dry_run,
            )
            summary["failed"] += 1
            continue

        if status not in {"migrated", "reconciled"}:
            _log.debug("[skip-non-rollbackable] project status=%r; skipping", status)
            continue
        project_id = record.get("project_id")
        try:
            _safe_audit_ids(users_dir, from_id, to_id)
            if not isinstance(project_id, str) or sanitize_project_id(project_id) != project_id:
                raise ValueError("audit record contains an invalid project id")
            details = record.get("details")
            moved_entries = _safe_relative_entries(
                details.get("moved") if isinstance(details, dict) else None
            )
        except ValueError as exc:
            _log.error("invalid project audit %r -> %r: %s", from_id, to_id, exc)
            summary["failed"] += 1
            _record_rollback(
                rollback_log_path=rollback_log_path,
                from_id=from_id,
                to_id=to_id,
                project_ids=[project_id] if isinstance(project_id, str) else [],
                status="failed: invalid_audit_path",
                dry_run=dry_run,
                project_id=project_id if isinstance(project_id, str) else None,
            )
            continue

        result = _rollback_project(
            users_dir=users_dir,
            from_id=to_id,
            to_id=from_id,
            project_id=project_id,
            moved_entries=moved_entries,
            dry_run=dry_run,
            rollback_log_path=rollback_log_path,
        )
        mapping = (from_id, to_id)
        if result.startswith("skipped"):
            summary["skipped"] += 1
        elif result == "failed":
            summary["failed"] += 1
        elif result in {"migrated", "dry-run"}:
            if mapping not in counted_mappings:
                summary["migrated"] += 1
                counted_mappings.add(mapping)
        else:
            summary["failed"] += 1
    summary["dry_run"] = dry_run
    summary["audit_log"] = str(audit_log_path)
    summary["rollback_log"] = str(rollback_log_path)
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    """CLI parser. Dry-run is the default; ``--apply`` flips the flag."""
    parser = argparse.ArgumentParser(
        description=(
            "Reverse a previous migrate_users_to_namespace_key.py run by "
            "reading its audit log and renaming projects/users/<ns_key>/ "
            "back to projects/users/<raw_openid>/."
        ),
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help=(
            "Print what would happen without moving anything "
            "(DEFAULT). The rollback audit log is still written "
            "with status=dry-run for traceability."
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
            "Forward audit log path. Defaults to "
            "<repo>/migrations/2026-09-02-namespace-key.jsonl."
        ),
    )
    parser.add_argument(
        "--rollback-log",
        type=Path,
        default=None,
        help=(
            "Rollback audit log path. Defaults to "
            "<repo>/migrations/2026-09-02-namespace-key.rollback.jsonl."
        ),
    )
    parser.add_argument(
        "--to",
        dest="only_to",
        default=None,
        help=(
            "Roll back only one principal — the forward log's "
            "<namespace_key> (the ``to`` column). Useful for a "
            "recovery pass after a partial rollback."
        ),
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

    projects_dir = (args.projects_dir or _lib_paths.PROJECTS_DIR).resolve()
    audit_log_path = (args.audit_log or (_REPO_ROOT / _DEFAULT_AUDIT_LOG)).resolve()
    rollback_log_path = (args.rollback_log or (_REPO_ROOT / _DEFAULT_ROLLBACK_LOG)).resolve()

    summary = rollback(
        projects_dir=projects_dir,
        audit_log_path=audit_log_path,
        rollback_log_path=rollback_log_path,
        dry_run=args.dry_run,
        only_to=args.only_to,
    )
    _log.info(
        "summary: rolled_back=%d skipped=%d failed=%d dry_run=%s "
        "audit_log=%s rollback_log=%s",
        summary["migrated"],
        summary["skipped"],
        summary["failed"],
        summary["dry_run"],
        summary["audit_log"],
        summary["rollback_log"],
    )
    return 1 if summary["failed"] > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
