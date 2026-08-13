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
	Cfg   *config.Config
	Store *mcp.SessionStore
}

func New(cfg *config.Config, store *mcp.SessionStore) *Handlers {
	return &Handlers{Cfg: cfg, Store: store}
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
