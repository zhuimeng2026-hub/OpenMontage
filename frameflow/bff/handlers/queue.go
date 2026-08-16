package handlers

import (
	"net/http"
	"net/url"
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
		if j.Status != "渲染中" && j.Status != "排队" && !(j.Status == "已完成" && j.ShareURL == "") {
			continue
		}
		refreshed++
		args := map[string]interface{}{"render_job_id": j.JobID}
		var res map[string]interface{}
		var err error
		if j.BatchID != "" || j.ProjectID != "" {
			res, err = h.Store.CallBatch(sid, j.BatchID, j.ProjectID, "get_render_status", args)
		} else {
			res, err = h.Store.Call(sid, "get_render_status", args)
		}
		if err != nil {
			continue
		}
		if mapped := mapUpstreamStatus(digString(res, "status")); mapped != "" {
			shareURL := digString(res, "share_url")
			if mapped == "已完成" && !validHTTPURL(shareURL) {
				h.Store.UpdateJobResult(sid, j.JobID, "失败", "")
				continue
			}
			if mapped != j.Status || shareURL != "" {
				h.Store.UpdateJobResult(sid, j.JobID, mapped, shareURL)
			}
		}
	}
}

func validHTTPURL(value string) bool {
	u, err := url.Parse(strings.TrimSpace(value))
	return err == nil && u.Scheme != "" && (strings.EqualFold(u.Scheme, "http") || strings.EqualFold(u.Scheme, "https")) && u.Host != ""
}

// mapUpstreamStatus normalises the upstream render-status vocabulary to the
// Chinese labels the frontend renders.
func mapUpstreamStatus(s string) string {
	switch strings.ToLower(strings.TrimSpace(s)) {
	case "published", "done", "success", "succeeded", "completed", "finished":
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
