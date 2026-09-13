package auth

import (
	"database/sql"
	"encoding/json"
	"errors"
	"net/http"
	"os"
	"strings"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
)

// JWTService bundles deps for the JWT-based login flow.
type JWTService struct {
	Cfg *config.Config
	DB  *sql.DB
}

func NewJWTService(cfg *config.Config, db *sql.DB) *JWTService {
	return &JWTService{Cfg: cfg, DB: db}
}

// JWTSecret returns the secret bytes used for HS256 signing.
// MVP rule: explicit JWT_SECRET env → fall back to SHA256("MVP_DEV:" + WechatAppID + AppSecret).
func (s *JWTService) JWTSecret() []byte {
	if v := os.Getenv("JWT_SECRET"); v != "" {
		return []byte(v)
	}
	seed := "MVP_DEV:" + s.Cfg.WechatAppID + ":" + s.Cfg.WechatAppSecret
	return []byte(SHA256Hex(seed))
}

// MockOpenIDForCode returns a deterministic mock openid when WEIXIN_MOCK_AUTH=1.
// Format: "mock_openid_<code>". Lets gate.sh smoke-test without real WeChat IdP.
func MockOpenIDForCode(code string) string {
	return "mock_openid_" + code
}

// LoginRequest is the POST /api/auth/login body.
type LoginRequest struct {
	Code string `json:"code" binding:"required"`
}

// LoginResponse is what Login returns.
type LoginResponse struct {
	Token           string `json:"token"`
	InternalUserID  string `json:"internal_user_id"`
	ExpiresInSec    int64  `json:"expires_in"`
}

// Login handles POST /api/auth/login — code → openid (mocked or real) → JWT.
func (s *JWTService) Login(c *gin.Context) {
	var req LoginRequest
	if err := c.ShouldBindJSON(&req); err != nil || req.Code == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "code required"})
		return
	}

	var openid string
	if os.Getenv("WEIXIN_MOCK_AUTH") == "1" {
		// Mock mode for smoke tests + dev without real WeChat AppID.
		openid = MockOpenIDForCode(req.Code)
	} else {
		// Real WeChat exchange — defer to existing OAuth client. If WeChat
		// isn't configured, refuse (fail-closed) rather than silently mock.
		if s.Cfg.WechatAppID == "" || s.Cfg.WechatAppSecret == "" {
			c.JSON(http.StatusServiceUnavailable, gin.H{"error": "wechat not configured and WEIXIN_MOCK_AUTH not set"})
			return
		}
		// Stub: in a fuller impl, call wechat.Code2OpenID(req.Code). For Phase 0
		// MVP we 503 — gate.sh runs with WEIXIN_MOCK_AUTH=1.
		c.JSON(http.StatusNotImplemented, gin.H{"error": "real WeChat code exchange not wired in Phase 0 — set WEIXIN_MOCK_AUTH=1"})
		return
	}

	// Upsert wechat_users by openid; mint internal_user_id if absent.
	iu, err := s.upsertUser(openid)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "user upsert failed: " + err.Error()})
		return
	}

	tok, err := Sign(s.JWTSecret(), iu, openid, 24*time.Hour)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "sign failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, LoginResponse{
		Token:          tok,
		InternalUserID: iu,
		ExpiresInSec:   int64((24 * time.Hour).Seconds()),
	})
}

func (s *JWTService) upsertUser(openid string) (string, error) {
	// Look up existing internal_user_id
	var iu string
	err := s.DB.QueryRow(`SELECT internal_user_id FROM wechat_users WHERE openid = ? LIMIT 1`, openid).Scan(&iu)
	if err == nil && iu != "" {
		return iu, nil
	}
	if err != nil && !errors.Is(err, sql.ErrNoRows) {
		return "", err
	}
	// No row OR row missing internal_user_id → mint + insert/update
	newIU := NewInternalUserID()
	if errors.Is(err, sql.ErrNoRows) {
		_, ierr := s.DB.Exec(
			`INSERT INTO wechat_users (ff_sid, openid, internal_user_id, created_at, expires_at)
			 VALUES (?, ?, ?, datetime('now'), datetime('now', '+30 days'))`,
			"jwt_"+newIU, openid, newIU,
		)
		if ierr != nil {
			return "", ierr
		}
	} else {
		_, uerr := s.DB.Exec(`UPDATE wechat_users SET internal_user_id = ? WHERE openid = ?`, newIU, openid)
		if uerr != nil {
			return "", uerr
		}
	}
	return newIU, nil
}

// MeResponse is what /api/me/jwt returns.
type MeResponse struct {
	UserID          string `json:"user_id"`
	InternalUserID  string `json:"internal_user_id"`
	OpenID          string `json:"openid"`
	ExpiresAtUnix   int64  `json:"expires_at"`
}

// Me handles GET /api/me/jwt — Bearer JWT → user fields.
func (s *JWTService) Me(c *gin.Context) {
	tok := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
	tok = strings.TrimSpace(tok)
	if tok == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing bearer token"})
		return
	}
	claims, err := Verify(s.JWTSecret(), tok)
	if err != nil {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "invalid token: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, MeResponse{
		UserID:         "wechat:" + claims.OpenID,
		InternalUserID: claims.InternalUserID,
		OpenID:         claims.OpenID,
		ExpiresAtUnix:  claims.ExpiresAt,
	})
}

// JWTAuthMiddleware enforces Bearer JWT on protected routes.
// Used by future Phase 1+ handlers; Phase 0 only mounts the public /auth/login + /me/jwt.
func (s *JWTService) JWTAuthMiddleware() gin.HandlerFunc {
	return func(c *gin.Context) {
		tok := strings.TrimPrefix(c.GetHeader("Authorization"), "Bearer ")
		tok = strings.TrimSpace(tok)
		if tok == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "missing bearer token"})
			return
		}
		claims, err := Verify(s.JWTSecret(), tok)
		if err != nil {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{"error": "invalid token"})
			return
		}
		c.Set("internal_user_id", claims.InternalUserID)
		c.Set("openid", claims.OpenID)
		c.Next()
	}
}

// DumpSecretForDebug only used by gate.sh to verify secret deterministic seeding in dev.
func (s *JWTService) DumpSecretSHA256() string {
	return SHA256Hex(string(s.JWTSecret()))
}

// HealthCheck is a no-deps liveness route used by gate.sh to know the server is up.
func HealthCheck(c *gin.Context) {
	c.JSON(http.StatusOK, gin.H{"ok": true, "service": "frameflow-bff", "phase0_mvp": true})
}

// ProfileJSON is a tiny helper to keep login_test.go minimal — exported for tests.
func ProfileJSON(v any) string {
	b, _ := json.Marshal(v)
	return string(b)
}
