package handlers

import (
	"fmt"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
)

// Named scripts are deliberately server-side choices. They map to trusted
// Remotion compositions; arbitrary browser-supplied TSX is not executed.
var imageScripts = map[string]string{
	"photo-ken-burns":        "照片运镜",
	"cinematic-montage":      "电影混剪",
	"ecommerce-product-demo": "电商产品演示",
}

type ImageBatchHandler struct {
	Batches   *imagebatch.Store
	Sessions  *mcp.SessionStore
	Cfg       *config.Config
	Semaphore *limits.Semaphore
}

// ensureBatchSession restores the dedicated upstream MCP session after a BFF
// restart. Durable batch metadata is enough to recreate the client lazily.
func (h *ImageBatchHandler) ensureBatchSession(sid string, b *imagebatch.Batch) error {
	return h.Sessions.CreateBatch(sid, b.ID, b.ProjectID)
}

func NewImageBatchHandler(cfg *config.Config, batches *imagebatch.Store, sessions *mcp.SessionStore, semaphores ...*limits.Semaphore) *ImageBatchHandler {
	var semaphore *limits.Semaphore
	if len(semaphores) > 0 {
		semaphore = semaphores[0]
	}
	return &ImageBatchHandler{Cfg: cfg, Batches: batches, Sessions: sessions, Semaphore: semaphore}
}

func (h *ImageBatchHandler) ensureSession(c *gin.Context) string {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		return sid
	}
	sid := randHex(16)
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(sessionCookieName, sid, 60*60*24*7, "/", "", h.Cfg.SessionSecure, true)
	return sid
}

func (h *ImageBatchHandler) Scripts(c *gin.Context) {
	out := make([]gin.H, 0, len(imageScripts))
	for id, name := range imageScripts {
		out = append(out, gin.H{"id": id, "name": name})
	}
	c.JSON(http.StatusOK, gin.H{"scripts": out})
}

func (h *ImageBatchHandler) Create(c *gin.Context) {
	sid := h.ensureSession(c)
	var req struct {
		ScriptID string `json:"script_id"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
		return
	}
	if _, ok := imageScripts[req.ScriptID]; !ok {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": "unknown script_id", "scripts": imageScripts})
		return
	}

	id := "batch-" + randHex(12)
	projectID := "frameflow-batch-" + id
	if err := h.Sessions.CreateBatch(sid, id, projectID); err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	b, err := h.Batches.Create(sid, id, projectID, req.ScriptID)
	if err != nil {
		h.Sessions.DropBatch(sid, id, projectID)
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, b)
}

func (h *ImageBatchHandler) List(c *gin.Context) {
	sid := h.ensureSession(c)
	batches, err := h.Batches.List(sid)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"batches": batches})
}

func (h *ImageBatchHandler) Get(c *gin.Context) {
	sid := h.ensureSession(c)
	b, err := h.Batches.Get(sid, c.Param("id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if b == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "image batch not found"})
		return
	}
	if b.RenderJobID != "" && (b.Status == "rendering" || b.Status == "collecting") {
		if err := h.ensureBatchSession(sid, b); err != nil {
			c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
			return
		}
		if res, err := h.Sessions.CallBatch(sid, b.ID, b.ProjectID, "get_render_status", map[string]interface{}{"render_job_id": b.RenderJobID}); err == nil {
			status := strings.ToLower(strings.TrimSpace(digString(res, "status")))
			switch status {
			case "published", "done", "success", "completed", "finished":
				if b.Status == "rendering" && h.Semaphore != nil {
					h.Semaphore.ReleaseBatch(b.ID)
				}
				h.Batches.Update(sid, b.ID, func(x *imagebatch.Batch) { x.Status = "published"; x.VideoURL = digString(res, "share_url") })
				h.Sessions.DropBatch(sid, b.ID, b.ProjectID)
			case "failed", "error":
				if b.Status == "rendering" && h.Semaphore != nil {
					h.Semaphore.ReleaseBatch(b.ID)
				}
				h.Batches.Update(sid, b.ID, func(x *imagebatch.Batch) { x.Status = "failed"; x.Error = digString(res, "error") })
				h.Sessions.DropBatch(sid, b.ID, b.ProjectID)
			}
		}
	}
	current, err := h.Batches.Get(sid, b.ID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, current)
}

func (h *ImageBatchHandler) Render(c *gin.Context) {
	sid := h.ensureSession(c)
	b, err := h.Batches.Get(sid, c.Param("id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if b == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "image batch not found"})
		return
	}
	if b.Status != "collecting" {
		c.JSON(http.StatusConflict, gin.H{"error": fmt.Sprintf("batch status is %s", b.Status)})
		return
	}
	if b.AssetCount == 0 {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": "upload at least one image before rendering"})
		return
	}
	if err := h.ensureBatchSession(sid, b); err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	if h.Semaphore != nil && !h.Semaphore.TryAcquireBatch(sid, b.ID) {
		c.JSON(http.StatusTooManyRequests, gin.H{"error": "render capacity is full; retry shortly"})
		return
	}
	aspectRatio := "9:16"
	if b.ScriptID == "ecommerce-product-demo" {
		aspectRatio = "16:9"
	}
	res, err := h.Sessions.CallBatch(sid, b.ID, b.ProjectID, "create_remotion_video_share", map[string]interface{}{
		"project_id": b.ProjectID, "script_id": b.ScriptID, "title": "帧流作品 " + b.ID,
		"duration_per_image": 3.0, "aspect_ratio": aspectRatio,
	})
	if err != nil {
		if h.Semaphore != nil {
			h.Semaphore.ReleaseBatch(b.ID)
		}
		h.Batches.Update(sid, b.ID, func(x *imagebatch.Batch) { x.Status = "failed"; x.Error = err.Error() })
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	jobID := digString(res, "render_job_id")
	if jobID == "" || res["success"] == false {
		if h.Semaphore != nil {
			h.Semaphore.ReleaseBatch(b.ID)
		}
		c.JSON(http.StatusBadGateway, gin.H{"error": "upstream render submission failed", "result": res})
		return
	}
	if _, updateErr := h.Batches.Update(sid, b.ID, func(x *imagebatch.Batch) { x.Status = "rendering"; x.RenderJobID = jobID }); updateErr != nil {
		if h.Semaphore != nil {
			h.Semaphore.ReleaseBatch(b.ID)
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": updateErr.Error()})
		return
	}
	h.Sessions.RecordJob(sid, mcp.RenderJob{JobID: jobID, BatchID: b.ID, ProjectID: b.ProjectID, Name: "帧流作品 " + b.ID, Res: aspectRatio, Status: "渲染中", CreatedAt: time.Now()})
	// This batch has been closed by a successful render submission. Reset the
	// per-submission counter so a later batch starts with its own tier quota.
	h.Sessions.ResetAsset(sid)
	c.JSON(http.StatusAccepted, gin.H{"batch_id": b.ID, "render_job_id": jobID, "status": "rendering"})
}
