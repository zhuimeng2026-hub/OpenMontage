package handlers

import (
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"sync"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/mcp"
)

const sessionCookieName = "ff_sid"

// Handlers bundles the BFF dependencies.
type Handlers struct {
	Cfg       *config.Config
	Store     *mcp.SessionStore
	RateLimit *RateLimiter
}

func New(cfg *config.Config, store *mcp.SessionStore) *Handlers {
	return &Handlers{
		Cfg:       cfg,
		Store:     store,
		RateLimit: NewRateLimiter(cfg.RateLimitPerMin),
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
		if h.Cfg.WechatAppID == "" {
			// dev fallback: IdP not configured, nothing to authenticate against
			c.Next()
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
