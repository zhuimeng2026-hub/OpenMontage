package handlers

import (
	"testing"

	"frameflow-bff/internal/mcp"
)

func TestOwnedRenderJobRejectsAnotherSession(t *testing.T) {
	store := mcp.NewSessionStore("http://127.0.0.1:1", "")
	store.RecordJob("sid-owner", mcp.RenderJob{JobID: "job-1", Status: "已完成"})
	if ownedRenderJob(store, "sid-other", "job-1") != nil {
		t.Fatal("job from another session must not be found")
	}
}

func TestFinishRepublishBackfillsShareURL(t *testing.T) {
	store := mcp.NewSessionStore("http://127.0.0.1:1", "")
	store.RecordJob("sid", mcp.RenderJob{JobID: "job-1", Status: "已完成"})
	job := ownedRenderJob(store, "sid", "job-1")
	url, err := finishRepublish(store, "sid", job, map[string]interface{}{"share_url": "https://share.weiyun.com/test"})
	if err != nil || url != "https://share.weiyun.com/test" {
		t.Fatalf("finishRepublish() = %q, %v", url, err)
	}
	if got := ownedRenderJob(store, "sid", "job-1").ShareURL; got != url {
		t.Fatalf("share URL not persisted: %q", got)
	}
}

func TestRepublishEligibleStatusIncludesOrphanedFailure(t *testing.T) {
	if !republishEligibleStatus("失败") {
		t.Fatal("orphaned failed jobs must be eligible for republish")
	}
	if republishEligibleStatus("渲染中") || republishEligibleStatus("排队") {
		t.Fatal("in-flight jobs must not be eligible for republish")
	}
}

func TestFinishRepublishRejectsInvalidShareURL(t *testing.T) {
	store := mcp.NewSessionStore("http://127.0.0.1:1", "")
	job := &mcp.RenderJob{JobID: "job-1", Status: "已完成"}
	if _, err := finishRepublish(store, "sid", job, map[string]interface{}{"share_url": "javascript:alert(1)"}); err == nil {
		t.Fatal("invalid share URL must be rejected")
	}
}
