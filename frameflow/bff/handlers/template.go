package handlers

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"io"
	"net/http"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/business"
	"frameflow-bff/internal/config"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
	"frameflow-bff/internal/template"
)

// TemplateHandler exposes the batch-render surface: a reusable Template (the
// "fixed script") plus Scenarios (per-scenario image sets pulled from the
// business system) and a BatchRender that fans out to N upstream renders.
//
// Pipeline per scenario (run async in a goroutine):
//
//	download bytes -> upload_asset_chunk into project_base-<scenarioID>
//	-> create_remotion_video_share(project_id) -> poll get_render_status
//	-> record share_url.
//
// (Per-scenario image refs are resolved up-front in BatchRender so the
// single-submission file cap can be enforced before any upstream work starts.)
type TemplateHandler struct {
	Cfg       *config.Config
	Templates *template.Store
	Sessions  *mcp.SessionStore
	Fetcher   business.Fetcher
	Limits    limits.Resolver
	Usage     *limits.Usage
}

func NewTemplateHandler(cfg *config.Config, tpls *template.Store, sessions *mcp.SessionStore, fetcher business.Fetcher, lim limits.Resolver, usage *limits.Usage) *TemplateHandler {
	return &TemplateHandler{Cfg: cfg, Templates: tpls, Sessions: sessions, Fetcher: fetcher, Limits: lim, Usage: usage}
}

// ensureSession returns the BFF session id, creating + setting the ff_sid cookie
// on first use. Same cookie contract as Handlers.ensureSession /
// CompositionHandler.ensureSession — it is what binds a browser to its dedicated
// MCP client (and to WeChat user info once logged in).
func (h *TemplateHandler) ensureSession(c *gin.Context) string {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		return sid
	}
	sid := randHex(16)
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(sessionCookieName, sid, 60*60*24*7, "/", "", h.Cfg.SessionSecure, true)
	return sid
}

// ---------- Template CRUD ----------

func (h *TemplateHandler) CreateTemplate(c *gin.Context) {
	sid := h.ensureSession(c)
	var req struct {
		Name             string `json:"name"`
		ProjectBase      string `json:"project_base"`
		AspectRatio      string `json:"aspect_ratio"`
		DurationPerImage int    `json:"duration_per_image"`
		TitleTemplate    string `json:"title_template"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body"})
		return
	}
	if req.Name == "" {
		req.Name = "未命名模板"
	}
	if req.ProjectBase == "" {
		req.ProjectBase = "ff-" + sid
	}
	if req.AspectRatio == "" {
		req.AspectRatio = "9:16"
	}
	if req.DurationPerImage <= 0 {
		req.DurationPerImage = 3
	}
	t := h.Templates.SaveTemplate(sid, req.Name, req.ProjectBase, req.AspectRatio, req.TitleTemplate, req.DurationPerImage)
	c.JSON(http.StatusOK, t)
}

func (h *TemplateHandler) ListTemplates(c *gin.Context) {
	sid := h.ensureSession(c)
	c.JSON(http.StatusOK, gin.H{"templates": h.Templates.ListTemplates(sid)})
}

func (h *TemplateHandler) GetTemplate(c *gin.Context) {
	sid := h.ensureSession(c)
	t := h.Templates.GetTemplate(sid, c.Param("id"))
	if t == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "template not found"})
		return
	}
	c.JSON(http.StatusOK, t)
}

// ---------- Scenarios ----------

func (h *TemplateHandler) AddScenario(c *gin.Context) {
	sid := h.ensureSession(c)
	t := h.Templates.GetTemplate(sid, c.Param("id"))
	if t == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "template not found"})
		return
	}
	var req struct {
		BusinessKey string `json:"business_key"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.BusinessKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "business_key required"})
		return
	}
	sc := h.Templates.AddScenario(sid, t.ID, req.BusinessKey)
	c.JSON(http.StatusOK, sc)
}

func (h *TemplateHandler) ListScenarios(c *gin.Context) {
	sid := h.ensureSession(c)
	t := h.Templates.GetTemplate(sid, c.Param("id"))
	if t == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "template not found"})
		return
	}
	c.JSON(http.StatusOK, gin.H{"scenarios": h.Templates.ListScenarios(sid, t.ID)})
}

// ---------- Batch render ----------

// BatchRender kicks off an async job that renders every scenario under the
// template. It enforces the user's tier quota BEFORE any upstream work:
//   - a concurrent + daily slot must be available (else 429 + Retry-After)
//   - the total image files across this submission must fit the tier cap
//     (else 422). Image refs are resolved up-front so the cap is a clean
//     pre-check and the worker never re-fetches.
func (h *TemplateHandler) BatchRender(c *gin.Context) {
	sid := h.ensureSession(c)
	t := h.Templates.GetTemplate(sid, c.Param("id"))
	if t == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "template not found"})
		return
	}
	scenarios := h.Templates.ListScenarios(sid, t.ID)
	if len(scenarios) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "no scenarios to render — add at least one first"})
		return
	}

	// 1) resolve image refs up-front and enforce the single-submission file cap
	//    BEFORE reserving a quota slot, so a failed/over-cap submission does not
	//    burn a daily task.
	ctx := c.Request.Context()
	tier := h.Limits.Resolve(sid)
	lim := limits.ForTier(tier)
	totalFiles := 0
	for _, sc := range scenarios {
		imgs, err := h.Fetcher.Fetch(ctx, sc.BusinessKey)
		if err != nil {
			c.JSON(http.StatusUnprocessableEntity, gin.H{
				"error": "fetch images for scenario " + sc.BusinessKey + " failed: " + err.Error(),
			})
			return
		}
		h.Templates.SetScenarioImages(sid, sc.ID, imgs)
		totalFiles += len(imgs)
	}
	if totalFiles > lim.MaxFilesPerSubmission {
		c.JSON(http.StatusUnprocessableEntity, gin.H{
			"error": fmt.Sprintf(
				"this submission has %d image files but your %q tier allows at most %d per submission",
				totalFiles, tier, lim.MaxFilesPerSubmission),
			"files": totalFiles,
		})
		return
	}

	// 2) reserve a quota slot (concurrent + daily). Fails with 429 + Retry-After
	//    when the tier's concurrent or daily cap is already reached.
	snap, ok := h.Usage.Acquire(sid, tier, lim)
	if !ok {
		c.Header("Retry-After", retryAfter(snap))
		c.JSON(http.StatusTooManyRequests, gin.H{
			"error":       quotaError(snap),
			"quota":       snap,
			"retry_after": retryAfter(snap),
		})
		return
	}
	ids := make([]string, 0, len(scenarios))
	for _, sc := range scenarios {
		ids = append(ids, sc.ID)
	}
	job := h.Templates.CreateBatchJob(sid, t.ID, ids)
	// The HTTP handler returns immediately, so the slot must be released by the
	// background worker rather than by a handler defer. Otherwise every request
	// releases its slot before the batch has rendered anything.
	go func() {
		defer h.Usage.Release(sid)
		h.runBatch(sid, t, scenarios, job.ID)
	}()
	c.JSON(http.StatusAccepted, gin.H{
		"job_id":               job.ID,
		"status":               "running",
		"scenarios":            len(scenarios),
		"files":                totalFiles,
		"tier":                 tier,
		"daily_remaining":      snap.DailyRemaining,
		"concurrent_remaining": snap.ConcurrentRemaining,
	})
}

func (h *TemplateHandler) GetBatchJob(c *gin.Context) {
	sid := h.ensureSession(c)
	job := h.Templates.GetBatchJob(sid, c.Param("jobId"))
	if job == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "job not found"})
		return
	}
	c.JSON(http.StatusOK, job)
}

// GetQuota reports the current user's tier and live usage so the UI can show
// remaining capacity and gate the submit button.
func (h *TemplateHandler) GetQuota(c *gin.Context) {
	sid := h.ensureSession(c)
	tier := h.Limits.Resolve(sid)
	lim := limits.ForTier(tier)
	snap := h.Usage.Inspect(sid, tier, lim)
	c.JSON(http.StatusOK, snap)
}

// runBatch is the async worker: one scenario at a time, upload -> render -> poll.
// Image refs are already resolved on each scenario (set by BatchRender).
func (h *TemplateHandler) runBatch(sid string, t *template.Template, scenarios []*template.Scenario, jobID string) {
	ctx := context.Background()
	for _, sc := range scenarios {
		imgs := sc.ImageRefs
		if len(imgs) == 0 {
			h.Templates.SetScenarioStatus(sid, sc.ID, "failed", "no images resolved for scenario")
			h.Templates.SetJobOutput(sid, jobID, sc.ID, "")
			continue
		}
		h.Templates.SetScenarioStatus(sid, sc.ID, "uploading", "")
		projectID := t.ProjectBase + "-" + sc.ID
		uploadOK := true
		for _, img := range imgs {
			data, derr := downloadBytes(ctx, img.URL, img.Headers)
			if derr != nil {
				h.Templates.SetScenarioStatus(sid, sc.ID, "failed", "download "+img.URL+": "+derr.Error())
				uploadOK = false
				break
			}
			if uerr := uploadImageBytes(h.Sessions, sid, projectID, img.Name, data); uerr != nil {
				h.Templates.SetScenarioStatus(sid, sc.ID, "failed", "upload: "+uerr.Error())
				uploadOK = false
				break
			}
		}
		if !uploadOK {
			h.Templates.SetJobOutput(sid, jobID, sc.ID, "")
			continue
		}

		h.Templates.SetScenarioStatus(sid, sc.ID, "rendering", "")
		title := t.TitleTemplate
		if title == "" {
			title = sc.BusinessKey
		} else {
			title = strings.ReplaceAll(title, "{{business_key}}", sc.BusinessKey)
		}
		res, rerr := h.Sessions.Call(sid, "create_remotion_video_share", map[string]interface{}{
			"project_id":         projectID,
			"duration_per_image": t.DurationPerImage,
			"aspect_ratio":       t.AspectRatio,
			"title":              title,
			"queue_owner_id":     renderQueueOwnerID(sid),
		})
		if rerr != nil {
			h.Templates.SetScenarioStatus(sid, sc.ID, "failed", "render submit: "+rerr.Error())
			h.Templates.SetJobOutput(sid, jobID, sc.ID, "")
			continue
		}
		renderJobID := digString(res, "render_job_id")
		if renderJobID == "" {
			h.Templates.SetScenarioStatus(sid, sc.ID, "failed", "no render_job_id in upstream response")
			h.Templates.SetJobOutput(sid, jobID, sc.ID, "")
			continue
		}
		h.Templates.SetScenarioRenderJob(sid, sc.ID, renderJobID)

		// Each batch-rendered scenario is also an entry in the caller's own
		// render queue (scoped by the BFF session, so never cross-visible).
		jobStatus := "渲染中"
		if mapUpstreamStatus(digString(res, "status")) == "排队" {
			jobStatus = "排队"
		}
		h.Sessions.RecordJob(sid, mcp.RenderJob{
			JobID:     renderJobID,
			Name:      title,
			Res:       t.AspectRatio,
			Status:    jobStatus,
			CreatedAt: time.Now(),
		})

		videoURL, perr := h.pollRender(sid, renderJobID)
		if perr != nil {
			h.Templates.SetScenarioStatus(sid, sc.ID, "failed", perr.Error())
			h.Templates.SetJobOutput(sid, jobID, sc.ID, "")
			continue
		}
		h.Templates.SetScenarioStatus(sid, sc.ID, "done", "")
		h.Templates.SetScenarioVideo(sid, sc.ID, videoURL)
		h.Templates.SetJobOutput(sid, jobID, sc.ID, videoURL)
	}
	h.Templates.SetJobDone(sid, jobID)
}

// retryAfter returns a Retry-After value (seconds) tuned to which cap was hit:
// daily exhaustion waits until midnight; concurrency exhaustion polls soon.
func retryAfter(snap limits.Snapshot) string {
	if snap.DailyRemaining <= 0 {
		now := time.Now()
		midnight := time.Date(now.Year(), now.Month(), now.Day()+1, 0, 0, 0, 0, now.Location())
		return fmt.Sprintf("%d", int(midnight.Sub(now).Seconds()))
	}
	return "30"
}

func quotaError(snap limits.Snapshot) string {
	if snap.DailyRemaining <= 0 {
		return fmt.Sprintf("daily render-task limit reached for tier %q (%d/day); resets at midnight", snap.Tier, snap.MaxRenderTasksPerDay)
	}
	return fmt.Sprintf("concurrent render-task limit reached for tier %q (%d at once); retry shortly", snap.Tier, snap.MaxConcurrentTasks)
}

// pollRender waits for a render job to reach "published" (share_url) or "failed".
func (h *TemplateHandler) pollRender(sid, renderJobID string) (string, error) {
	const maxPolls = 180 // 180 * 5s = 15min ceiling
	for i := 0; i < maxPolls; i++ {
		res, err := h.Sessions.Call(sid, "get_render_status", map[string]interface{}{"render_job_id": renderJobID})
		if err != nil {
			return "", err
		}
		switch strings.ToLower(strings.TrimSpace(digString(res, "status"))) {
		case "published", "done", "success", "completed", "finished":
			return digString(res, "share_url"), nil
		case "failed", "error":
			return "", fmt.Errorf("render failed at stage %s", digString(res, "stage"))
		}
		time.Sleep(5 * time.Second)
	}
	return "", fmt.Errorf("render timed out after polling")
}

// ---------- helpers ----------

// uploadImageBytes streams image bytes into the upstream project asset store
// using the resumable chunk protocol (start -> append* -> complete).
func uploadImageBytes(s *mcp.SessionStore, sid, projectID, filename string, data []byte) error {
	sum := sha256.Sum256(data)
	sha := hex.EncodeToString(sum[:])
	uploadID := randHex(16)
	if _, err := s.Call(sid, "upload_asset_chunk", map[string]interface{}{
		"operation":   "start",
		"project_id":  projectID,
		"filename":    filename,
		"total_bytes": len(data),
		"upload_id":   uploadID,
		"sha256":      sha,
	}); err != nil {
		return err
	}
	const chunk = 600000 // raw bytes; base64 (~800KB) stays under the 1 MiB cap
	for off := 0; off < len(data); off += chunk {
		end := off + chunk
		if end > len(data) {
			end = len(data)
		}
		b64 := base64.StdEncoding.EncodeToString(data[off:end])
		if _, err := s.Call(sid, "upload_asset_chunk", map[string]interface{}{
			"operation":    "append",
			"project_id":   projectID,
			"filename":     filename,
			"upload_id":    uploadID,
			"offset":       off,
			"chunk_base64": b64,
		}); err != nil {
			return err
		}
	}
	_, err := s.Call(sid, "upload_asset_chunk", map[string]interface{}{
		"operation":  "complete",
		"project_id": projectID,
		"filename":   filename,
		"upload_id":  uploadID,
	})
	return err
}

// downloadBytes fetches an image's raw bytes (business images must be
// publicly reachable by the BFF).
func downloadBytes(ctx context.Context, url string, headers map[string]string) ([]byte, error) {
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return nil, err
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("http %d", resp.StatusCode)
	}
	return io.ReadAll(resp.Body)
}

// digString pulls a string field from an MCP result, handling both the
// flattened Extract() output and a nested "result" envelope.
func digString(m map[string]interface{}, key string) string {
	if m == nil {
		return ""
	}
	if v, ok := m[key].(string); ok {
		return v
	}
	if r, ok := m["result"].(map[string]interface{}); ok {
		if v, ok := r[key].(string); ok {
			return v
		}
	}
	return ""
}
