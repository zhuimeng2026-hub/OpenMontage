package handlers

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"sync"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
)

const sessionCookieName = "ff_sid"

// Handlers bundles the BFF dependencies.
type Handlers struct {
	Cfg          *config.Config
	Store        *mcp.SessionStore
	Limits       limits.Resolver
	RateLimit    *RateLimiter
	ImageBatches *imagebatch.Store
}

func New(cfg *config.Config, store *mcp.SessionStore, lim limits.Resolver, batches *imagebatch.Store) *Handlers {
	return &Handlers{
		Cfg:          cfg,
		Store:        store,
		Limits:       lim,
		RateLimit:    NewRateLimiter(cfg.RateLimitPerMin),
		ImageBatches: batches,
	}
}

// RequireAuth gates the expensive upstream-facing routes. It is a no-op when
// AUTH_REQUIRED is false. When enabled, it requires a logged-in WeChat session
// (stored against the ff_sid cookie). If the IdP (WechatAppID) is not
// configured there is no way to authenticate, so we degrade to open with a
// startup warning — set AUTH_REQUIRED=true AND configure WeChat before launch.
func (h *Handlers) RequireAuth() gin.HandlerFunc {
	return func(c *gin.Context) {
		if !h.Cfg.AuthRequired {
			c.Next()
			return
		}
		if h.Cfg.WechatAppID == "" || h.Cfg.WechatAppSecret == "" {
			// Fail closed. A production deployment with authentication enabled but
			// no configured IdP must never silently become anonymous.
			c.AbortWithStatusJSON(http.StatusServiceUnavailable, gin.H{"error": "authentication provider is not configured"})
			return
		}
		sid, err := c.Cookie(sessionCookieName)
		if err != nil || sid == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
			return
		}
		if h.loadUser(sid) == nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "authentication required"})
			return
		}
		c.Next()
	}
}

// ensureSession returns the BFF session id, creating + setting a cookie on first
// use. The cookie is what binds a browser to its dedicated MCP client (and to
// the WeChat user info once logged in).
func (h *Handlers) ensureSession(c *gin.Context) string {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		return sid
	}
	sid := randHex(16)
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(sessionCookieName, sid, 60*60*24*7, "/", "", h.Cfg.SessionSecure, true)
	return sid
}

func randHex(n int) string {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "fallback"
	}
	return hex.EncodeToString(b)
}

// in-memory user store (swap for Redis in multi-instance deploys)
var userStore = struct {
	sync.RWMutex
	m map[string]map[string]interface{}
}{m: make(map[string]map[string]interface{})}

func (h *Handlers) saveUser(sid string, u map[string]interface{}) {
	userStore.Lock()
	userStore.m[sid] = u
	userStore.Unlock()
}

func (h *Handlers) loadUser(sid string) map[string]interface{} {
	userStore.RLock()
	defer userStore.RUnlock()
	return userStore.m[sid]
}

func (h *Handlers) dropUser(sid string) {
	userStore.Lock()
	delete(userStore.m, sid)
	userStore.Unlock()
}

// DevLogin bootstraps a logged-in BFF session WITHOUT a real WeChat IdP. It
// exists ONLY to verify per-user queue isolation on a dev machine that has no
// configured WeChat service account — it sets the same user record that
// WechatCallback would, so RequireAuth() treats the session as authenticated.
//
// It is a no-op (404) unless ALL of the following hold, so it can never be
// enabled in production by accident:
//   - AUTH_REQUIRED=true   (otherwise there is nothing to prove)
//   - DEV_LOGIN_ALLOWED=true (explicit opt-in; defaults to false)
//   - WECHAT_APP_ID is set (so RequireAuth would otherwise demand a real login)
func (h *Handlers) DevLogin(c *gin.Context) {
	if !h.Cfg.AuthRequired || !h.Cfg.DevLoginAllowed || h.Cfg.WechatAppID == "" {
		c.JSON(http.StatusNotFound, gin.H{"error": "dev login disabled"})
		return
	}
	name := c.Query("as")
	if name == "" {
		name = "dev-user"
	}
	sid := h.ensureSession(c)
	user := map[string]interface{}{
		"openid":        "dev-" + sid,
		"nickname":      name,
		"authenticated": true,
		"dev":           true,
	}
	h.saveUser(sid, user)
	c.JSON(http.StatusOK, gin.H{
		"authenticated": true,
		"user":          user,
		"note":          "DEV-ONLY session; set DEV_LOGIN_ALLOWED=false before deploying",
	})
}
