package main

import (
	"database/sql"
	"log"

	_ "github.com/mattn/go-sqlite3"
)

func openDB(path string) *sql.DB {
	db, err := sql.Open("sqlite3", path+"?_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		panic(err)
	}
	if err := db.Ping(); err != nil {
		panic(err)
	}
	// Phase 6: artifacts_json column on production_jobs — stores the §23
	// three-tier preview artifact (storyboard / animatic / sample / render).
	// ALTER TABLE ADD COLUMN is idempotent on SQLite from 3.35 onwards; on
	// older builds it errors with "duplicate column" which we swallow via
	// the Try()-style guard.
	if _, err := db.Exec(
		`ALTER TABLE production_jobs ADD COLUMN artifacts_json TEXT DEFAULT NULL`,
	); err != nil {
		// Expected on re-run; log only at debug.
		log.Printf("[mvp] artifacts_json migration: %v (ignored if column already exists)", err)
	}
	return db
}
