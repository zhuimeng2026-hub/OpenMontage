package handlers

import (
	"fmt"
	"net/http"
	"strings"

	"frameflow-bff/internal/mcp"
	"github.com/gin-gonic/gin"
)

// RepublishRender asks the upstream worker to publish an already-rendered job
// again.  Ownership is checked against the caller's session before any MCP
// call; the batch/project identifiers are taken from the stored job rather
// than from the browser request.
func (h *Handlers) RepublishRender(c *gin.Context) {
	sid := h.ensureSession(c)
	// Scope by the stable WeChat identity (or device session when anonymous) so a
	// render job created on one machine is republishable after login on another.
	scope := renderQueueOwnerID(sid)
	jobID := strings.TrimSpace(c.Param("jobId"))
	job := ownedRenderJob(h.Store, scope, jobID)
	if job == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "render job not found"})
		return
	}
	if !republishEligibleStatus(job.Status) {
		c.JSON(http.StatusConflict, gin.H{"error": "render job is not eligible for republish"})
		return
	}
	args := map[string]interface{}{"render_job_id": job.JobID}
	var result map[string]interface{}
	var err error
	if job.BatchID != "" || job.ProjectID != "" {
		result, err = h.Store.CallBatch(scope, job.BatchID, job.ProjectID, "retry_render_publish", args)
	} else {
		result, err = h.Store.Call(scope, "retry_render_publish", args)
	}
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	if msg := digString(result, "error"); msg != "" {
		c.JSON(http.StatusBadGateway, gin.H{"error": msg})
		return
	}
	shareURL, err := finishRepublish(h.Store, scope, job, result)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"job_id": job.JobID, "status": "已完成", "share_url": shareURL})
}

func republishEligibleStatus(status string) bool {
	return status == "已完成" || status == "失败"
}

func ownedRenderJob(store *mcp.SessionStore, scope, jobID string) *mcp.RenderJob {
	for _, job := range store.ListJobs(scope) {
		if job != nil && job.JobID == jobID {
			return job
		}
	}
	return nil
}

func finishRepublish(store *mcp.SessionStore, scope string, job *mcp.RenderJob, result map[string]interface{}) (string, error) {
	if msg := digString(result, "error"); msg != "" {
		return "", fmt.Errorf("%s", msg)
	}
	shareURL := digString(result, "share_url")
	if !validHTTPURL(shareURL) {
		return "", fmt.Errorf("upstream returned an invalid share_url")
	}
	store.UpdateJobResult(scope, job.JobID, "已完成", shareURL)
	return shareURL, nil
}
