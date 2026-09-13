"""Backup ``projects/.mcp_sessions/`` to a timestamped directory.

Why
---
Phase D runs a one-shot data migration that physically renames
``projects/users/<raw_openid>/`` → ``projects/users/<namespace_key>/``.
The legacy ``.mcp_sessions/`` directory holds per-MCP-session JSON state
that backs ``workbuddy_session.py``; its loss is unrecoverable for any
in-flight job. Before any destructive operation, run this script.

Behaviour
---------
* Default destination: ``backups/mcp_sessions/<UTC-timestamp>/``.
* Uses ``shutil.copytree(..., dirs_exist_ok=False)`` so a re-run with the
  same timestamp raises ``FileExistsError`` instead of silently overwriting.
* `--dry-run` prints the planned source/destination without copying.
* `--source <path>` overrides the default ``projects/.mcp_sessions/``
  (test harness).

Exit codes
----------
0   backup succeeded
2   source directory missing
3   destination already exists
5   partial copy (copytree raised mid-flight)
"""
from __future__ import annotations

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from lib import paths as lib_paths

DEFAULT_SOURCE = lib_paths.PROJECTS_DIR / ".mcp_sessions"
DEFAULT_DEST_ROOT = REPO_ROOT / "backups" / "mcp_sessions"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--source", type=Path, default=DEFAULT_SOURCE,
        help=f"mcp_sessions source (default: {DEFAULT_SOURCE})",
    )
    parser.add_argument(
        "--dest-root", type=Path, default=DEFAULT_DEST_ROOT,
        help=f"backup root (default: {DEFAULT_DEST_ROOT})",
    )
    parser.add_argument(
        "--label", default=None,
        help="optional suffix for the timestamped directory (e.g. 'pre-migration')",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="print plan without copying",
    )
    return parser.parse_args(argv)


def build_destination(dest_root: Path, label: str | None) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = f"-{label}" if label else ""
    return dest_root / f"{stamp}{suffix}"


def run(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = args.source.resolve()
    dest = build_destination(args.dest_root, args.label).resolve()

    if not source.is_dir():
        print(f"backup_mcp_sessions: source missing: {source}", file=sys.stderr)
        return 2
    if dest.exists():
        print(f"backup_mcp_sessions: destination already exists: {dest}", file=sys.stderr)
        return 3

    print(f"backup_mcp_sessions: source  = {source}")
    print(f"backup_mcp_sessions: dest    = {dest}")
    if args.dry_run:
        print("backup_mcp_sessions: --dry-run; not copying")
        return 0

    try:
        shutil.copytree(source, dest)
    except OSError as exc:
        print(f"backup_mcp_sessions: copy failed: {exc}", file=sys.stderr)
        return 5

    print(f"backup_mcp_sessions: copied to {dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
