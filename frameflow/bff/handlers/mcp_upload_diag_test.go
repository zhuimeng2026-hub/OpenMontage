package handlers

import (
	"strings"
	"testing"
)

func TestUploadArgsSummaryDoesNotLogFilename(t *testing.T) {
	summary := uploadArgsSummary(map[string]interface{}{
		"filename":    "商品主图.png",
		"total_bytes": float64(4654885),
		"offset":      float64(2000000),
		"upload_id":   "upload-id",
	})
	for _, want := range []string{
		"filename_safe=false",
		"extension=\".png\"",
		"total_bytes=4654885",
		"offset=2000000",
		"upload_id_present=true",
	} {
		if !strings.Contains(summary, want) {
			t.Fatalf("summary missing %q: %s", want, summary)
		}
	}
	if strings.Contains(summary, "商品主图") {
		t.Fatalf("summary leaked original filename: %s", summary)
	}
}

func TestUploadArgsSummaryRecognizesSafeFilename(t *testing.T) {
	summary := uploadArgsSummary(map[string]interface{}{
		"filename": "product-01.jpg",
	})
	if !strings.Contains(summary, "filename_safe=true") {
		t.Fatalf("expected safe filename: %s", summary)
	}
	if !strings.Contains(summary, "total_bytes=-") || !strings.Contains(summary, "offset=-") {
		t.Fatalf("expected absent numeric fields to be marked unknown: %s", summary)
	}
}
