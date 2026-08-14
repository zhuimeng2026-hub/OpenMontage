package handlers

import (
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/mcp"
)

// RenderQueue returns the caller's own render jobs. Scoping is structural: the
// jobs are stored per BFF session (ff_sid), so a user can only ever see the
// renders they submitted — never another user's. The route is auth-gated in
// main.go so an unauthenticated caller is rejected before reaching here.
//
// In-flight jobs (渲染中/排队) are refreshed asynchronously against the upstream
// get_render_status so terminal states (已完成/失败) land in the store without
// blocking this response; the frontend's poll + SSE then surface them shortly.
func (h *Handlers) RenderQueue(c *gin.Context) {
	sid := h.ensureSession(c)
	jobs := h.Store.ListJobs(sid)
	go h.refreshJobStatuses(sid, jobs)
	c.JSON(http.StatusOK, gin.H{"jobs": jobs})
}

// refreshJobStatuses backfills terminal states for in-flight jobs. It runs
// best-effort in the background and never fails the request; the frontend's
// 5s poll (and the SSE stream) will pick up the update on the next cycle.
func (h *Handlers) refreshJobStatuses(sid string, jobs []*mcp.RenderJob) {
	const maxRefresh = 5 // cap upstream calls per cycle to keep the store light
	refreshed := 0
	for _, j := range jobs {
		if refreshed >= maxRefresh {
			break
		}
		if j.Status != "渲染中" && j.Status != "排队" {
			continue
		}
		refreshed++
		res, err := h.Store.Call(sid, "get_render_status", map[string]interface{}{"render_job_id": j.JobID})
		if err != nil {
			continue
		}
		if mapped := mapUpstreamStatus(digString(res, "status")); mapped != "" && mapped != j.Status {
			h.Store.UpdateJobStatus(sid, j.JobID, mapped)
		}
	}
}

// mapUpstreamStatus normalises the upstream render-status vocabulary to the
// Chinese labels the frontend renders.
func mapUpstreamStatus(s string) string {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "published", "done", "success", "completed", "finished":
		return "已完成"
	case "failed", "error":
		return "失败"
	case "rendering", "running", "processing", "in_progress", "progress":
		return "渲染中"
	case "queued", "queue", "pending", "waiting":
		return "排队"
	}
	return ""
}
