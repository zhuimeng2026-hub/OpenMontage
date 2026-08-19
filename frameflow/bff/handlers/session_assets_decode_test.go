package handlers

import (
	"encoding/base64"
	"testing"
)

// TestDecodeMcpAssetResponseHappyPath verifies that the helper used by
// ServeAsset to convert MCP read_session_asset output into HTTP response
// bytes works for a normal success payload.
func TestDecodeMcpAssetResponseHappyPath(t *testing.T) {
	payload := []byte("\x89PNG\r\n\x1a\nfakepng")
	res := map[string]interface{}{
		"success":     true,
		"bytes":       len(payload),
		"data_base64": base64.StdEncoding.EncodeToString(payload),
		"mime_type":   "image/png",
		"filename":    "foo.png",
	}
	raw, mime, filename, ok := decodeMcpAssetResponse(res)
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if string(raw) != string(payload) {
		t.Fatalf("payload mismatch: got %q want %q", raw, payload)
	}
	if mime != "image/png" {
		t.Fatalf("mime mismatch: %q", mime)
	}
	if filename != "foo.png" {
		t.Fatalf("filename mismatch: %q", filename)
	}
}

// TestDecodeMcpAssetResponseMissingBase64 covers the malformed-success branch
// (success=true but no payload) so ServeAsset falls through to the local-fs
// fallback instead of streaming an empty body.
func TestDecodeMcpAssetResponseMissingBase64(t *testing.T) {
	res := map[string]interface{}{"success": true, "mime_type": "image/png"}
	if _, _, _, ok := decodeMcpAssetResponse(res); ok {
		t.Fatalf("expected ok=false when data_base64 missing")
	}
}

// TestDecodeMcpAssetResponseToolLevelError covers the tool-error branch
// (success=false, error="file not found"). Must return ok=false so the
// fallback path runs.
func TestDecodeMcpAssetResponseToolLevelError(t *testing.T) {
	res := map[string]interface{}{"success": false, "error": "file not found"}
	if _, _, _, ok := decodeMcpAssetResponse(res); ok {
		t.Fatalf("expected ok=false for tool-level error response")
	}
}

// TestDecodeMcpAssetResponseInvalidBase64 covers the corrupt-payload branch.
// Must not panic; must return ok=false so the fallback path runs.
func TestDecodeMcpAssetResponseInvalidBase64(t *testing.T) {
	res := map[string]interface{}{
		"success":     true,
		"data_base64": "this is not valid base64 !!!",
	}
	if _, _, _, ok := decodeMcpAssetResponse(res); ok {
		t.Fatalf("expected ok=false for invalid base64")
	}
}

// TestDecodeMcpAssetResponseMimeDefault covers the "unknown extension"
// fallback. Empty mime must be replaced with application/octet-stream.
func TestDecodeMcpAssetResponseMimeDefault(t *testing.T) {
	payload := []byte{0x00, 0x01, 0x02}
	res := map[string]interface{}{
		"success":     true,
		"bytes":       len(payload),
		"data_base64": base64.StdEncoding.EncodeToString(payload),
		"mime_type":   "",
		"filename":    "blob.bin",
	}
	raw, mime, filename, ok := decodeMcpAssetResponse(res)
	if !ok {
		t.Fatalf("expected ok=true")
	}
	if mime != "application/octet-stream" {
		t.Fatalf("expected default mime, got %q", mime)
	}
	if filename != "blob.bin" {
		t.Fatalf("filename lost: %q", filename)
	}
	if len(raw) != 3 {
		t.Fatalf("payload truncated")
	}
}