package handlers

import (
	"net/http"
	"strconv"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/mcp"
	"frameflow-bff/internal/script"
)

// ScriptHandler exposes the FrameFlow "定义视频脚本" save/manage surface.
// Scripts are scoped to the BFF session (same cookie that binds the MCP client
// and WeChat login), so each caller only sees their own scripts.
type ScriptHandler struct {
	Cfg      *config.Config
	Scripts  *script.Store
	Sessions *mcp.SessionStore
}

func NewScriptHandler(cfg *config.Config, scripts *script.Store, sessions *mcp.SessionStore) *ScriptHandler {
	return &ScriptHandler{Cfg: cfg, Scripts: scripts, Sessions: sessions}
}

func (h *ScriptHandler) ensureSession(c *gin.Context) string {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		return sid
	}
	sid := randHex(16)
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(sessionCookieName, sid, 60*60*24*7, "/", "", h.Cfg.SessionSecure, true)
	return sid
}

// Create saves a video-generation script (name + content) for the session. The
// script is persisted server-side and handed to the backend for rendering use.
func (h *ScriptHandler) Create(c *gin.Context) {
	var req struct {
		Name    string `json:"name"`
		Content string `json:"content"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid request body"})
		return
	}
	if req.Name == "" {
		req.Name = "未命名脚本"
	}
	if req.Content == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "脚本内容不能为空"})
		return
	}
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	s := h.Scripts.Save(scope, req.Name, req.Content)
	c.JSON(http.StatusOK, gin.H{
		"id":         s.ID,
		"key":        s.Key,
		"name":       s.Name,
		"created_at": s.CreatedAt,
		"updated_at": s.UpdatedAt,
	})
}

// List returns the session's saved scripts (newest first), paginated when
// ?limit is provided. total is always the unfiltered count.
func (h *ScriptHandler) List(c *gin.Context) {
	sid := h.ensureSession(c)
	scope := renderQueueOwnerID(sid)
	limit, _ := strconv.Atoi(c.DefaultQuery("limit", "0"))
	offset, _ := strconv.Atoi(c.DefaultQuery("offset", "0"))
	scripts, total := h.Scripts.List(scope, limit, offset)
	c.JSON(http.StatusOK, gin.H{"scripts": scripts, "total": total})
}
