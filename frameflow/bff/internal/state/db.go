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

`); err != nil {
		db.Close()
		return nil, fmt.Errorf("sqlite schema: %w", err)
	}
	// A restart can leave a batch in rendering state while the upstream job is
	// still running. Keep it recoverable; the handler lazily recreates its MCP
	// client from the durable batch metadata on the next request.
	return db, nil
}
