#!/usr/bin/env bash
# Phase 1 — §17.B + §17.H — 多租户 + 文件权限 (REAL implementation)
#
# 由 orchestrator.sh 调用:bash phase_1/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. schema 迁移 — CREATE TABLE tenants / tenant_users / file_acl
#   2. 写入 internal/middleware/{auth.go,tenant.go} — RequireJWT + TenantScope
#   3. 写入 internal/filesvc/{signed.go,store.go} — HMAC sign/verify + ACL lookup
#   4. 写入 cmd/mvp/handlers_tenant.go + handlers_file.go
#   5. 重写 cmd/mvp/main.go — 挂载新路由(jwtOnly + scoped + public serve)
#   6. go build → 输出到 /tmp/frameflow-bff-mvp-p1
#   7. 启动 binary(后台, :18902) + 跑 gate.sh
#
# 设计要点(详见 tasks.yaml):
#   - 不动主 BFF main.go。cmd/mvp/ 是独立 binary,跨 Phase 累积。
#   - signed URL secret 走 FILESIGN_SECRET > JWT_SECRET > MVP_DEV seed。
#   - 文件 ACL 显式查表,即使签过名也要 recheck tenant(防 row re-binding)。

set -u
# Ensure go is on PATH — cron doesn't source /etc/profile.d; Phase 0 hit this
# with "go: command not found" on the most recent run.
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"
REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_1-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_1 run.sh start $(date -Iseconds) mode=${1:-?} ===}"

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_1] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_1 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. schema 迁移 ----
echo "[phase_1] step 1: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
sqlite3 "${DB_PATH}" <<'SQL'
CREATE TABLE IF NOT EXISTS tenants (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tenants_created_by ON tenants(created_by);

CREATE TABLE IF NOT EXISTS tenant_users (
  tenant_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'member',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_users_user ON tenant_users(user_id);

CREATE TABLE IF NOT EXISTS file_acl (
  file_key      TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  uploaded_by   TEXT NOT NULL,
  media_type    TEXT NOT NULL DEFAULT 'image',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_file_acl_tenant ON file_acl(tenant_id);
SQL
echo "[phase_1] schema verify:"
sqlite3 "${DB_PATH}" ".tables" | tr ' ' '\n' | grep -E "tenants|tenant_users|file_acl" | sort -u

# ---- 2. 写 internal/middleware/auth.go ----
echo "[phase_1] step 2: write internal/middleware/auth.go"
mkdir -p internal/middleware
cat > internal/middleware/auth.go <<'GOEOF'
// Package middleware exposes thin Gin middleware wrappers reused by both
// the production BFF and the MVP standalone binary (cmd/mvp).
package middleware

import (
	"frameflow-bff/internal/auth"

	"github.com/gin-gonic/gin"
)

// RequireJWT returns a Gin middleware that enforces a Bearer JWT signed by
// the given JWTService. On success it sets `internal_user_id` and `openid`
// on the gin.Context (matching the keys auth.JWTService.JWTAuthMiddleware
// writes, so downstream code can stay agnostic of which auth path issued it).
func RequireJWT(jwtSvc *auth.JWTService) gin.HandlerFunc {
	return jwtSvc.JWTAuthMiddleware()
}
GOEOF

# ---- 3. 写 internal/middleware/tenant.go ----
echo "[phase_1] step 3: write internal/middleware/tenant.go"
cat > internal/middleware/tenant.go <<'GOEOF'
package middleware

import (
	"database/sql"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"
)

// TenantScope aborts with 401 when X-Tenant-Id is missing or 403 when the
// authenticated user (looked up from gin.Context key "internal_user_id"
// populated by RequireJWT) is not a member of the named tenant.
//
// On success it sets `tenant_id` and `role` on the gin.Context.
//
// MUST be chained AFTER RequireJWT — without it, internal_user_id is absent.
func TenantScope(db *sql.DB) gin.HandlerFunc {
	return func(c *gin.Context) {
		uidV, ok := c.Get("internal_user_id")
		if !ok {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "missing internal_user_id (RequireJWT must run first)",
			})
			return
		}
		uid, _ := uidV.(string)

		tid := c.GetHeader("X-Tenant-Id")
		if tid == "" {
			c.AbortWithStatusJSON(http.StatusUnauthorized, gin.H{
				"error": "X-Tenant-Id header required",
			})
			return
		}

		var role string
		err := db.QueryRow(
			`SELECT role FROM tenant_users WHERE tenant_id = ? AND user_id = ?`,
			tid, uid,
		).Scan(&role)
		if errors.Is(err, sql.ErrNoRows) {
			c.AbortWithStatusJSON(http.StatusForbidden, gin.H{
				"error": "not a member of tenant",
			})
			return
		}
		if err != nil {
			c.AbortWithStatusJSON(http.StatusInternalServerError, gin.H{
				"error": "tenant lookup failed: " + err.Error(),
			})
			return
		}

		c.Set("tenant_id", tid)
		c.Set("role", role)
		c.Next()
	}
}
GOEOF

# ---- 4. 写 internal/filesvc/signed.go ----
echo "[phase_1] step 4: write internal/filesvc/signed.go"
mkdir -p internal/filesvc
cat > internal/filesvc/signed.go <<'GOEOF'
// Package filesvc implements signed-URL mint/verify and file_acl lookup.
//
// Signed URL format (no JWT required to serve):
//
//	GET /api/files/<file_key>?exp=<unix_seconds>&sig=<hex_hmac_sha256>
//
// sig = HMAC-SHA256(SecretBytes(), fileKey + ":" + exp)
//
// The secret is loaded at request time (not package init) so tests can
// override it via env without rebuilding.
package filesvc

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"strconv"
	"time"
)

// ErrExpired is returned by Verify when the URL's exp has passed.
var ErrExpired = errors.New("signed url expired")

// ErrBadSignature is returned by Verify when sig doesn't match the expected HMAC.
var ErrBadSignature = errors.New("bad signature")

// SecretBytes resolves the signing secret at call time.
// Precedence: FILESIGN_SECRET > JWT_SECRET > MVP_DEV seed (NEVER use the
// dev seed in production — rotate secrets regularly).
func SecretBytes() []byte {
	if v := os.Getenv("FILESIGN_SECRET"); v != "" {
		return []byte(v)
	}
	if v := os.Getenv("JWT_SECRET"); v != "" {
		return []byte(v)
	}
	return []byte("MVP_DEV_FILESIGN_SEED_DO_NOT_USE_IN_PROD")
}

// SignURL returns (exp, sig) for a signed download URL with the given TTL.
// exp is unix seconds; sig is hex-encoded HMAC-SHA256.
func SignURL(secret []byte, fileKey string, ttl time.Duration) (exp int64, sig string) {
	exp = time.Now().Add(ttl).Unix()
	sig = computeSig(secret, fileKey, exp)
	return
}

// Verify checks that sig matches HMAC-SHA256(secret, fileKey + ":" + exp)
// AND that exp is in the future. Returns nil on success.
func Verify(secret []byte, fileKey string, exp int64, sig string) error {
	want := computeSig(secret, fileKey, exp)
	if !hmac.Equal([]byte(want), []byte(sig)) {
		return ErrBadSignature
	}
	if time.Now().Unix() >= exp {
		return ErrExpired
	}
	return nil
}

func computeSig(secret []byte, fileKey string, exp int64) string {
	m := hmac.New(sha256.New, secret)
	m.Write([]byte(fileKey))
	m.Write([]byte(":"))
	m.Write([]byte(strconv.FormatInt(exp, 10)))
	return hex.EncodeToString(m.Sum(nil))
}
GOEOF

# ---- 5. 写 internal/filesvc/store.go ----
echo "[phase_1] step 5: write internal/filesvc/store.go"
cat > internal/filesvc/store.go <<'GOEOF'
package filesvc

import (
	"context"
	"database/sql"
	"errors"
)

// ErrFileNotFound is returned by LookupTenant when no ACL row exists for fileKey.
var ErrFileNotFound = errors.New("file_acl: file not found")

// Register creates or replaces an ACL row binding fileKey to tenantID.
// Used by Phase 2+ upload endpoints. Idempotent — calling twice for the same
// fileKey updates the binding to the latest uploader.
func Register(ctx context.Context, db *sql.DB, fileKey, tenantID, uploadedBy, mediaType string) error {
	if mediaType == "" {
		mediaType = "image"
	}
	_, err := db.ExecContext(ctx,
		`INSERT INTO file_acl (file_key, tenant_id, uploaded_by, media_type)
		 VALUES (?, ?, ?, ?)
		 ON CONFLICT(file_key) DO UPDATE SET
		   tenant_id = excluded.tenant_id,
		   uploaded_by = excluded.uploaded_by,
		   media_type = excluded.media_type`,
		fileKey, tenantID, uploadedBy, mediaType,
	)
	return err
}

// LookupTenant returns the tenant_id bound to fileKey.
// Returns ErrFileNotFound when no ACL row exists.
func LookupTenant(ctx context.Context, db *sql.DB, fileKey string) (string, error) {
	var tid string
	err := db.QueryRowContext(ctx,
		`SELECT tenant_id FROM file_acl WHERE file_key = ?`, fileKey,
	).Scan(&tid)
	if errors.Is(err, sql.ErrNoRows) {
		return "", ErrFileNotFound
	}
	if err != nil {
		return "", err
	}
	return tid, nil
}

// CountByTenant returns how many file_acl rows belong to the tenant.
// Used by gate.sh for invariant checks (after seeding).
func CountByTenant(ctx context.Context, db *sql.DB, tenantID string) (int, error) {
	var n int
	err := db.QueryRowContext(ctx,
		`SELECT COUNT(*) FROM file_acl WHERE tenant_id = ?`, tenantID,
	).Scan(&n)
	return n, err
}
GOEOF

# ---- 6. 写 cmd/mvp/handlers_tenant.go ----
echo "[phase_1] step 6: write cmd/mvp/handlers_tenant.go"
mkdir -p cmd/mvp
cat > cmd/mvp/handlers_tenant.go <<'GOEOF'
// Package main — Phase 1 tenant CRUD handlers.
// Routes registered in main.go; protected by RequireJWT (no X-Tenant-Id
// required for Create/ListMine, because the caller doesn't have a tenant yet).
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"errors"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"
)

// TenantHandler exposes tenant CRUD for the Phase 1 MVP.
type TenantHandler struct {
	DB *sql.DB
}

// NewTenantHandler is the canonical constructor.
func NewTenantHandler(db *sql.DB) *TenantHandler { return &TenantHandler{DB: db} }

type createTenantReq struct {
	Name string `json:"name" binding:"required"`
}

type tenantResp struct {
	ID     string `json:"id"`
	Name   string `json:"name"`
	Status string `json:"status"`
	Role   string `json:"role"`
}

// Create handles POST /api/tenants — creates a tenant, binds the caller as owner.
// Auth: JWT only (no X-Tenant-Id needed pre-creation).
func (h *TenantHandler) Create(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	if uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing internal_user_id"})
		return
	}
	var req createTenantReq
	if err := c.ShouldBindJSON(&req); err != nil || strings.TrimSpace(req.Name) == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "name required"})
		return
	}
	id := newTenantID()
	if _, err := h.DB.Exec(
		`INSERT INTO tenants (id, name, created_by) VALUES (?, ?, ?)`,
		id, req.Name, uid,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create tenant failed: " + err.Error()})
		return
	}
	if _, err := h.DB.Exec(
		`INSERT INTO tenant_users (tenant_id, user_id, role) VALUES (?, ?, 'owner')`,
		id, uid,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "owner bind failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, tenantResp{ID: id, Name: req.Name, Status: "active", Role: "owner"})
}

// ListMine handles GET /api/tenants — lists tenants the caller belongs to.
// Auth: JWT only.
func (h *TenantHandler) ListMine(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	if uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing internal_user_id"})
		return
	}
	rows, err := h.DB.Query(
		`SELECT t.id, t.name, t.status, tu.role
		 FROM tenants t JOIN tenant_users tu ON t.id = tu.tenant_id
		 WHERE tu.user_id = ?
		 ORDER BY t.created_at DESC`,
		uid,
	)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list failed: " + err.Error()})
		return
	}
	defer rows.Close()

	out := []tenantResp{}
	for rows.Next() {
		var r tenantResp
		if err := rows.Scan(&r.ID, &r.Name, &r.Status, &r.Role); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "scan failed: " + err.Error()})
			return
		}
		out = append(out, r)
	}
	c.JSON(http.StatusOK, gin.H{"tenants": out, "count": len(out)})
}

type addMemberReq struct {
	UserID string `json:"user_id" binding:"required"`
	Role   string `json:"role"`
}

// AddMember handles POST /api/tenants/:id/members — adds a member to a tenant.
// Auth: JWT + X-Tenant-Id; only owner of the tenant may add members.
func (h *TenantHandler) AddMember(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	tid := c.Param("id")

	var callerRole string
	err := h.DB.QueryRow(
		`SELECT role FROM tenant_users WHERE tenant_id = ? AND user_id = ?`,
		tid, uid,
	).Scan(&callerRole)
	if errors.Is(err, sql.ErrNoRows) {
		c.JSON(http.StatusForbidden, gin.H{"error": "not a member of tenant"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if callerRole != "owner" {
		c.JSON(http.StatusForbidden, gin.H{"error": "only owner can add members"})
		return
	}

	var req addMemberReq
	if err := c.ShouldBindJSON(&req); err != nil || req.UserID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "user_id required"})
		return
	}
	if req.Role == "" {
		req.Role = "member"
	}
	if _, err := h.DB.Exec(
		`INSERT OR IGNORE INTO tenant_users (tenant_id, user_id, role) VALUES (?, ?, ?)`,
		tid, req.UserID, req.Role,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "add failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"tenant_id": tid,
		"user_id":   req.UserID,
		"role":      req.Role,
	})
}

func newTenantID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "tn_" + hex.EncodeToString(b)
}
GOEOF

# ---- 7. 写 cmd/mvp/handlers_file.go ----
echo "[phase_1] step 7: write cmd/mvp/handlers_file.go"
cat > cmd/mvp/handlers_file.go <<'GOEOF'
// Package main — Phase 1 file sign + serve handlers.
package main

import (
	"database/sql"
	"errors"
	"fmt"
	"net/http"
	"strconv"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/filesvc"
)

// FileHandler exposes /api/files/sign (mint) and /api/files/:key (serve).
type FileHandler struct {
	DB *sql.DB
}

func NewFileHandler(db *sql.DB) *FileHandler { return &FileHandler{DB: db} }

// Sign handles GET /api/files/sign?key=<file_key>[&ttl_seconds=N] — mints a
// signed URL for the named file. Caller must be in the tenant that owns the
// file (X-Tenant-Id + tenant_users lookup).
func (h *FileHandler) Sign(c *gin.Context) {
	fileKey := c.Query("key")
	if fileKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "key query param required"})
		return
	}
	tidV, ok := c.Get("tenant_id")
	if !ok {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "tenant_id missing from context (TenantScope must run first)"})
		return
	}
	tid := tidV.(string)

	actualTid, err := filesvc.LookupTenant(c.Request.Context(), h.DB, fileKey)
	if errors.Is(err, filesvc.ErrFileNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "file not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "acl lookup: " + err.Error()})
		return
	}
	if actualTid != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "file belongs to another tenant"})
		return
	}

	ttl := 5 * time.Minute
	if v := c.Query("ttl_seconds"); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 && n <= 3600 {
			ttl = time.Duration(n) * time.Second
		}
	}
	exp, sig := filesvc.SignURL(filesvc.SecretBytes(), fileKey, ttl)
	c.JSON(http.StatusOK, gin.H{
		"file_key":    fileKey,
		"exp":         exp,
		"sig":         sig,
		"ttl_seconds": int(ttl.Seconds()),
		"url":         fmt.Sprintf("/api/files/%s?exp=%d&sig=%s", fileKey, exp, sig),
	})
}

// Serve handles GET /api/files/:key?exp=<unix>&sig=<hex> — serves file
// bytes (placeholder for MVP). No JWT required: the URL itself is the
// authorization. Verify checks sig + exp; ACL re-check confirms the file
// is still bound (avoids serving rows whose file_key was re-bound mid-flight).
func (h *FileHandler) Serve(c *gin.Context) {
	fileKey := c.Param("key")
	expStr := c.Query("exp")
	sig := c.Query("sig")
	if expStr == "" || sig == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "exp and sig required"})
		return
	}
	exp, err := strconv.ParseInt(expStr, 10, 64)
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid exp"})
		return
	}
	if err := filesvc.Verify(filesvc.SecretBytes(), fileKey, exp, sig); err != nil {
		c.JSON(http.StatusForbidden, gin.H{"error": "verify failed: " + err.Error()})
		return
	}
	tid, err := filesvc.LookupTenant(c.Request.Context(), h.DB, fileKey)
	if err != nil {
		c.JSON(http.StatusNotFound, gin.H{"error": "file not found"})
		return
	}
	// MVP placeholder — real bytes are wired in Phase 2+ via object storage.
	c.JSON(http.StatusOK, gin.H{
		"file_key":  fileKey,
		"tenant_id": tid,
		"note":      "MVP placeholder — real bytes wired in Phase 2+ (object storage + signed URL cache)",
		"served_at": time.Now().Unix(),
	})
}
GOEOF

# ---- 8. 重写 cmd/mvp/main.go (Phase 0 → Phase 1 扩展) ----
echo "[phase_1] step 8: rewrite cmd/mvp/main.go (extends Phase 0)"
cat > cmd/mvp/main.go <<'GOEOF'
// Package main is the MVP standalone binary extended across Phases 0 + 1.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
//
// Runs on a separate port from the production BFF (default :18902) so a
// crash here can't take down the main server. Doesn't touch main.go.
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/auth"
	"frameflow-bff/internal/config"
	"frameflow-bff/internal/middleware"
)

func main() {
	cfg := config.Load()
	if err := config.Validate(cfg); err != nil {
		log.Printf("[mvp] WARN config validate: %v (continuing — JWT seed handles missing WeChat)", err)
	}

	port := os.Getenv("MVP_PORT")
	if port == "" {
		// Phase 1 default: 18902 (Phase 0 was 18901; orchestrator chooses)
		port = "18902"
	}

	dbPath := os.Getenv("MVP_DB_PATH")
	if dbPath == "" {
		dbPath = "/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db"
	}

	db := openDB(dbPath)
	defer db.Close()

	jwtSvc := auth.NewJWTService(cfg, db)
	tenants := NewTenantHandler(db)
	files := NewFileHandler(db)

	r := gin.Default()
	r.GET("/healthz", auth.HealthCheck)

	api := r.Group("/api")

	// --- Phase 0 public routes ---
	api.POST("/auth/login", jwtSvc.Login)
	api.GET("/me/jwt", jwtSvc.Me)

	// --- Phase 1 routes requiring JWT only (no tenant header — caller
	//     may not have one yet, e.g. when creating their first tenant) ---
	jwtOnly := api.Group("")
	jwtOnly.Use(middleware.RequireJWT(jwtSvc))
	jwtOnly.POST("/tenants", tenants.Create)
	jwtOnly.GET("/tenants", tenants.ListMine)

	// --- Phase 1 routes requiring JWT + X-Tenant-Id ---
	scoped := api.Group("")
	scoped.Use(middleware.RequireJWT(jwtSvc))
	scoped.Use(middleware.TenantScope(db))
	scoped.POST("/tenants/:id/members", tenants.AddMember)
	scoped.GET("/files/sign", files.Sign)

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_1 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
GOEOF

# ---- 9. go build ----
echo "[phase_1] step 9: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p1"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
if [ ! -x "${BIN}" ]; then
    echo "[phase_1] build FAILED — binary not produced" >&2
    exit 4
fi

# ---- 10. start binary (background) ----
echo "[phase_1] step 10: start binary on :18902"
pkill -f frameflow-bff-mvp-p1 2>/dev/null || true
sleep 1

WEIXIN_MOCK_AUTH=1 MVP_PORT=18902 MVP_DB_PATH="${DB_PATH}" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_1-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_1] server pid=${SERVER_PID}"

# Wait for /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18902/healthz" >/dev/null 2>&1; then
        echo "[phase_1] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_1] /healthz never came up — server log:" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_1-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    exit 5
fi

# ---- 11. run gate ----
echo "[phase_1] step 11: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 12. stop server ----
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_1] gate FAILED exit=${GATE_EXIT}"
    exit 1
fi

echo "[phase_1] DONE — gate green, server stopped"
exit 0