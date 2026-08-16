package mcp

import (
	"testing"

	"frameflow-bff/internal/state"
)

func TestOwnsJobScopesBySession(t *testing.T) {
	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s := NewSessionStore("http://127.0.0.1:1", "", db)
	s.RecordJob("sid-a", RenderJob{JobID: "job-a"})
	if !s.OwnsJob("sid-a", "job-a") {
		t.Fatal("owner session should see its job")
	}
	if s.OwnsJob("sid-b", "job-a") {
		t.Fatal("different session must not see the job")
	}
}
