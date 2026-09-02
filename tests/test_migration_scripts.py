"""Phase D scripts: migrate + rollback + backup."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"scripts.{name}", SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ns_key_for(raw_openid: str) -> str:
    import sys
    sys.path.insert(0, str(ROOT))
    from lib.principal_registry import compute_namespace_key
    return compute_namespace_key(raw_openid)


def _seed_legacy_user(projects_dir: Path, raw_openid: str, project_ids: list) -> None:
    user_dir = projects_dir / "users" / raw_openid
    user_dir.mkdir(parents=True)
    for pid in project_ids:
        proj = user_dir / pid / "assets"
        proj.mkdir(parents=True)
        (proj / "image.jpg").write_bytes(b"data-" + pid.encode())


def test_migrate_dry_run_does_not_move(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    projects = tmp_path / "projects"
    _seed_legacy_user(projects, "raw_openid_xyz", ["proj1", "proj2"])
    audit = tmp_path / "audit.jsonl"
    summary = mig.migrate(projects_dir=projects, dry_run=True, audit_log_path=audit)
    assert summary["migrated"] == 1
    assert (projects / "users" / "raw_openid_xyz" / "proj1" / "assets" / "image.jpg").is_file()
    assert not (projects / "users" / _ns_key_for("raw_openid_xyz")).exists()
    lines = [json.loads(line) for line in audit.read_text().splitlines() if line]
    assert any(line["from"] == "raw_openid_xyz" and line["status"] == "dry-run" for line in lines)


def test_migrate_real_moves_dirs_and_writes_audit(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    projects = tmp_path / "projects"
    _seed_legacy_user(projects, "raw_openid_xyz", ["proj1"])
    audit = tmp_path / "audit.jsonl"
    summary = mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=audit)
    assert summary["migrated"] == 1
    assert not (projects / "users" / "raw_openid_xyz").exists()
    target = projects / "users" / _ns_key_for("raw_openid_xyz") / "proj1" / "assets" / "image.jpg"
    assert target.is_file()
    lines = [json.loads(line) for line in audit.read_text().splitlines() if line]
    assert any(line["from"] == "raw_openid_xyz" and line["to"] == _ns_key_for("raw_openid_xyz") and line["status"] == "migrated" for line in lines)


def test_migrate_reconciles_non_conflicting_existing_target(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    projects = tmp_path / "projects"
    _seed_legacy_user(projects, "raw_openid_xyz", ["proj1"])
    v2_dir = projects / "users" / _ns_key_for("raw_openid_xyz") / "proj1"
    v2_dir.mkdir(parents=True)
    (v2_dir / "different.txt").write_bytes(b"v2")
    audit = tmp_path / "audit.jsonl"
    mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=audit)
    # Existing target projects are reconciled entry-by-entry: disjoint
    # source files move, while target files are never overwritten.
    assert not (projects / "users" / "raw_openid_xyz").exists()
    assert (v2_dir / "assets" / "image.jpg").is_file()
    assert (v2_dir / "different.txt").read_bytes() == b"v2"
    lines = [json.loads(line) for line in audit.read_text().splitlines() if line]
    assert any(line["status"] == "reconciled" for line in lines)


def test_migrate_leaves_conflicting_project_and_audits_paths(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    projects = tmp_path / "projects"
    _seed_legacy_user(projects, "raw_openid_xyz", ["proj1", "proj2"])
    target = projects / "users" / _ns_key_for("raw_openid_xyz") / "proj1" / "assets"
    target.mkdir(parents=True)
    (target / "image.jpg").write_bytes(b"different")
    audit = tmp_path / "audit.jsonl"

    summary = mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=audit)

    assert summary["conflicts"] == 1
    assert (projects / "users" / "raw_openid_xyz" / "proj1" / "assets" / "image.jpg").is_file()
    assert (projects / "users" / _ns_key_for("raw_openid_xyz") / "proj2" / "assets" / "image.jpg").is_file()
    records = [json.loads(line) for line in audit.read_text().splitlines() if line]
    conflict = next(record for record in records if record.get("scope") == "project" and record["project_id"] == "proj1")
    assert conflict["status"] == "conflict"
    assert "assets/image.jpg" in conflict["details"]["conflict_paths"]


def test_migrate_does_not_guess_32hex_directory_is_v2(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    projects = tmp_path / "projects"
    raw_hex = "a" * 32
    _seed_legacy_user(projects, raw_hex, ["proj1"])
    audit = tmp_path / "audit.jsonl"

    summary = mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=audit)

    assert summary["skipped"] == 1
    assert (projects / "users" / raw_hex / "proj1").is_dir()
    records = [json.loads(line) for line in audit.read_text().splitlines() if line]
    assert records[-1]["status"] == "skipped: ambiguous_32hex"


def test_rollback_reverses_migration(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    rb = _load("rollback_namespace_key")
    projects = tmp_path / "projects"
    _seed_legacy_user(projects, "raw_openid_xyz", ["proj1", "proj2"])
    forward_audit = tmp_path / "forward.jsonl"
    mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=forward_audit)
    assert (projects / "users" / _ns_key_for("raw_openid_xyz") / "proj1" / "assets" / "image.jpg").is_file()
    rb_audit = tmp_path / "rollback.jsonl"
    summary = rb.rollback(
        projects_dir=projects,
        audit_log_path=forward_audit,
        rollback_log_path=rb_audit,
        dry_run=False,
    )
    assert summary["migrated"] == 1
    assert (projects / "users" / "raw_openid_xyz" / "proj1" / "assets" / "image.jpg").is_file()
    assert not (projects / "users" / _ns_key_for("raw_openid_xyz") / "proj1").exists()
    rb_lines = [json.loads(line) for line in rb_audit.read_text().splitlines() if line]
    assert any(line["from"] == _ns_key_for("raw_openid_xyz") and line["to"] == "raw_openid_xyz" for line in rb_lines)


def test_rollback_rejects_tampered_audit_path(tmp_path: Path) -> None:
    rb = _load("rollback_namespace_key")
    projects = tmp_path / "projects"
    users = projects / "users"
    users.mkdir(parents=True)
    audit = tmp_path / "forward.jsonl"
    audit.write_text(json.dumps({
        "from": "..\\outside", "to": _ns_key_for("raw_openid_xyz"),
        "project_ids": [], "status": "migrated",
    }) + "\n", encoding="utf-8")
    rollback_log = tmp_path / "rollback.jsonl"

    summary = rb.rollback(projects_dir=projects, audit_log_path=audit,
                          rollback_log_path=rollback_log, dry_run=False)

    assert summary["failed"] == 1
    assert not (tmp_path / "outside").exists()
    record = json.loads(rollback_log.read_text().splitlines()[0])
    assert record["status"] == "failed: invalid_audit_path"


def test_rollback_reverses_only_reconciled_entries(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    rb = _load("rollback_namespace_key")
    projects = tmp_path / "projects"
    _seed_legacy_user(projects, "raw_openid_xyz", ["proj1"])
    key = _ns_key_for("raw_openid_xyz")
    target = projects / "users" / key / "proj1"
    target.mkdir(parents=True)
    (target / "new-v2.txt").write_text("keep", encoding="utf-8")
    forward = tmp_path / "forward.jsonl"
    mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=forward)
    rollback_log = tmp_path / "rollback.jsonl"
    summary = rb.rollback(
        projects_dir=projects, audit_log_path=forward,
        rollback_log_path=rollback_log, dry_run=False,
    )
    assert summary["migrated"] == 1
    assert (projects / "users" / "raw_openid_xyz" / "proj1" / "assets" / "image.jpg").is_file()
    assert (projects / "users" / key / "proj1" / "new-v2.txt").read_text() == "keep"


def test_rollback_counts_non_object_audit_rows_as_failures(tmp_path: Path) -> None:
    rb = _load("rollback_namespace_key")
    projects = tmp_path / "projects"
    (projects / "users").mkdir(parents=True)
    forward = tmp_path / "forward.jsonl"
    forward.write_text("[]\n", encoding="utf-8")
    summary = rb.rollback(
        projects_dir=projects, audit_log_path=forward,
        rollback_log_path=tmp_path / "rollback.jsonl", dry_run=True,
    )
    assert summary["failed"] == 1


def test_migrate_empty_legacy_principal_is_skipped(tmp_path: Path) -> None:
    mig = _load("migrate_users_to_namespace_key")
    projects = tmp_path / "projects"
    (projects / "users" / "empty-user").mkdir(parents=True)
    audit = tmp_path / "audit.jsonl"

    summary = mig.migrate(projects_dir=projects, dry_run=False, audit_log_path=audit)

    assert summary["failed"] == 0
    assert summary["skipped"] == 1
    assert json.loads(audit.read_text().splitlines()[0])["status"] == "skipped: empty_source"


def test_backup_copies_to_timestamped_dir(tmp_path: Path) -> None:
    backup = _load("backup_mcp_sessions")
    sessions = tmp_path / "projects" / ".mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "session_a.json").write_text("{}")
    (sessions / "session_b.json").write_text("{}")
    backups_root = tmp_path / "backups"
    rc = backup.run(["--source", str(sessions), "--dest-root", str(backups_root), "--label", "pre-migration"])
    assert rc == 0
    matches = list(backups_root.glob("*-pre-migration"))
    assert len(matches) == 1
    assert (matches[0] / "session_a.json").is_file()
    assert (matches[0] / "session_b.json").is_file()


def test_backup_refuses_destination_exists(tmp_path: Path) -> None:
    backup = _load("backup_mcp_sessions")
    sessions = tmp_path / "projects" / ".mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "x.json").write_text("{}")
    backups_root = tmp_path / "backups"
    rc = backup.run(["--source", str(sessions), "--dest-root", str(backups_root), "--label", "first"])
    assert rc == 0
    rc2 = backup.run(["--source", str(sessions), "--dest-root", str(backups_root), "--label", "first"])
    assert rc2 == 3


def test_backup_dry_run(tmp_path: Path) -> None:
    backup = _load("backup_mcp_sessions")
    sessions = tmp_path / "projects" / ".mcp_sessions"
    sessions.mkdir(parents=True)
    (sessions / "x.json").write_text("{}")
    backups_root = tmp_path / "backups"
    rc = backup.run(["--source", str(sessions), "--dest-root", str(backups_root), "--dry-run"])
    assert rc == 0
    assert not backups_root.exists()
