package handlers

import (
	"net/http"
	"net/url"
	"strings"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/mcp"
)

// RenderQueue returns the caller's own render jobs. Scoping is structural: the
// jobs are stored under the stable owner identity (WeChat account or, when
// anonymous, the device session), so a user can only ever see the renders they
// submitted — never another user's — and the same account sees the same queue
// on any machine. The route is auth-gated in main.go so an unauthenticated
// caller is rejected before reaching here.
//
// 状态一致性：渲染任务状态以渲染后台（MCP get_render_status）为唯一权威来源。
// 这里对每个 job 同步实时取上游状态并回填存储后再返回，避免 BFF 本地缓存
// 状态与后台漂移（例如后台已 published，BFF 仍显示失败）。
func (h *Handlers) RenderQueue(c *gin.Context) {
	sid := h.ensureSession(c)
	// Scope by the stable WeChat identity (or device session when anonymous) so a
	// user's render queue is consistent across machines.
	scope := renderQueueOwnerID(sid)
	jobs := h.Store.ListJobs(scope)
	h.refreshJobStatuses(scope, jobs)
	// 刷新后重新读取，确保响应反映渲染后台的最新状态（jobStore 路径下
	// UpdateJobResult 只写库、不回填本次切片）。
	jobs = h.Store.ListJobs(scope)
	c.JSON(http.StatusOK, gin.H{"jobs": jobs})
}

// refreshJobStatuses backfills the authoritative render status from the upstream
// MCP for ALL owned jobs (not just in-flight ones), so the store always mirrors
// the render backend. It runs best-effort and never fails the request; the
// frontend's 5s poll (and the SSE stream) will pick up the update.
func (h *Handlers) refreshJobStatuses(scope string, jobs []*mcp.RenderJob) {
	const maxRefresh = 20 // cap upstream calls per cycle to keep the store light
	refreshed := 0
	for _, j := range jobs {
		if refreshed >= maxRefresh {
			break
		}
		// 不再跳过「失败」等终态：任何 job 都向渲染后台实时取状态。
		refreshed++
		args := map[string]interface{}{"render_job_id": j.JobID}
		var res map[string]interface{}
		var err error
		if j.BatchID != "" || j.ProjectID != "" {
			res, err = h.Store.CallBatch(scope, j.BatchID, j.ProjectID, "get_render_status", args)
		} else {
			res, err = h.Store.Call(scope, "get_render_status", args)
		}
		if err != nil {
			continue
		}
		if mapped := mapUpstreamStatus(digString(res, "status")); mapped != "" {
			shareURL := digString(res, "share_url")
			if mapped == "已完成" && !validHTTPURL(shareURL) {
				h.Store.UpdateJobResult(scope, j.JobID, "失败", "")
				continue
			}
			if mapped != j.Status || shareURL != "" {
				h.Store.UpdateJobResult(scope, j.JobID, mapped, shareURL)
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
