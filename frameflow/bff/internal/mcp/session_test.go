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

func TestRenderJobShareURLIsSessionAndJobScopedAndDurable(t *testing.T) {
	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s := NewSessionStore("http://127.0.0.1:1", "", db)
	s.RecordJob("sid-a", RenderJob{JobID: "job-a", Status: "排队"})
	s.RecordJob("sid-a", RenderJob{JobID: "job-b", Status: "排队"})
	s.RecordJob("sid-b", RenderJob{JobID: "job-a", Status: "排队"})
	s.UpdateJobResult("sid-a", "job-a", "已完成", "https://share.weiyun.com/a")
	s.UpdateJobResult("sid-b", "job-a", "已完成", "https://share.weiyun.com/b")
	a := s.ListJobs("sid-a")
	if len(a) != 2 {
		t.Fatalf("expected two jobs, got %d", len(a))
	}
	for _, j := range a {
		if j.JobID == "job-a" && j.ShareURL != "https://share.weiyun.com/a" {
			t.Fatalf("sid-a link mixed: %#v", j)
		}
		if j.JobID == "job-b" && j.ShareURL != "" {
			t.Fatalf("unpublished job got link: %#v", j)
		}
	}
	b := s.ListJobs("sid-b")
	if len(b) != 1 || b[0].ShareURL != "https://share.weiyun.com/b" {
		t.Fatalf("sid-b link mixed: %#v", b)
	}
}

func TestRecordJobDoesNotEraseExistingShareURL(t *testing.T) {
	db, err := state.Open(t.TempDir() + "/state.db")
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	s := NewSessionStore("http://127.0.0.1:1", "", db)
	s.RecordJob("sid", RenderJob{JobID: "job", Status: "排队", ShareURL: "https://share.weiyun.com/keep"})
	s.RecordJob("sid", RenderJob{JobID: "job", Status: "已完成"})
	j := s.ListJobs("sid")
	if len(j) != 1 || j[0].ShareURL != "https://share.weiyun.com/keep" {
		t.Fatalf("share URL lost on upsert: %#v", j)
	}
}
