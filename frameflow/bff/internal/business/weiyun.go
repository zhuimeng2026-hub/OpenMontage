package business

import (
	"context"
	"fmt"
	"strings"

	"frameflow-bff/internal/mcp"
)

// WeiyunFetcher pulls per-scenario images from Tencent Weiyun's OFFICIAL MCP
// service (https://www.weiyun.com/api/v3/mcpserver), authenticated with an API
// Key via the `WyHeader: mcp_token=<key>` header (NOT the Bearer scheme).
//
// Model: each business scenario is a Weiyun folder. businessKey is that folder's
// dir_key (hex). Fetch lists its files, then resolves a download URL + cookie
// for each image file and returns them so the upload pipeline can pull bytes.
//
// This reuses the same Streamable-HTTP MCP client used for the Remotion server,
// so the handshake / session-rotation logic is shared.
type WeiyunFetcher struct {
	client *mcp.Client
}

// NewWeiyunFetcher wraps an already-constructed MCP client (authorized for the
// Weiyun endpoint) as a Fetcher.
func NewWeiyunFetcher(client *mcp.Client) *WeiyunFetcher {
	return &WeiyunFetcher{client: client}
}

// Initialize performs the MCP handshake up front. Safe to call once at startup;
// Fetch also re-initializes on a stale-session error.
func (f *WeiyunFetcher) Initialize() error {
	return f.client.Initialize()
}

var imageExts = []string{".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".heic"}

// Fetch lists the scenario folder's files and resolves a download URL + cookie
// for each image, returning them as ImageRefs.
func (f *WeiyunFetcher) Fetch(_ context.Context, businessKey string) ([]ImageRef, error) {
	if businessKey == "" {
		return nil, fmt.Errorf("weiyun: businessKey (dir_key) is required")
	}
	listRes, err := f.call("weiyun.list", map[string]interface{}{
		"dir_key":  businessKey,
		"get_type": 2, // files only
		"limit":    50,
	})
	if err != nil {
		return nil, fmt.Errorf("weiyun.list(%s): %w", businessKey, err)
	}
	pdirKey := digStr(listRes, "pdir_key")

	files, _ := listRes["file_list"].([]interface{})
	if files == nil {
		if r, ok := listRes["result"].(map[string]interface{}); ok {
			files, _ = r["file_list"].([]interface{})
		}
	}
	if files == nil {
		return nil, fmt.Errorf("weiyun.list(%s): file_list missing in response", businessKey)
	}

	imgs := make([]ImageRef, 0, len(files))
	for _, fi := range files {
		fm, ok := fi.(map[string]interface{})
		if !ok {
			continue
		}
		name := firstNonEmpty(digStr(fm, "file_name"), digStr(fm, "filename"), digStr(fm, "name"))
		fileID := firstNonEmpty(digStr(fm, "file_id"), digStr(fm, "id"))
		if fileID == "" || !isImage(name) {
			continue
		}
		dl, err := f.call("weiyun.download", map[string]interface{}{
			"items": []map[string]interface{}{
				{"file_id": fileID, "pdir_key": pdirKey},
			},
		})
		if err != nil {
			return nil, fmt.Errorf("weiyun.download(%s): %w", fileID, err)
		}
		url := digStr(dl, "https_download_url")
		cookie := digStr(dl, "cookie")
		if url == "" {
			return nil, fmt.Errorf("weiyun.download(%s): https_download_url missing", fileID)
		}
		ref := ImageRef{URL: url, Name: name}
		if cookie != "" {
			ref.Headers = map[string]string{"Cookie": cookie}
		}
		imgs = append(imgs, ref)
	}
	if len(imgs) == 0 {
		return nil, fmt.Errorf("weiyun: no images found in dir %q (check dir_key / image extensions)", businessKey)
	}
	return imgs, nil
}

// call wraps CallTool with a stale-session retry: if the upstream says the MCP
// session is gone, re-handshake once and retry.
func (f *WeiyunFetcher) call(tool string, args map[string]interface{}) (map[string]interface{}, error) {
	res, err := f.client.CallTool(tool, args)
	if err != nil {
		return nil, err
	}
	if mcp.IsSessionError(res) {
		if ierr := f.client.Initialize(); ierr != nil {
			return nil, ierr
		}
		return f.client.CallTool(tool, args)
	}
	return res, nil
}

func isImage(name string) bool {
	if name == "" {
		return false
	}
	lower := strings.ToLower(name)
	for _, ext := range imageExts {
		if strings.HasSuffix(lower, ext) {
			return true
		}
	}
	return false
}

// digStr pulls a string field, trying top-level first, then nested under
// "result" (MCP tools sometimes return the payload wrapped there).
func digStr(m map[string]interface{}, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return v
	}
	if r, ok := m["result"].(map[string]interface{}); ok {
		if v, ok := r[key].(string); ok {
			return v
		}
	}
	return ""
}

func firstNonEmpty(vals ...string) string {
	for _, v := range vals {
		if v != "" {
			return v
		}
	}
	return ""
}
