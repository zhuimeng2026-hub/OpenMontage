package handlers

import (
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/composition"
	"frameflow-bff/internal/config"
	"frameflow-bff/internal/mcp"
)

// CompositionHandler exposes the FrameFlow editor's save/manage/render surface
// for user-authored Remotion compositions (TSX source).
type CompositionHandler struct {
	Cfg      *config.Config
	Comps    *composition.Store
	Sessions *mcp.SessionStore
}

func NewCompositionHandler(cfg *config.Config, comps *composition.Store, sessions *mcp.SessionStore) *CompositionHandler {
	return &CompositionHandler{Cfg: cfg, Comps: comps, Sessions: sessions}
}

func (h *CompositionHandler) ensureSession(c *gin.Context) string {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		return sid
	}
	sid := randHex(16)
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(sessionCookieName, sid, 60*60*24*7, "/", "", h.Cfg.SessionSecure, true)
	return sid
}

// Create saves a new/updated composition for the session.
func (h *CompositionHandler) Create(c *gin.Context) {
	var req struct {
		Name string `json:"name"`
		Code string `json:"code"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
		return
	}
	if req.Name == "" {
		req.Name = "未命名合成"
	}
	if req.Code == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "code is required"})
		return
	}
	sid := h.ensureSession(c)
	comp := h.Comps.Save(sid, req.Name, req.Code)
	c.JSON(http.StatusOK, gin.H{
		"id":         comp.ID,
		"name":       comp.Name,
		"created_at": comp.CreatedAt,
		"updated_at": comp.UpdatedAt,
	})
}

// List returns the session's compositions.
func (h *CompositionHandler) List(c *gin.Context) {
	sid := h.ensureSession(c)
	c.JSON(http.StatusOK, gin.H{"compositions": h.Comps.List(sid)})
}

// Get returns one composition by id (owned by the session).
func (h *CompositionHandler) Get(c *gin.Context) {
	sid := h.ensureSession(c)
	id := c.Param("id")
	comp := h.Comps.Get(sid, id)
	if comp == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "composition not found"})
		return
	}
	c.JSON(http.StatusOK, comp)
}

// Render submits a composition for rendering.
//
// The upstream dw.aixifs.com/mcp `create_remotion_video_share` tool currently
// accepts only project_id / duration_per_image / aspect_ratio / title and does
// NOT accept custom composition source. Until that changes, custom-code
// rendering is gated behind CUSTOM_COMPOSITION_ENABLED: when off we return 501
// with an explicit note so the UI never fakes success. When on, we forward the
// source as a `code` argument (forward-compatible) to the same MCP session that
// already holds the uploaded assets.
func (h *CompositionHandler) Render(c *gin.Context) {
	sid := h.ensureSession(c)
	id := c.Param("id")
	comp := h.Comps.Get(sid, id)
	if comp == nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "composition not found"})
		return
	}
	var req struct {
		Title            string `json:"title"`
		AspectRatio      string `json:"aspect_ratio"`
		DurationPerImage int    `json:"duration_per_image"`
	}
	_ = c.ShouldBindJSON(&req)
	if req.AspectRatio == "" {
		req.AspectRatio = "9:16"
	}
	if req.DurationPerImage <= 0 {
		// Short-form defaults: three seconds per image keeps an 8-image
		// composition in the ~30 second range instead of producing a
		// multi-minute video.
		req.DurationPerImage = 3
	}

	if !h.Cfg.CustomCompositionEnabled {
		c.JSON(http.StatusNotImplemented, gin.H{
			"error": "custom composition rendering is not enabled",
			"note":  "上游 MCP 暂不支持接收自定义合成代码；将 CUSTOM_COMPOSITION_ENABLED=true 并在上游 create_remotion_video_share 增加 code 入参后即可点亮真实渲染。当前仅保存了合成，未提交渲染。",
		})
		return
	}

	args := map[string]interface{}{
		"title":              req.Title,
		"aspect_ratio":       req.AspectRatio,
		"duration_per_image": req.DurationPerImage,
		"code":               comp.Code, // forward-compatible; ignored by upstream until supported
		"queue_owner_id":     renderQueueOwnerID(sid),
	}
	res, err := h.Sessions.Call(sid, "create_remotion_video_share", args)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": err.Error()})
		return
	}
	// A successful render submission enters the caller's own render queue.
	if jobID := digString(res, "render_job_id"); jobID != "" {
		name := req.Title
		if name == "" {
			name = comp.Name
		}
		jobStatus := "渲染中"
		if mapUpstreamStatus(digString(res, "status")) == "排队" {
			jobStatus = "排队"
		}
		h.Sessions.RecordJob(sid, mcp.RenderJob{
			JobID:     jobID,
			Name:      name,
			Res:       req.AspectRatio,
			Status:    jobStatus,
			CreatedAt: time.Now(),
		})
	}
	c.JSON(http.StatusOK, res)
}
