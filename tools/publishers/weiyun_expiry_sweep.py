"""Weiyun share-link expiry sweeper.

Reads ``projects/_share_expiry/index.jsonl`` (one row per share link that was
registered with ``retain_days``), and for any row where ``expires_at`` has
elapsed, calls ``WeiyunDelete`` with the captured ``file_ids`` and
``pdir_keys``. Marking rows ``deleted_at`` makes them idempotent — running the
sweeper twice does not double-delete.

Why this exists: the upstream Tencent Weiyun MCP ``gen_share_link`` tool has
no expiration parameter (verified by querying ``tools/list``). Deleting the
underlying file is the only way to invalidate a share URL from the client
side.

CLI usage::

    # dry-run, only show what would be deleted
    python -m tools.publishers.weiyun_expiry_sweep --dry-run

    # actually delete (move to Weiyun trash by default)
    python -m tools.publishers.weiyun_expiry_sweep

    # hard delete (irreversible — moves past trash)
    python -m tools.publishers.weiyun_expiry_sweep --completely

    # cap how many rows we touch per run (defensive)
    python -m tools.publishers.weiyun_expiry_sweep --limit 50

    # sweep only rows for a given project
    python -m tools.publishers.weiyun_expiry_sweep --project-id frameflow-default

Exit code is 0 on success (including "nothing to do"), 1 if any row failed to
delete (partial sweep; subsequent runs pick up the rest).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

# Reuse the same path the share_link tool writes to.
SHARE_EXPIRY_DIR = Path(__file__).resolve().parents[2] / "projects" / "_share_expiry"
SHARE_EXPIRY_INDEX = SHARE_EXPIRY_DIR / "index.jsonl"


def _iter_rows() -> Iterable[dict[str, Any]]:
    if not SHARE_EXPIRY_INDEX.is_file():
        return
    with SHARE_EXPIRY_INDEX.open("r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                yield {"_lineno": lineno, **_json(raw)}
            except json.JSONDecodeError:
                # Skip malformed rows; log to stderr.
                print(f"[warn] skipping malformed line {lineno}: {raw[:80]}", file=sys.stderr)


def _json(s: str) -> dict[str, Any]:
    return json.loads(s)


def _parse_iso(ts: str) -> datetime | None:
    if not ts:
        return None
    try:
        # Python 3.10 fromisoformat doesn't accept the trailing 'Z' until 3.11.
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def sweep(
    *,
    dry_run: bool,
    delete_completely: bool,
    limit: int | None,
    project_id: str | None,
) -> int:
    """Run one sweep pass. Returns process exit code."""
    if not SHARE_EXPIRY_INDEX.is_file():
        print(f"no index at {SHARE_EXPIRY_INDEX}; nothing to do")
        return 0

    now = datetime.now(timezone.utc)
    pending: list[dict[str, Any]] = []

    for row in _iter_rows():
        if row.get("status") == "deleted" or row.get("deleted_at"):
            continue
        if project_id and row.get("project_id") != project_id:
            continue
        expires_at = _parse_iso(row.get("expires_at", ""))
        if expires_at is None:
            continue
        if expires_at > now:
            continue
        pending.append(row)

    if not pending:
        print("no expired shares to sweep")
        return 0

    if limit is not None:
        pending = pending[:limit]

    print(f"sweeping {len(pending)} expired share(s)"
          + (" (dry-run, no deletes)" if dry_run else ""))

    # Late import — only need the tool when actually deleting.
    delete_tool = None
    if not dry_run:
        from .weiyun_delete import WeiyunDelete
        delete_tool = WeiyunDelete()

    failed = 0
    rows = SHARE_EXPIRY_INDEX.read_text(encoding="utf-8").splitlines()
    for row in pending:
        lineno = row["_lineno"]
        short_url = row.get("short_url", "?")
        file_ids = row.get("file_ids") or []
        pdir_keys = row.get("pdir_keys") or []
        if not file_ids:
            print(f"  [skip] {short_url} — no file_ids recorded")
            continue
        if not pdir_keys:
            print(f"  [skip] {short_url} — no pdir_key recorded; "
                  "rerun share_link with pdir_key to enable expiry")
            continue

        if dry_run:
            print(f"  [dry-run] would delete file_id={file_ids[0]} pdir_key={pdir_keys[0]} "
                  f"(share={short_url})")
            continue

        result = delete_tool.execute({
            "file_list": [{"file_id": fid, "pdir_key": pdir_keys[0]} for fid in file_ids],
            "delete_completely": delete_completely,
        })

        if not result.success:
            failed += 1
            print(f"  [FAIL] {short_url} — {result.error}")
            continue

        freed = (result.data or {}).get("freed_index_cnt", 0)
        print(f"  [OK]   {short_url} — deleted {freed} file(s)")

        # Mark the row as deleted by rewriting the line in place.
        updated = dict(row)
        updated["status"] = "deleted"
        updated["deleted_at"] = now.isoformat()
        updated.pop("_lineno", None)
        rows[lineno - 1] = json.dumps(updated, ensure_ascii=False)

    if not dry_run:
        SHARE_EXPIRY_INDEX.write_text("\n".join(rows) + "\n", encoding="utf-8")

    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Delete Weiyun files backing expired share links."
    )
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be deleted; do not call weiyun.delete.")
    p.add_argument("--completely", action="store_true",
                   help="Pass delete_completely=true to weiyun.delete "
                        "(irreversible). Default: move to Weiyun trash.")
    p.add_argument("--limit", type=int, default=None,
                   help="Cap rows touched this run.")
    p.add_argument("--project-id", default=None,
                   help="Only sweep rows for this project_id.")
    args = p.parse_args(argv)

    return sweep(
        dry_run=args.dry_run,
        delete_completely=args.completely,
        limit=args.limit,
        project_id=args.project_id,
    )


if __name__ == "__main__":
    sys.exit(main())