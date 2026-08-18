package handlers

import (
	"path/filepath"
	"testing"

	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
	"frameflow-bff/internal/state"

	_ "modernc.org/sqlite"
)

// newTestStore builds a Handlers wired to a real (temp-file) SQLite DB with the
// full schema applied. No upstream MCP is contacted by quotaRejectForUpload,
// so tests stay hermetic.
func newTestStore(t *testing.T) (*Handlers, func()) {
	t.Helper()
	dir := t.TempDir()
	db, err := state.Open(filepath.Join(dir, "test.db"))
	if err != nil {
		t.Fatalf("open test db: %v", err)
	}
	batches := imagebatch.NewStore(db)
	store := mcp.NewSessionStore("http://127.0.0.1:1/mcp", "tok", db)
	h := &Handlers{
		Store:        store,
		Limits:       limits.NewResolver("free", ""),
		ImageBatches: batches,
	}
	return h, func() { db.Close() }
}

// TestUploadQuotaBatchAwareDoesNotBlockReachingMinimum reproduces the reported
// deadlock: 5 images selected, 1 fails (4 committed), the user retries the
// failed one. The OLD code enforced the cap against a leaky session-wide
// counter that only resets on a successful render, so repeated attempts drove
// it to the tier cap and rejected the retry with 422 quota. The fix enforces
// against the ACTIVE batch's committed count, so the retry (4 -> 5) is allowed.
func TestUploadQuotaBatchAwareDoesNotBlockReachingMinimum(t *testing.T) {
	h, cleanup := newTestStore(t)
	defer cleanup()
	scope := renderQueueOwnerID("sid-deadlock")

	// Simulate a submission where 4 of 5 uploads already committed.
	b, err := h.ImageBatches.Create(scope, "batch-dead", "frameflow-batch-dead", "photo-ken-burns")
	if err != nil {
		t.Fatalf("create batch: %v", err)
	}
	if err := setBatchAssetCount(h, scope, b.ID, 4, "collecting"); err != nil {
		t.Fatalf("set asset count: %v", err)
	}

	// A retry of the failed 5th image must NOT be rejected.
	reject, _, _ := h.quotaRejectForUpload(scope, "frameflow-batch-dead")
	if reject {
		t.Fatalf("retry of failed image wrongly rejected at batch count 4 (should allow reaching 5)")
	}

	// Only at the true batch cap (10) is the upload rejected.
	if err := setBatchAssetCount(h, scope, b.ID, imagebatch.MaxBatchImages, "collecting"); err != nil {
		t.Fatalf("set asset count: %v", err)
	}
	reject, status, body := h.quotaRejectForUpload(scope, "frameflow-batch-dead")
	if !reject {
		t.Fatalf("upload at batch cap %d must be rejected", imagebatch.MaxBatchImages)
	}
	if status != 422 {
		t.Fatalf("expected 422, got %d", status)
	}
	if body["max"] != imagebatch.MaxBatchImages {
		t.Fatalf("expected max=%d in body, got %v", imagebatch.MaxBatchImages, body["max"])
	}
}

// TestUploadQuotaIgnoresLeakySessionCounter proves the regression: a stale,
// inflated session-wide counter (e.g. from prior abandoned/retried batches)
// no longer blocks a fresh batch's legitimate uploads.
func TestUploadQuotaIgnoresLeakySessionCounter(t *testing.T) {
	h, cleanup := newTestStore(t)
	defer cleanup()
	scope := renderQueueOwnerID("sid-leaky")

	// Leak the session counter up to the free tier cap (10) — the exact
	// condition that produced the 422 in production.
	for i := 0; i < limits.ForTier(limits.TierFree).MaxFilesPerSubmission; i++ {
		h.Store.IncAsset(scope)
	}
	if h.Store.AssetCount(scope) != limits.ForTier(limits.TierFree).MaxFilesPerSubmission {
		t.Fatalf("precondition: session counter not at cap")
	}

	// A brand-new batch with 0 committed images must still be allowed to
	// upload (the OLD code would have rejected this with 422).
	b, err := h.ImageBatches.Create(scope, "batch-fresh", "frameflow-batch-fresh", "photo-ken-burns")
	if err != nil {
		t.Fatalf("create batch: %v", err)
	}
	if err := setBatchAssetCount(h, scope, b.ID, 0, "collecting"); err != nil {
		t.Fatalf("set asset count: %v", err)
	}
	reject, _, _ := h.quotaRejectForUpload(scope, "frameflow-batch-fresh")
	if reject {
		t.Fatalf("fresh batch wrongly rejected despite leaky session counter (the reported 422 deadlock)")
	}
}

// TestUploadQuotaScriptModeFallback verifies the session-wide cap still applies
// for uploads that are not part of an image batch (script mode).
func TestUploadQuotaScriptModeFallback(t *testing.T) {
	h, cleanup := newTestStore(t)
	defer cleanup()
	scope := renderQueueOwnerID("sid-script")
	capN := limits.ForTier(limits.TierFree).MaxFilesPerSubmission

	reject, _, _ := h.quotaRejectForUpload(scope, "frameflow-default")
	if reject {
		t.Fatalf("empty session should be allowed to upload")
	}
	for i := 0; i < capN; i++ {
		h.Store.IncAsset(scope)
	}
	reject, st, _ := h.quotaRejectForUpload(scope, "frameflow-default")
	if !reject || st != 422 {
		t.Fatalf("script-mode upload at cap must be rejected with 422, got reject=%v status=%d", reject, st)
	}
}

// setBatchAssetCount updates a batch's committed asset count directly via the
// imagebatch store (there is no increment-by-N helper).
func setBatchAssetCount(h *Handlers, scope, batchID string, n int, status string) error {
	_, err := h.ImageBatches.Update(scope, batchID, func(b *imagebatch.Batch) {
		b.AssetCount = n
		b.Status = status
	})
	return err
}
