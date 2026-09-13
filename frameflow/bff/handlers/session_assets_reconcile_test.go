package handlers

import (
	"os"
	"path/filepath"
	"testing"
)

// TestReconcileAssetsDropsMissingFiles verifies that stale relative_path
// entries (pointing at files that no longer exist on disk) are filtered out
// and counted in stale_count. This is the regression test for the
// `create-video` 404 bug: session asset metadata referenced files that were
// dedup-removed or never written, and the SPA silently rendered broken <img>.
func TestReconcileAssetsDropsMissingFiles(t *testing.T) {
	dir := t.TempDir()
	good := filepath.Join(dir, "good.png")
	if err := os.WriteFile(good, []byte("PNG"), 0o644); err != nil {
		t.Fatalf("write good: %v", err)
	}
	assets := []map[string]interface{}{
		{"relative_path": "good.png", "filename": "good.png"},
		{"relative_path": "missing.png", "filename": "missing.png"},
		{"filename": "no-rel.png"}, // legacy: no relative_path ⇒ kept as-is
	}
	live, stale := reconcileAssets(assets, dir, "test-scope", "test-sid")
	if stale != 1 {
		t.Fatalf("expected stale=1, got %d", stale)
	}
	if len(live) != 2 {
		t.Fatalf("expected live=2, got %d", len(live))
	}
	found := false
	for _, a := range live {
		if rp, _ := a["relative_path"].(string); rp == "good.png" {
			found = true
		}
		if rp, _ := a["relative_path"].(string); rp == "missing.png" {
			t.Fatalf("missing.png should have been filtered out")
		}
	}
	if !found {
		t.Fatalf("good.png should have been kept")
	}
}

// TestReconcileAssetsDirectoryNotCountedAsFile ensures a directory at the
// relative_path is also treated as stale (ServeAsset refuses directories).
func TestReconcileAssetsDirectoryNotCountedAsFile(t *testing.T) {
	dir := t.TempDir()
	subdir := filepath.Join(dir, "a-dir")
	if err := os.MkdirAll(subdir, 0o755); err != nil {
		t.Fatalf("mkdir: %v", err)
	}
	assets := []map[string]interface{}{
		{"relative_path": "a-dir", "filename": "a-dir"},
	}
	live, stale := reconcileAssets(assets, dir, "scope", "sid")
	if stale != 1 || len(live) != 0 {
		t.Fatalf("directory entry should be stale; got live=%d stale=%d", len(live), stale)
	}
}

// TestReconcileAssetsEmptyInputIsNoop covers the freshly-created-session case.
func TestReconcileAssetsEmptyInputIsNoop(t *testing.T) {
	live, stale := reconcileAssets(nil, "/tmp", "scope", "sid")
	if stale != 0 || len(live) != 0 {
		t.Fatalf("empty input should yield empty output; got live=%d stale=%d", len(live), stale)
	}
}