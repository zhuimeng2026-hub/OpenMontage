package handlers

import (
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"
)

// SessionAssets returns the images already uploaded for the caller's current
// MCP session. It lets the create-video page show what is already on the
// server so the user does not re-upload files after a partial upload failure.
//
// Resolution order (all scoped to the caller's stable owner identity):
//   - if ?project_id= is given, use that image batch's dedicated upstream session;
//   - else if the scope has an active "collecting" batch, use it (template mode);
//   - else fall back to the long-lived user-level upstream session (script mode).
func (h *Handlers) SessionAssets(c *gin.Context) {
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	assets, err := h.listSessionAssets(scope, c.Query("project_id"))
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"assets": assets})
}

// ServeAsset streams one uploaded image the caller has already uploaded, given
// its repo-root-relative path. Access is owner-scoped: the path must belong to
// one of the caller's own session assets, and (defense in depth) the resolved
// absolute path must live under <RepoRoot>/projects. This prevents the
// endpoint from serving arbitrary files.
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
	repoRoot := h.Cfg.RepoRoot
	abs := filepath.Join(repoRoot, filepath.Clean(rel))
	projectsRoot := filepath.Join(repoRoot, "projects")
	if !strings.HasPrefix(abs, projectsRoot+string(os.PathSeparator)) && abs != projectsRoot {
		c.Status(http.StatusForbidden)
		return
	}
	info, statErr := os.Stat(abs)
	if statErr != nil || info.IsDir() {
		c.Status(http.StatusNotFound)
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
