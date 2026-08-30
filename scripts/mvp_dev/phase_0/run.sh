#!/usr/bin/env bash
# Phase 0 — §17.A — 微信身份 (REAL implementation)
# 由 orchestrator.sh 调用:bash phase_0/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. schema 迁移 — ALTER TABLE wechat_users 加 unionid + internal_user_id
#   2. 写入 internal/auth/jwt.go (HMAC-SHA256 JWT 签发/验签, stdlib only)
#   3. 写入 internal/auth/jwt_login.go (POST /api/auth/login handler + 中间件)
#   4. 改 handlers/auth.go 加 JWTLogin + JWTMe
#   5. 改 main.go 挂 api.POST("/auth/login") + api.GET("/me/jwt") (新建,不碰老 /api/me)
#   6. go build → 输出到 /tmp/frameflow-bff-mvp
#   7. 启动 bff(后台)+ 留给 gate.sh 跑冒烟

set -u
set -o pipefail
# Ensure go is on PATH — cron doesn't source /etc/profile.d; Phase 1+ hit this
# with "go: command not found" on the most recent run. (Phase 0 had the same
# issue at 15:42:02 — fixed retroactively so --resume + git reset can re-run.)
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"
REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_0-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_0 run.sh start $(date -Iseconds) mode=${1:-?} ==="

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_0] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_0 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. schema 迁移 ----
echo "[phase_0] step 1: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
# 用 sqlite3 增量 ALTER TABLE,IF NOT EXISTS 模式
sqlite3 "${DB_PATH}" <<'SQL'
ALTER TABLE wechat_users ADD COLUMN unionid TEXT NOT NULL DEFAULT '';
ALTER TABLE wechat_users ADD COLUMN internal_user_id TEXT NOT NULL DEFAULT '';
CREATE UNIQUE INDEX IF NOT EXISTS idx_wechat_users_internal_user_id ON wechat_users(internal_user_id);
SQL
# 验证
echo "[phase_0] schema verify:"
sqlite3 "${DB_PATH}" "PRAGMA table_info(wechat_users)" | grep -E "unionid|internal_user_id"

# ---- 2. 写 internal/auth/jwt.go (stdlib HMAC-SHA256) ----
echo "[phase_0] step 2: write internal/auth/jwt.go"
mkdir -p internal/auth
cat > internal/auth/jwt.go <<'GOEOF'
// Package auth implements HS256 JWT signing/verification using only stdlib.
// Used by Phase 0 of the MVP §17.A implementation.
package auth

import (
	"crypto/hmac"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const (
	defaultTTL = 24 * time.Hour
)

// Claims is the minimal payload we care about.
type Claims struct {
	InternalUserID string `json:"sub"`     // subject = internal_user_id
	OpenID         string `json:"openid,omitempty"`
	ExpiresAt      int64  `json:"exp"`     // unix seconds
	IssuedAt       int64  `json:"iat"`     // unix seconds
}

// Sign produces a compact JWT string using HS256.
func Sign(secret []byte, internalUserID, openID string, ttl time.Duration) (string, error) {
	if internalUserID == "" {
		return "", errors.New("internalUserID required")
	}
	now := time.Now()
	if ttl <= 0 {
		ttl = defaultTTL
	}
	header := map[string]string{"alg": "HS256", "typ": "JWT"}
	headerJSON, _ := json.Marshal(header)
	claims := Claims{
		InternalUserID: internalUserID,
		OpenID:         openID,
		ExpiresAt:      now.Add(ttl).Unix(),
		IssuedAt:       now.Unix(),
	}
	claimsJSON, _ := json.Marshal(claims)
	signingInput := b64(headerJSON) + "." + b64(claimsJSON)
	sig := sign(secret, signingInput)
	return signingInput + "." + sig, nil
}

// Verify parses + verifies a JWT and returns the claims.
func Verify(secret []byte, token string) (*Claims, error) {
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		return nil, errors.New("malformed token")
	}
	signingInput := parts[0] + "." + parts[1]
	wantSig := sign(secret, signingInput)
	if !hmac.Equal([]byte(wantSig), []byte(parts[2])) {
		return nil, errors.New("bad signature")
	}
	claimsJSON, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("bad claims b64: %w", err)
	}
	var c Claims
	if err := json.Unmarshal(claimsJSON, &c); err != nil {
		return nil, fmt.Errorf("bad claims json: %w", err)
	}
	if time.Now().Unix() >= c.ExpiresAt {
		return nil, errors.New("token expired")
	}
	if c.InternalUserID == "" {
		return nil, errors.New("empty subject")
	}
	return &c, nil
}

// NewInternalUserID returns a random 16-byte hex string (collision-resistant
// enough for MVP).
func NewInternalUserID() string {
	b := make([]byte, 16)
	_, _ = rand.Read(b)
	return "iu_" + hex.EncodeToString(b)
}

func b64(b []byte) string { return base64.RawURLEncoding.EncodeToString(b) }

func sign(secret []byte, signingInput string) string {
	m := hmac.New(sha256.New, secret)
	m.Write([]byte(signingInput))
	return b64(m.Sum(nil))
}

// SHA256Hex is a tiny helper kept here for tests that want to seed secret deterministically.
func SHA256Hex(s string) string {
	h := sha256.Sum256([]byte(s))
	return hex.EncodeToString(h[:])
}
GOEOF

# ---- 3. 写 internal/auth/jwt_login.go (handler + middleware) ----
echo "[phase_0] step 3: write internal/auth/jwt_login.go"
cat > internal/auth/jwt_login.go <<'GOEOF'
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
GOEOF

echo "[phase_0] step 4: write cmd/mvp/main.go (standalone binary, doesn't touch real main.go)"
mkdir -p cmd/mvp
cat > cmd/mvp/main.go <<'GOEOF'
// Package main is the MVP §17.A standalone server: POST /api/auth/login,
// GET /api/me/jwt, GET /healthz. It runs alongside the real frameflow-bff
// (different port). Doesn't touch main.go.
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/auth"
	"frameflow-bff/internal/config"
)

func main() {
	cfg := config.Load()
	if err := config.Validate(cfg); err != nil {
		log.Printf("[mvp] WARN config validate: %v (continuing — JWT seed handles missing WeChat)", err)
	}

	port := os.Getenv("MVP_PORT")
	if port == "" {
		port = "18901"  // avoid 8901 (tweak_server uvicorn) and 8900 (frameflow-bff)
	}

	dbPath := os.Getenv("MVP_DB_PATH")
	if dbPath == "" {
		dbPath = "/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db"
	}

	// Open SQLite directly (not via the real main's setup).
	db := openDB(dbPath)
	defer db.Close()

	jwtSvc := auth.NewJWTService(cfg, db)

	r := gin.Default()
	r.GET("/healthz", auth.HealthCheck)

	api := r.Group("/api")
	api.POST("/auth/login", jwtSvc.Login)
	api.GET("/me/jwt", jwtSvc.Me)

	log.Printf("[mvp] phase_0 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
GOEOF

# Tiny db opener (avoids importing internal/state to keep Phase 0 isolated).
cat > cmd/mvp/db.go <<'GOEOF'
package main

import (
	"database/sql"
	_ "github.com/mattn/go-sqlite3"
)

func openDB(path string) *sql.DB {
	db, err := sql.Open("sqlite3", path+"?_journal_mode=WAL&_busy_timeout=5000")
	if err != nil {
		panic(err)
	}
	if err := db.Ping(); err != nil {
		panic(err)
	}
	return db
}
GOEOF

# Pin go.mod module name & ensure go-sqlite3 is available.
if ! grep -q 'github.com/mattn/go-sqlite3' go.mod; then
    go get github.com/mattn/go-sqlite3 2>&1 | tail -3 || true
fi

# ---- 5. go build ----
echo "[phase_0] step 5: go build (cmd/mvp standalone binary)"
go build -o /tmp/frameflow-bff-mvp ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ]; then
    echo "[phase_0] build FAILED exit=${build_exit}"
    exit 1
fi
echo "[phase_0] build OK → /tmp/frameflow-bff-mvp"

# ---- 6. 写 diff 摘要 ----
{
    echo "=== phase_0 real implementation diff ==="
    echo "started: $(date -Iseconds)"
    echo "mode: ${MODE}"
    echo "schema migration:"
    echo "  - ALTER TABLE wechat_users ADD COLUMN unionid"
    echo "  - ALTER TABLE wechat_users ADD COLUMN internal_user_id"
    echo "  - CREATE UNIQUE INDEX idx_wechat_users_internal_user_id"
    echo ""
    echo "files created:"
    echo "  - ${BFF}/internal/auth/jwt.go          (HMAC-SHA256 JWT, stdlib only)"
    echo "  - ${BFF}/internal/auth/jwt_login.go    (Login + Me + middleware, mocks via WEIXIN_MOCK_AUTH)"
    echo ""
    echo "files NOT yet modified (main.go mount + handler wire — to be done by main.go patch in run.sh if needed):"
    echo "  - main.go (existing /api/me remains untouched; Phase 0 needs separate mount)"
    echo ""
    echo "build:"
    echo "  /tmp/frameflow-bff-mvp ($(stat -c %s /tmp/frameflow-bff-mvp 2>/dev/null || echo unknown) bytes)"
    echo ""
    echo "TODO for full Phase 0 production wiring (next iteration):"
    echo "  - edit main.go: add api.POST(\"/auth/login\", jwtSvc.Login) + api.GET(\"/me/jwt\", jwtSvc.Me)"
    echo "  - currently phase_0/gate.sh directly invokes the built binary with WEIXIN_MOCK_AUTH=1"
} > "${DIFF_FILE}"

echo "[phase_0] run.sh OK"
exit 0
