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
	// Key the upstream MCP session mapping by the stable WeChat identity (or the
	// device session when anonymous) so the same account maps to the SAME upstream
	// Mcp-Session-Id across machines — that is what makes uploaded assets and the
	// generated video consistent cross-device (like email). See plan:
	// cosmic-pulse-babbage.
	scope := renderQueueOwnerID(sid)
	operation, _ := req.Args["operation"].(string)
	projectID, _ := req.Args["project_id"].(string)
	log.Printf("[bff-mcp] start tool=%s operation=%s sid_hash=%s scope_hash=%s project_id=%s", req.Tool, operation, mcp.ShortHashForLog(sid), mcp.ShortHashForLog(scope), projectID)
	start := time.Now()
	var resultErr error
	defer func() {
		log.Printf("[bff-mcp] done tool=%s operation=%s scope_hash=%s project_id=%s elapsed_ms=%d err=%v",
			req.Tool, operation, mcp.ShortHashForLog(scope), projectID, time.Since(start).Milliseconds(), resultErr)
	}()

	// Pre-check the per-submission file cap on a new upload. Rejecting at the
	// "start" step prevents any bytes from being sent upstream once the cap is
	// reached. (upload_asset_chunk's operation lives in req.Args.)
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "start" {
			tier := h.Limits.Resolve(scope)
			lim := limits.ForTier(tier)
			if h.Store.AssetCount(scope) >= lim.MaxFilesPerSubmission {
				c.JSON(http.StatusUnprocessableEntity, gin.H{
					"error": fmt.Sprintf(
						"your %q tier allows at most %d images per submission; this submission has already reached the limit",
						tier, lim.MaxFilesPerSubmission),
					"files": h.Store.AssetCount(scope),
					"max":   lim.MaxFilesPerSubmission,
				})
				return
			}
		}
	}
	if req.Tool == "create_remotion_video_share" {
		// Never trust a browser-supplied fairness key. Bind scheduling to the
		// authenticated WeChat identity (or this BFF session when anonymous).
		if req.Args == nil {
			req.Args = make(map[string]interface{})
		}
		req.Args["queue_owner_id"] = renderQueueOwnerID(sid)
	}

	res, err := h.Store.Call(scope, req.Tool, req.Args)
	if err != nil {
		resultErr = err
		log.Printf("[bff-mcp] upstream_failed tool=%s operation=%s sid_hash=%s project_id=%s err=%v", req.Tool, operation, mcp.ShortHashForLog(sid), projectID, err)
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	if failure, ok := res["error"].(string); ok && failure != "" {
		resultErr = fmt.Errorf("%s", failure)
		log.Printf("[bff-mcp] tool_error tool=%s operation=%s sid_hash=%s project_id=%s error=%q", req.Tool, operation, mcp.ShortHashForLog(sid), projectID, failure)
	}

	// A successful render submission enters the caller's own render queue. The
	// queue is keyed by the stable owner identity (scope), so each user only ever
	// sees their own jobs — never another caller's (owner isolation is structural,
	// not a filter) and the same account sees the same queue across machines.
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
			jobStatus := "渲染中"
			if mapUpstreamStatus(digString(res, "status")) == "排队" {
				jobStatus = "排队"
			}
			h.Store.RecordJob(scope, mcp.RenderJob{
				JobID:     jobID,
				Name:      name,
				Res:       resLabel,
				Status:    jobStatus,
				CreatedAt: time.Now(),
			})
		}
	}

	// Update the per-submission counter after a successful call:
	//   - a completed upload increments the count
	//   - creating a video closes the submission and resets it for the next one
	if req.Tool == "upload_asset_chunk" {
		if op, _ := req.Args["operation"].(string); op == "complete" {
			h.Store.IncAsset(scope)
			if h.ImageBatches != nil {
				if projectID, _ := req.Args["project_id"].(string); projectID != "" {
					h.ImageBatches.IncAsset(scope, projectID)
				}
			}
		}
	} else if req.Tool == "create_remotion_video_share" {
		h.Store.ResetAsset(scope)
	}

	c.JSON(http.StatusOK, res)
}
