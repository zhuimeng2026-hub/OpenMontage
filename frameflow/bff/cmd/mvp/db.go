package main

import (
	"database/sql"
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
	return db
}
