package handlers

import (
	"fmt"
	"log"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/mcp"
)

// Named scripts are deliberately server-side choices. They map to trusted
// Remotion compositions; arbitrary browser-supplied TSX is not executed.
var imageScripts = map[string]string{
	"photo-ken-burns":        "照片运镜",
	"cinematic-montage":      "电影混剪",
	"ecommerce-product-demo": "电商产品演示",
}

func validateImageCount(count int) error {
	if count < imagebatch.MinBatchImages || count > imagebatch.MaxBatchImages {
		return fmt.Errorf("image batch requires %d to %d images", imagebatch.MinBatchImages, imagebatch.MaxBatchImages)
	}
	return nil
}

type ImageBatchHandler struct {
	Batches  *imagebatch.Store
	Sessions *mcp.SessionStore
	Cfg      *config.Config
}

// ensureBatchSession restores the dedicated upstream MCP session after a BFF
// restart. Durable batch metadata is enough to recreate the client lazily.
func (h *ImageBatchHandler) ensureBatchSession(scope string, b *imagebatch.Batch) error {
	return h.Sessions.CreateBatch(scope, b.ID, b.ProjectID)
}

func NewImageBatchHandler(cfg *config.Config, batches *imagebatch.Store, sessions *mcp.SessionStore) *ImageBatchHandler {
	return &ImageBatchHandler{Cfg: cfg, Batches: batches, Sessions: sessions}
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
	// Scope the upstream session + batch metadata by the stable WeChat identity
	// (or device session when anonymous) so a user's image batches are consistent
	// across machines.
	scope := renderQueueOwnerID(sid)
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

	// A brand-new submission must start with the full upload quota. The
	// session-wide counter only resets on a successful render, so without this a
	// user who abandoned a broken batch (or retried several times) would carry
	// stale "used" counts into the next batch and hit the 422 quota wall before
	// reaching the required minimum of 5 images.
	h.Sessions.ResetAsset(scope)

	id := "batch-" + randHex(12)
	projectID := "frameflow-batch-" + id
	t0 := time.Now()
	if err := h.Sessions.CreateBatch(scope, id, projectID); err != nil {
		log.Printf("[image-batch] create_session_failed batch_id=%s project_id=%s sid_hash=%s elapsed_ms=%d err=%v", id, projectID, mcp.ShortHashForLog(sid), time.Since(t0).Milliseconds(), err)
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	log.Printf("[image-batch] create_session_ok batch_id=%s project_id=%s sid_hash=%s elapsed_ms=%d", id, projectID, mcp.ShortHashForLog(sid), time.Since(t0).Milliseconds())
	b, err := h.Batches.Create(scope, id, projectID, req.ScriptID)
	if err != nil {
		h.Sessions.DropBatch(scope, id, projectID)
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusCreated, b)
}

func (h *ImageBatchHandler) List(c *gin.Context) {
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	batches, err := h.Batches.List(scope)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"batches": batches})
}

func (h *ImageBatchHandler) Get(c *gin.Context) {
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	b, err := h.Batches.Get(scope, c.Param("id"))
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if b == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "image batch not found"})
		return
	}
	if b.RenderJobID != "" && (b.Status == "queued" || b.Status == "rendering" || b.Status == "collecting") {
		if err := h.ensureBatchSession(scope, b); err != nil {
			c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
			return
		}
		if res, err := h.Sessions.CallBatch(scope, b.ID, b.ProjectID, "get_render_status", map[string]interface{}{"render_job_id": b.RenderJobID}); err == nil {
			status := strings.ToLower(strings.TrimSpace(digString(res, "status")))
			switch status {
			case "queued", "queue", "pending", "waiting":
				h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "queued" })
			case "rendering", "running", "processing", "in_progress", "progress":
				h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "rendering" })
			case "published", "done", "success", "succeeded", "completed", "finished":
				shareURL := digString(res, "share_url")
				if validHTTPURL(shareURL) {
					h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "published"; x.VideoURL = shareURL })
					h.Sessions.UpdateJobResult(scope, b.RenderJobID, "已完成", shareURL)
					h.Sessions.DropBatch(scope, b.ID, b.ProjectID)
				} else {
					h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "failed"; x.Error = "微云分享链接缺失" })
					h.Sessions.UpdateJobResult(scope, b.RenderJobID, "失败", "")
					h.Sessions.DropBatch(scope, b.ID, b.ProjectID)
				}
			case "failed", "error":
				h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "failed"; x.Error = digString(res, "error") })
				h.Sessions.UpdateJobResult(scope, b.RenderJobID, "失败", "")
				h.Sessions.DropBatch(scope, b.ID, b.ProjectID)
			}
		}
	}
	current, err := h.Batches.Get(scope, b.ID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	c.JSON(http.StatusOK, current)
}

func (h *ImageBatchHandler) Render(c *gin.Context) {
	sid := h.ensureSession(c)
	// Scope the upstream session + batch metadata by the stable WeChat identity
	// (or device session when anonymous) so a user's image batches and their
	// rendered videos are consistent across machines.
	scope := renderQueueOwnerID(sid)
	b, err := h.Batches.Get(scope, c.Param("id"))
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
	if err := validateImageCount(b.AssetCount); err != nil {
		c.JSON(http.StatusUnprocessableEntity, gin.H{"error": err.Error(), "asset_count": b.AssetCount, "min": imagebatch.MinBatchImages, "max": imagebatch.MaxBatchImages})
		return
	}
	if err := h.ensureBatchSession(scope, b); err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	aspectRatio := "9:16"
	if b.ScriptID == "ecommerce-product-demo" {
		aspectRatio = "16:9"
	}
	res, err := h.Sessions.CallBatch(scope, b.ID, b.ProjectID, "create_remotion_video_share", map[string]interface{}{
		"project_id": b.ProjectID, "script_id": b.ScriptID, "title": "帧流作品 " + b.ID,
		"duration_per_image": 60.0 / float64(b.AssetCount), "aspect_ratio": aspectRatio,
		"queue_owner_id": scope,
	})
	if err != nil {
		log.Printf("[image-batch] render_submit_failed batch_id=%s project_id=%s script_id=%s sid_hash=%s err=%v", b.ID, b.ProjectID, b.ScriptID, mcp.ShortHashForLog(sid), err)
		h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "failed"; x.Error = err.Error() })
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	jobID := digString(res, "render_job_id")
	if jobID == "" || res["success"] == false {
		c.JSON(http.StatusBadGateway, gin.H{"error": "upstream render submission failed", "result": res})
		return
	}
	if _, updateErr := h.Batches.Update(scope, b.ID, func(x *imagebatch.Batch) { x.Status = "queued"; x.RenderJobID = jobID }); updateErr != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": updateErr.Error()})
		return
	}
	h.Sessions.RecordJob(scope, mcp.RenderJob{JobID: jobID, BatchID: b.ID, ProjectID: b.ProjectID, Name: "帧流作品 " + b.ID, Res: aspectRatio, Status: "排队", CreatedAt: time.Now()})
	// This batch has been closed by a successful render submission. Reset the
	// per-submission counter so a later batch starts with its own tier quota.
	h.Sessions.ResetAsset(scope)
	c.JSON(http.StatusAccepted, gin.H{"batch_id": b.ID, "render_job_id": jobID, "status": "queued"})
}
