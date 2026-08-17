package state

import (
	"database/sql"
	"fmt"

	_ "modernc.org/sqlite"
)

func Open(path string) (*sql.DB, error) {
	db, err := sql.Open("sqlite", path)
	if err != nil {
		return nil, err
	}
	for _, pragma := range []string{
		"PRAGMA journal_mode=WAL",
		"PRAGMA busy_timeout=5000",
		"PRAGMA foreign_keys=ON",
	} {
		if _, err := db.Exec(pragma); err != nil {
			db.Close()
			return nil, fmt.Errorf("sqlite %s: %w", pragma, err)
		}
	}
	if _, err := db.Exec(`
CREATE TABLE IF NOT EXISTS image_batches (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  script_id TEXT NOT NULL,
  status TEXT NOT NULL,
  asset_count INTEGER NOT NULL DEFAULT 0,
  render_job_id TEXT NOT NULL DEFAULT '',
  video_url TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_batches_session ON image_batches(session_id, created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_image_batches_project ON image_batches(session_id, project_id);

CREATE TABLE IF NOT EXISTS mcp_batch_sessions (
  session_id TEXT NOT NULL,
  batch_id TEXT NOT NULL,
  project_id TEXT NOT NULL,
  upstream_session_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (session_id, batch_id),
  UNIQUE (session_id, project_id)
);
CREATE INDEX IF NOT EXISTS idx_mcp_batch_sessions_project ON mcp_batch_sessions(session_id, project_id);

-- One row per BFF session (ff_sid) holding the upstream MCP session id that
-- the browser's render loop depends on. Persisting it lets any BFF instance
-- (multi-instance deploy / restart) resume the SAME upstream Mcp-Session-Id
-- instead of opening a fresh upstream session and losing the uploaded assets.
CREATE TABLE IF NOT EXISTS mcp_user_sessions (
  session_id TEXT PRIMARY KEY,
  upstream_session_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcp_user_sessions_upstream ON mcp_user_sessions(upstream_session_id);

CREATE TABLE IF NOT EXISTS render_jobs (
  session_id TEXT NOT NULL,
  job_id TEXT NOT NULL,
  batch_id TEXT NOT NULL DEFAULT '',
  project_id TEXT NOT NULL DEFAULT '',
  name TEXT NOT NULL DEFAULT '',
  res TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT '',
  share_url TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  PRIMARY KEY (session_id, job_id)
);
CREATE INDEX IF NOT EXISTS idx_render_jobs_session ON render_jobs(session_id, created_at DESC);

CREATE TABLE IF NOT EXISTS image_batch_render_leases (
  batch_id TEXT PRIMARY KEY,
  user_id TEXT NOT NULL,
  owner_id TEXT NOT NULL,
  expires_at INTEGER NOT NULL,
  created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_image_batch_render_leases_expiry ON image_batch_render_leases(expires_at);
CREATE INDEX IF NOT EXISTS idx_image_batch_render_leases_user ON image_batch_render_leases(user_id, expires_at);

-- Durable WeChat login state. Each browser ff_sid cookie is bound to a WeChat
-- user profile here so a login survives a BFF restart and is visible to every
-- instance in a multi-instance deploy. The in-memory userStore is only a hot
-- cache; this table is the cross-instance source of truth.
CREATE TABLE IF NOT EXISTS wechat_users (
  ff_sid TEXT PRIMARY KEY,
  openid TEXT NOT NULL DEFAULT '',
  nickname TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '',
  profile_json TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wechat_users_openid ON wechat_users(openid);
CREATE INDEX IF NOT EXISTS idx_wechat_users_expiry ON wechat_users(expires_at);

-- Durable WeChat QR-login tickets. A desktop scan authorizes a ticket on one
-- BFF instance (the phone's WeChat browser hits the callback), while the PC
-- polls the ticket status on another instance. Persisting tickets here makes
-- the authorized state visible to every instance in a multi-instance deploy,
-- so a scan is not lost when the callback and the poll land on different pods.
-- The in-memory qrTickets map is only a hot cache; this table is the
-- cross-instance source of truth. Requires a shared DB volume across instances.
CREATE TABLE IF NOT EXISTS wechat_qr_tickets (
  ticket_id TEXT PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'pending',
  profile_json TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_wechat_qr_tickets_expiry ON wechat_qr_tickets(expires_at);

	`); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite schema: %w", err)
	}
	// Backward-compatible migration for databases created before share URLs
	// were persisted with render jobs.
	var hasShareURL bool
	rows, err := db.Query(`PRAGMA table_info(render_jobs)`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite render_jobs schema: %w", err)
	}
	for rows.Next() {
		var cid int
		var name, typ string
		var notnull, pk int
		var dflt interface{}
		if err := rows.Scan(&cid, &name, &typ, &notnull, &dflt, &pk); err != nil {
			rows.Close()
			db.Close()
			return nil, err
		}
		if name == "share_url" {
			hasShareURL = true
		}
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		db.Close()
		return nil, fmt.Errorf("sqlite render_jobs schema rows: %w", err)
	}
	rows.Close()
	if !hasShareURL {
		if _, err := db.Exec(`ALTER TABLE render_jobs ADD COLUMN share_url TEXT NOT NULL DEFAULT ''`); err != nil {
			db.Close()
			return nil, fmt.Errorf("sqlite render_jobs share_url migration: %w", err)
		}
	}
	// Backfill links for historical image-batch jobs created before render_jobs
	// persisted share URLs.
	if _, err := db.Exec(`UPDATE render_jobs SET share_url=(SELECT video_url FROM image_batches b WHERE b.session_id=render_jobs.session_id AND b.render_job_id=render_jobs.job_id) WHERE share_url='' AND EXISTS (SELECT 1 FROM image_batches b WHERE b.session_id=render_jobs.session_id AND b.render_job_id=render_jobs.job_id AND b.video_url<>'')`); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite render_jobs share_url backfill: %w", err)
	}
	// A restart can leave a batch in rendering state while the upstream job is
	// still running. Keep it recoverable; the handler lazily recreates its MCP
	// client from the durable batch metadata on the next request.
	return db, nil
}
