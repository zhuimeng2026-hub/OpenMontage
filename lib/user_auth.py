"""Small, dependency-light identity and user-scope store for the web UI.

The MCP transport still uses ``Mcp-Session-Id`` for backwards compatibility.
This module adds a durable account boundary for browser users without treating
that transport session id as an account identifier.
"""

from __future__ import annotations

import hashlib
from contextlib import contextmanager
import base64
import binascii
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any


_SAFE_PROJECT = re.compile(r"[^A-Za-z0-9._-]+")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif", ".mp4", ".webm", ".mov", ".m4v", ".mp3", ".wav", ".m4a", ".aac", ".ogg"}


def _now() -> int:
    return int(time.time())


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_id(provider: str, subject: str) -> str:
    return "u_" + _hash(f"{provider}:{subject}")[:24]


class UserAuthStore:
    """SQLite-backed users, browser sessions, and one-time OAuth state."""

    def __init__(self, db_path: Path, projects_root: Path):
        self.db_path = Path(db_path)
        self.projects_root = Path(projects_root)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._init_db()

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            yield conn
        except Exception:
            conn.rollback()
            raise
        else:
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    profile_json TEXT NOT NULL DEFAULT '{}',
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE(provider, subject)
                );
                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id),
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_web_sessions_expiry ON web_sessions(expires_at);
                CREATE TABLE IF NOT EXISTS oauth_states (
                    state_hash TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    return_to TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL
                );
                """
            )

    def create_oauth_state(self, provider: str, return_to: str, ttl: int = 600) -> str:
        state = secrets.token_urlsafe(32)
        now = _now()
        with self._connection() as conn:
            conn.execute("DELETE FROM oauth_states WHERE expires_at < ?", (now,))
            conn.execute(
                "INSERT INTO oauth_states(state_hash, provider, return_to, expires_at, created_at) VALUES (?, ?, ?, ?, ?)",
                (_hash(state), provider, return_to, now + ttl, now),
            )
        return state

    def consume_oauth_state(self, state: str, provider: str) -> str | None:
        now = _now()
        with self._connection() as conn:
            row = conn.execute(
                "SELECT return_to FROM oauth_states WHERE state_hash = ? AND provider = ? AND expires_at >= ?",
                (_hash(state), provider, now),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM oauth_states WHERE state_hash = ?", (_hash(state),))
            return str(row["return_to"])

    def upsert_user(self, provider: str, subject: str, display_name: str = "", profile: dict[str, Any] | None = None) -> dict[str, Any]:
        uid = _user_id(provider, subject)
        now = _now()
        import json

        profile_json = json.dumps(profile or {}, ensure_ascii=False, separators=(",", ":"))
        with self._connection() as conn:
            conn.execute(
                """INSERT INTO users(id, provider, subject, display_name, profile_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(provider, subject) DO UPDATE SET display_name=excluded.display_name,
                   profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
                (uid, provider, subject, display_name[:200], profile_json, now, now),
            )
        return self.get_user(uid) or {"id": uid, "provider": provider, "display_name": display_name}

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT id, provider, subject, display_name, profile_json, created_at, updated_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        import json

        result = dict(row)
        result["profile"] = json.loads(result.pop("profile_json") or "{}")
        result.pop("subject", None)
        return result

    def create_session(self, user_id: str, ttl: int = 7 * 24 * 3600) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        expires = _now() + ttl
        with self._connection() as conn:
            conn.execute("DELETE FROM web_sessions WHERE expires_at < ?", (_now(),))
            conn.execute(
                "INSERT INTO web_sessions(token_hash, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (_hash(token), user_id, expires, _now()),
            )
        return token, expires

    def user_for_session(self, token: str | None) -> dict[str, Any] | None:
        if not token:
            return None
        with self._connection() as conn:
            row = conn.execute(
                "SELECT user_id FROM web_sessions WHERE token_hash = ? AND expires_at >= ?",
                (_hash(token), _now()),
            ).fetchone()
        return self.get_user(str(row["user_id"])) if row else None

    def delete_session(self, token: str | None) -> None:
        if token:
            with self._connection() as conn:
                conn.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_hash(token),))

    def user_projects_root(self, user_id: str) -> Path:
        path = (self.projects_root / "users" / user_id).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    def project_path(self, user_id: str, project_id: str) -> Path:
        clean = _SAFE_PROJECT.sub("-", project_id).strip(".-")[:100]
        if not clean:
            raise ValueError("project_id must contain letters, numbers, '.', '_' or '-'")
        root = self.user_projects_root(user_id)
        path = (root / clean).resolve()
        path.relative_to(root)
        return path

    def ensure_project(self, user_id: str, project_id: str) -> Path:
        project = self.project_path(user_id, project_id)
        for child in ("assets", "renders", "artifacts"):
            (project / child).mkdir(parents=True, exist_ok=True)
        marker = project / "project.json"
        if not marker.exists():
            import json

            marker.write_text(
                json.dumps({"project_id": project.name, "owner_user_id": user_id, "created_at": _now()}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return project

    def list_projects(self, user_id: str) -> list[dict[str, Any]]:
        root = self.user_projects_root(user_id)
        result = []
        for item in sorted(root.iterdir()):
            if not item.is_dir() or item.name.startswith("."):
                continue
            result.append({"project_id": item.name, "has_assets": (item / "assets").exists(), "has_renders": (item / "renders").exists()})
        return result

    def save_asset(self, user_id: str, project_id: str, filename: str, content_base64: str) -> dict[str, Any]:
        if not isinstance(filename, str) or not _SAFE_FILENAME.fullmatch(filename):
            raise ValueError("filename must be a safe basename")
        suffix = Path(filename).suffix.lower()
        if suffix not in _ALLOWED_EXTENSIONS:
            raise ValueError(f"unsupported asset extension: {suffix or '(none)'}")
        try:
            content = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("content_base64 is not valid base64") from exc
        try:
            max_mb = max(1, int(os.environ.get("OPENMONTAGE_MAX_UPLOAD_MB", "100")))
        except ValueError:
            max_mb = 100
        max_bytes = max_mb * 1024 * 1024
        if len(content) > max_bytes:
            raise ValueError(f"asset exceeds {max_bytes // (1024 * 1024)} MB limit")
        project = self.ensure_project(user_id, project_id)
        digest = hashlib.sha256(content).hexdigest()
        target = project / "assets" / f"{digest[:12]}-{filename}"
        target.write_bytes(content)
        return {"id": f"{project.name}-{digest[:12]}", "filename": filename, "path": str(target), "relative_path": target.relative_to(self.projects_root).as_posix(), "bytes": len(content), "sha256": digest}


def default_user_store(project_root: Path) -> UserAuthStore:
    return UserAuthStore(project_root / "projects" / ".users" / "users.sqlite3", project_root / "projects")
