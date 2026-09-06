package handlers

import (
	"encoding/base64"
	"fmt"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/mcp"
)

// SessionAssets returns the images already uploaded for the caller's current
// MCP session. It lets the create-video page show what is already on the
// server so the user does not re-upload files after a partial upload failure.
//
// Resolution order (all scoped to the caller's stable owner identity):
//   - if ?project_id= is given, use that image batch's dedicated upstream session;
//   - else if the scope has an active "collecting" batch, use it (template mode);
//   - else fall back to the long-lived user-level upstream session (script mode).
//
// Reconciliation: assets whose ``relative_path`` no longer resolves to a
// regular file on disk are dropped from the response and counted as
// ``stale_count``. This keeps the SPA from rendering broken <img> tags when
// the upstream MCP session state references files that were cleaned up,
// dedup-removed, or never written because of a RepoRoot mismatch. Stale
// entries are also logged at warn level so an operator can spot recurring
// drift without reading the response body.
func (h *Handlers) SessionAssets(c *gin.Context) {
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	assets, err := h.listSessionAssets(scope, c.Query("project_id"))
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	repoRoot := h.Cfg.RepoRoot
	live, stale := reconcileAssets(assets, repoRoot, scope, sid)
	if stale > 0 {
		log.Printf("[session-asset] reconcile summary scope=%s sid_hash=%s stale=%d total=%d",
			scope, mcp.ShortHashForLog(sid), stale, len(assets))
	}
	c.JSON(http.StatusOK, gin.H{"assets": live, "stale_count": stale, "total_count": len(assets)})
}

// reconcileAssets filters out entries whose ``relative_path`` no longer
// resolves to a regular file under ``repoRoot``. Pure function so it can be
// unit-tested without standing up the full handler chain. Stale entries are
// logged at warn level here so both the list and per-entry visibility are in
// one place.
func reconcileAssets(assets []map[string]interface{}, repoRoot, scope string, sid string) ([]map[string]interface{}, int) {
	live := make([]map[string]interface{}, 0, len(assets))
	stale := 0
	for _, a := range assets {
		rel, _ := a["relative_path"].(string)
		if rel == "" {
			live = append(live, a)
			continue
		}
		abs := filepath.Join(repoRoot, filepath.Clean(rel))
		info, statErr := os.Stat(abs)
		if statErr != nil || info == nil || !info.Mode().IsRegular() {
			stale++
			log.Printf("[session-asset] stale_entry scope=%s sid_hash=%s rel=%s abs=%s err=%v",
				scope, mcp.ShortHashForLog(sid), rel, abs, statErr)
			continue
		}
		live = append(live, a)
	}
	return live, stale
}

// decodeMcpAssetResponse converts the JSON map returned by MCP
// ``read_session_asset`` into the bytes/mime/filename that ServeAsset streams
// back to the browser. Returns ok=false when the response is a tool-level
// error (caller should fall through to the local-fs fallback).
func decodeMcpAssetResponse(res map[string]interface{}) (raw []byte, mime string, filename string, ok bool) {
	success, _ := res["success"].(bool)
	if !success {
		return nil, "", "", false
	}
	encoded, _ := res["data_base64"].(string)
	if encoded == "" {
		return nil, "", "", false
	}
	decoded, err := base64.StdEncoding.DecodeString(encoded)
	if err != nil {
		return nil, "", "", false
	}
	mime, _ = res["mime_type"].(string)
	if mime == "" {
		mime = "application/octet-stream"
	}
	filename, _ = res["filename"].(string)
	return decoded, mime, filename, true
}

// ServeAsset streams one uploaded image the caller has already uploaded, given
// its repo-root-relative path. Access is owner-scoped: the path must belong to
// one of the caller's own session assets.
//
// As of the BFF/MCP split deployment, the BFF no longer reads the file off
// its own local filesystem — the upload lives on the MCP host. The primary
// path is to forward the read to MCP via ``read_session_asset`` after the
// whitelist check, then stream the returned base64 bytes back to the
// browser. The local RepoRoot-based filesystem check is retained as a
// defense-in-depth fallback: if a shared filesystem IS mounted, the file
// is served from there; otherwise the SPA gets a JSON 404 with the
// resolved absolute path for diagnostics.
func (h *Handlers) ServeAsset(c *gin.Context) {
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	rel := c.Query("rel")
	if rel == "" {
		c.Status(http.StatusBadRequest)
		return
	}
	assets, err := h.listSessionAssets(scope, c.Query("project_id"))
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	allowed := false
	for _, a := range assets {
		if rp, _ := a["relative_path"].(string); rp == rel {
			allowed = true
			break
		}
	}
	if !allowed {
		c.Status(http.StatusForbidden)
		return
	}

	// Primary path: proxy the read to the MCP server. The MCP host owns the
	// uploaded bytes; the BFF only owns the routing layer. ``Store.Call``
	// auto-routes to a batch-level MCP session if a matching ``batch_id``
	// or ``project_id`` is in ``args``; for a bare read we just hand the
	// user-level scope and let the server resolve ownership from the
	// session context.
	res, mcpErr := h.Store.Call(scope, "read_session_asset", map[string]interface{}{
		"relative_path": rel,
	})
	if mcpErr == nil {
		if raw, mime, filename, ok := decodeMcpAssetResponse(res); ok {
			if filename != "" {
				c.Header("Content-Disposition", fmt.Sprintf("inline; filename=%q", filename))
			}
			c.Data(http.StatusOK, mime, raw)
			return
		}
		if errStr, _ := res["error"].(string); errStr != "" {
			// MCP tool-level error (e.g. file not found). Log and fall
			// through to the local-fs path in case a shared mount has it.
			log.Printf("[session-asset] mcp_read_error scope=%s sid_hash=%s rel=%s err=%q",
				scope, mcp.ShortHashForLog(sid), rel, errStr)
		} else {
			log.Printf("[session-asset] mcp_decode_failed scope=%s sid_hash=%s rel=%s",
				scope, mcp.ShortHashForLog(sid), rel)
		}
	} else {
		log.Printf("[session-asset] mcp_proxy_err scope=%s sid_hash=%s rel=%s err=%v",
			scope, mcp.ShortHashForLog(sid), rel, mcpErr)
	}

	// Defense-in-depth local fallback (shared-filesystem deploys).
	repoRoot := h.Cfg.RepoRoot
	abs := filepath.Join(repoRoot, filepath.Clean(rel))
	projectsRoot := filepath.Join(repoRoot, "projects")
	if !strings.HasPrefix(abs, projectsRoot+string(os.PathSeparator)) && abs != projectsRoot {
		c.Status(http.StatusForbidden)
		return
	}
	info, statErr := os.Stat(abs)
	if statErr != nil || info == nil || !info.Mode().IsRegular() {
		reason := "missing"
		if statErr == nil && info != nil && info.IsDir() {
			reason = "is_dir"
		} else if os.IsNotExist(statErr) {
			reason = "not_found"
		} else if statErr != nil {
			reason = statErr.Error()
		}
		log.Printf("[session-asset] serve_404 scope=%s sid_hash=%s rel=%s repo_root=%s abs=%s reason=%s",
			scope, mcp.ShortHashForLog(sid), rel, repoRoot, abs, reason)
		c.Data(http.StatusNotFound, "application/json; charset=utf-8",
			[]byte(fmt.Sprintf(`{"error":"asset_not_found","reason":%q,"abs":%q,"rel":%q}`,
				reason, abs, rel)))
		return
	}
	c.File(abs)
}

// listSessionAssets resolves the caller's uploaded assets through the MCP layer
// and normalises them into a slice of maps.
func (h *Handlers) listSessionAssets(scope, projectID string) ([]map[string]interface{}, error) {
	var res map[string]interface{}
	var err error
	if projectID != "" {
		b, berr := h.ImageBatches.ByProject(scope, projectID)
		if berr != nil {
			return nil, berr
		}
		if b == nil {
			return nil, fmt.Errorf("image batch not found for project %q", projectID)
		}
		res, err = h.Store.CallBatch(scope, b.ID, b.ProjectID, "get_session_assets", map[string]interface{}{})
	} else {
		if batches, berr := h.ImageBatches.List(scope); berr == nil {
			for _, b := range batches {
				if b.Status == "collecting" {
					res, err = h.Store.CallBatch(scope, b.ID, b.ProjectID, "get_session_assets", map[string]interface{}{})
					break
				}
			}
		}
		if res == nil {
			res, err = h.Store.Call(scope, "get_session_assets", map[string]interface{}{})
		}
	}
	if err != nil {
		return nil, err
	}
	if res == nil {
		return []map[string]interface{}{}, nil
	}
	if errStr, ok := res["error"].(string); ok && errStr != "" {
		return nil, fmt.Errorf("%s", errStr)
	}
	raw, _ := res["assets"].([]interface{})
	out := make([]map[string]interface{}, 0, len(raw))
	for _, item := range raw {
		if m, ok := item.(map[string]interface{}); ok {
			out = append(out, m)
		}
	}
	return out, nil
}
