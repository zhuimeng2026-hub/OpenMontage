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

func TestSafeUploadFilenameRenamesUnsafeNameAndPreservesExtension(t *testing.T) {
	safe, renamed := safeUploadFilename("商品主图.png")
	if !renamed {
		t.Fatal("expected unsafe filename to be renamed")
	}
	if !uploadFilenamePattern.MatchString(safe) {
		t.Fatalf("renamed filename is not safe: %q", safe)
	}
	if !strings.HasSuffix(safe, ".png") {
		t.Fatalf("expected extension to be preserved: %q", safe)
	}
	if safe == "商品主图.png" {
		t.Fatalf("filename was not changed: %q", safe)
	}
}

func TestSafeUploadFilenameIsStableAcrossChunks(t *testing.T) {
	first, firstRenamed := safeUploadFilename(`C:\\用户资料\\商品 主图.PNG`)
	second, secondRenamed := safeUploadFilename(`C:\\用户资料\\商品 主图.PNG`)
	if !firstRenamed || !secondRenamed || first != second {
		t.Fatalf("expected deterministic filename, got %q/%q", first, second)
	}
	if !strings.HasSuffix(first, ".png") {
		t.Fatalf("expected normalized extension: %q", first)
	}
}

func TestSafeUploadFilenameRetainsReadableASCIIStem(t *testing.T) {
	safe, renamed := safeUploadFilename("Product 01 商品.png")
	if !renamed || !strings.Contains(safe, "Product_01") {
		t.Fatalf("expected readable ASCII stem, got %q", safe)
	}
	if !uploadFilenamePattern.MatchString(safe) {
		t.Fatalf("renamed filename is not safe: %q", safe)
	}
}
