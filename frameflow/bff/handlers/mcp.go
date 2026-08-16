package handlers

import (
	"fmt"
	"log"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
)

const maxMCPBodyBytes = 2 << 20

var allowedMCPTools = map[string]bool{
	"upload_asset_chunk":          true,
	"create_remotion_video_share": true,
	"get_render_status":           true,
}

// MCPProxy receives { "tool": "<name>", "args": { ... } } and forwards it to the
// OpenMontage MCP server as a tools/call, returning the extract()-ed structured
// result (mirrors om_mcp_probe.py). The caller's BFF session cookie selects the
// long-lived MCP client so uploads and the create call share one session.
//
// For the manual upload -> create flow it also enforces the user's per-tier
// MaxFilesPerSubmission cap, mirroring the pre-check the batch-render surface
// performs in TemplateHandler.BatchRender. The cap is tracked per BFF session
// in SessionStore (completed-upload count), reset when a video is created.
func (h *Handlers) MCPProxy(c *gin.Context) {
	c.Request.Body = http.MaxBytesReader(c.Writer, c.Request.Body, maxMCPBodyBytes)
	var req struct {
		Tool string                 `json:"tool"`
		Args map[string]interface{} `json:"args"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
		return
	}
	if req.Tool == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "tool is required"})
		return
	}
	if !allowedMCPTools[req.Tool] {
		c.JSON(http.StatusBadRequest, gin.H{"error": "tool is not allowed"})
		return
	}
	sid := h.ensureSession(c)
	operation, _ := req.Args["operation"].(string)
	projectID, _ := req.Args["project_id"].(string)
	log.Printf("[bff-mcp] start tool=%s operation=%s sid_hash=%s project_id=%s", req.Tool, operation, mcp.ShortHashForLog(sid), projectID)

	// Pre-check the per-submission file cap on a new upload. Rejecting at the
	// "start" step prevents any bytes from being sent upstream once the cap is
	// reached. (upload_asset_chunk's operation lives in req.Args.)
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "start" {
			tier := h.Limits.Resolve(sid)
			lim := limits.ForTier(tier)
			if h.Store.AssetCount(sid) >= lim.MaxFilesPerSubmission {
				c.JSON(http.StatusUnprocessableEntity, gin.H{
					"error": fmt.Sprintf(
						"your %q tier allows at most %d images per submission; this submission has already reached the limit",
						tier, lim.MaxFilesPerSubmission),
					"files": h.Store.AssetCount(sid),
					"max":   lim.MaxFilesPerSubmission,
				})
				return
			}
		}
	}

	res, err := h.Store.Call(sid, req.Tool, req.Args)
	if err != nil {
		log.Printf("[bff-mcp] upstream_failed tool=%s operation=%s sid_hash=%s project_id=%s err=%v", req.Tool, operation, mcp.ShortHashForLog(sid), projectID, err)
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	if failure, ok := res["error"].(string); ok && failure != "" {
		log.Printf("[bff-mcp] tool_error tool=%s operation=%s sid_hash=%s project_id=%s error=%q", req.Tool, operation, mcp.ShortHashForLog(sid), projectID, failure)
	}

	// A successful render submission enters the caller's own render queue. The
	// queue is keyed by the BFF session, so each user only ever sees their own
	// jobs — never another caller's (owner isolation is structural, not a filter).
	if req.Tool == "create_remotion_video_share" {
		if jobID := digString(res, "render_job_id"); jobID != "" {
			name, _ := req.Args["title"].(string)
			if name == "" {
				name = "帧流作品"
			}
			resLabel, _ := req.Args["aspect_ratio"].(string)
			if resLabel == "" {
				resLabel = "9:16"
			}
			h.Store.RecordJob(sid, mcp.RenderJob{
				JobID:     jobID,
				Name:      name,
				Res:       resLabel,
				Status:    "渲染中",
				CreatedAt: time.Now(),
			})
		}
	}

	// Update the per-submission counter after a successful call:
	//   - a completed upload increments the count
	//   - creating a video closes the submission and resets it for the next one
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "complete" {
			h.Store.IncAsset(sid)
			if h.ImageBatches != nil {
				if projectID, _ := req.Args["project_id"].(string); projectID != "" {
					h.ImageBatches.IncAsset(sid, projectID)
				}
			}
		}
	} else if req.Tool == "create_remotion_video_share" {
		h.Store.ResetAsset(sid)
	}

	c.JSON(http.StatusOK, res)
}
