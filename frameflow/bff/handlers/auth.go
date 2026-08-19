package handlers

import (
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"log"
	"net/http"
	"sync"
	"time"

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

func New(cfg *config.Config, store *mcp.SessionStore, lim limits.Resolver, batches *imagebatch.Store, db *sql.DB) *Handlers {
	userDB = db
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

// renderQueueOwnerID returns an opaque, stable fairness key. A WeChat user
// keeps the same key across browser sessions; anonymous/dev flows fall back to
// the BFF session. The raw openid/session cookie is never sent upstream.
func renderQueueOwnerID(sid string) string {
	identity := "session:" + sid
	if u := loadUserMap(sid); u != nil {
		if openid, ok := u["openid"].(string); ok && openid != "" {
			identity = "wechat:" + openid
		}
	}
	sum := sha256.Sum256([]byte(identity))
	return hex.EncodeToString(sum[:])
}

// userStore is the in-memory hot-cache for WeChat login state. It speeds up the
// per-request /api/me probe and the queue-owner lookup; the durable copy lives
// in the wechat_users SQLite table so logins survive BFF restarts and are
// shared across instances in a multi-instance deploy.
var userStore = struct {
	sync.RWMutex
	m map[string]map[string]interface{}
}{m: make(map[string]map[string]interface{})}

// userDB is the shared SQLite handle for durable WeChat login state. New() sets
// it once; a nil handle degrades to in-memory-only (dev / no DB available).
var userDB *sql.DB

// loginStateTTL is how long a WeChat login stays valid before the user must
// re-authenticate. 12h, independent of the ff_sid cookie lifetime, so the
// browser session binding can outlive a single login but the login itself
// expires and forces a fresh scan. The frontend /api/me probe (every 60s)
// detects the flip to authenticated:false and bounces the user to re-login.
const loginStateTTL = 12 * time.Hour

// loadUserMap returns the user record for a session id. It checks the hot cache
// first and falls back to the wechat_users table, so a login stays visible after
// a BFF restart or on another instance. Expired rows are deleted and reported as
// not-logged-in.
func loadUserMap(sid string) map[string]interface{} {
	userStore.RLock()
	if u, ok := userStore.m[sid]; ok {
		userStore.RUnlock()
		// Even a hot-cached login must honour its 12h expiry — otherwise an
		// always-on BFF would never invalidate a session until restart.
		if isExpired(u) {
			dropUserMap(sid)
			return nil
		}
		return u
	}
	userStore.RUnlock()

	if userDB != nil {
		u, err := findPersistedUser(userDB, sid)
		if err != nil {
			log.Printf("[auth] load persisted user ff_sid=%s err=%v", sid, err)
			return nil
		}
		if u != nil {
			if isExpired(u) {
				_ = deletePersistedUser(userDB, sid)
				return nil
			}
			userStore.Lock()
			userStore.m[sid] = u
			userStore.Unlock()
			// Belt-and-braces re-check after the cache write: between findPersistedUser
			// and the lock/unlock above, the persisted row could already be expired
			// (TTL crossed, or another goroutine just deleted it). Without this, an
			// in-flight expiry would land a stale user in the hot cache until the
			// next cache miss. Microsecond cost; locks down the invariant.
			if isExpired(u) {
				dropUserMap(sid)
				return nil
			}
			return u
		}
	}
	return nil
}

func (h *Handlers) saveUser(sid string, u map[string]interface{}) {
	// Stamp the login expiry onto the record itself so the hot cache (which is
	// checked before the DB) enforces loginStateTTL on read — otherwise an
	// active session would dodge expiry until a cache miss.
	u["expires_at"] = time.Now().Add(loginStateTTL).UTC().Format(time.RFC3339Nano)
	userStore.Lock()
	userStore.m[sid] = u
	userStore.Unlock()
	if userDB != nil {
		if err := persistUser(userDB, sid, u); err != nil {
			log.Printf("[auth] persist user ff_sid=%s err=%v", sid, err)
		}
	}
}

func (h *Handlers) loadUser(sid string) map[string]interface{} {
	return loadUserMap(sid)
}

// dropUserMap clears a login from both the hot cache and the durable table. It
// is package-level so loadUserMap can expire sessions on read without needing a
// *Handlers receiver.
func dropUserMap(sid string) {
	userStore.Lock()
	delete(userStore.m, sid)
	userStore.Unlock()
	if userDB != nil {
		_ = deletePersistedUser(userDB, sid)
	}
}

func (h *Handlers) dropUser(sid string) {
	dropUserMap(sid)
}

// ---- durable WeChat login persistence (wechat_users table) ----

func persistUser(db *sql.DB, sid string, u map[string]interface{}) error {
	openid, _ := u["openid"].(string)
	nickname, _ := u["nickname"].(string)
	scope, _ := u["scope"].(string)
	profile, err := json.Marshal(u)
	if err != nil {
		return err
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	expires := time.Now().Add(loginStateTTL).UTC().Format(time.RFC3339Nano)
	_, err = db.Exec(`INSERT INTO wechat_users (ff_sid, openid, nickname, scope, profile_json, created_at, expires_at)
VALUES(?,?,?,?,?,?,?)
ON CONFLICT(ff_sid) DO UPDATE SET
  openid=excluded.openid, nickname=excluded.nickname, scope=excluded.scope,
  profile_json=excluded.profile_json, expires_at=excluded.expires_at`,
		sid, openid, nickname, scope, string(profile), now, expires)
	return err
}

func findPersistedUser(db *sql.DB, sid string) (map[string]interface{}, error) {
	row := db.QueryRow(`SELECT profile_json, expires_at FROM wechat_users WHERE ff_sid=? LIMIT 1`, sid)
	var profile, expires string
	if err := row.Scan(&profile, &expires); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, err
	}
	var u map[string]interface{}
	if err := json.Unmarshal([]byte(profile), &u); err != nil {
		return nil, err
	}
	u["expires_at"] = expires
	return u, nil
}

func deletePersistedUser(db *sql.DB, sid string) error {
	_, err := db.Exec(`DELETE FROM wechat_users WHERE ff_sid=?`, sid)
	return err
}

func isExpired(u map[string]interface{}) bool {
	exp, ok := u["expires_at"].(string)
	if !ok || exp == "" {
		return false
	}
	t, err := time.Parse(time.RFC3339Nano, exp)
	if err != nil {
		return false
	}
	return time.Now().After(t)
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
