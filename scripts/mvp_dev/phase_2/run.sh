#!/usr/bin/env bash
# Phase 2 — §17.C — Product / Asset 管理 (REAL implementation)
#
# 由 orchestrator.sh 调用:bash phase_2/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. schema 迁移 — CREATE TABLE products / product_assets / product_manifests
#   2. 写入 internal/productsvc/{store.go,manifest.go,classify.go}
#   3. 写入 cmd/mvp/handlers_product.go — 6 条路由 handler
#   4. 改写 cmd/mvp/main.go — 累加 Phase 0 + Phase 1 路由 + 挂 Phase 2 路由
#   5. go build → 输出到 /tmp/frameflow-bff-mvp-p2
#   6. 启动 binary(后台, :18903) + 跑 gate.sh
#
# 设计要点(详见 tasks.yaml):
#   - cmd/mvp/main.go 跨 Phase 累积,不重写老路由。
#   - file_key 走 "pa_<16hex>" 格式,Register 进 file_acl 以便 Phase 1 签名 URL 工作。
#   - 分类走 MVP 启发式(classify.go),允许表单 role/quality_score 覆盖。
#   - Manifest 在每次 assets 变化后重建(version+1, missing_roles 来自固定 11 个 role 集合)。

set -u
set -o pipefail
# Ensure go is on PATH — cron doesn't source /etc/profile.d; Phase 0 hit this
# with "go: command not found" on the most recent run.
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"
REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_2-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_2 run.sh start $(date -Iseconds) mode=${1:-?} ===}"

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_2] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_2 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. schema 迁移 ----
echo "[phase_2] step 1: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
sqlite3 "${DB_PATH}" <<'SQL'
CREATE TABLE IF NOT EXISTS products (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  name        TEXT NOT NULL,
  category    TEXT NOT NULL DEFAULT 'general',
  sku         TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id);
CREATE INDEX IF NOT EXISTS idx_products_tenant_name ON products(tenant_id, name);

CREATE TABLE IF NOT EXISTS product_assets (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  product_id       TEXT NOT NULL,
  file_key         TEXT NOT NULL,
  media_type       TEXT NOT NULL DEFAULT 'image',
  role             TEXT NOT NULL DEFAULT 'unclassified',
  quality_score    REAL NOT NULL DEFAULT 0.5,
  ai_metadata_json TEXT NOT NULL DEFAULT '{}',
  uploaded_by      TEXT NOT NULL,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_assets_product ON product_assets(product_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_product_assets_file_key ON product_assets(file_key);

CREATE TABLE IF NOT EXISTS product_manifests (
  id                 TEXT PRIMARY KEY,
  product_id         TEXT NOT NULL,
  version            INTEGER NOT NULL DEFAULT 1,
  assets_json        TEXT NOT NULL DEFAULT '[]',
  missing_roles_json TEXT NOT NULL DEFAULT '[]',
  ai_model           TEXT NOT NULL DEFAULT 'mvp_heuristic_v1',
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_manifests_product ON product_manifests(product_id, version);
SQL
echo "[phase_2] schema verify:"
sqlite3 "${DB_PATH}" ".tables" | tr ' ' '\n' | grep -E "products|product_assets|product_manifests" | sort -u

# ---- 2. 写 internal/productsvc/store.go ----
echo "[phase_2] step 2: write internal/productsvc/store.go"
mkdir -p internal/productsvc
cat > internal/productsvc/store.go <<'GOEOF'
// Package productsvc implements the §17.C product / asset / manifest data model.
//
// Three tables (all created via CREATE TABLE IF NOT EXISTS in Phase 2 run.sh):
//
//   products          — one row per SKU
//   product_assets    — files (image / video / audio) bound to a product
//   product_manifests — append-only history of asset classification snapshots
//
// Every read/write keeps a tenant_id stamp so the BFF's TenantScope middleware
// can enforce row-level isolation; cross-tenant reads must return 403 at the
// handler layer (the gate test relies on this for security).
package productsvc

import (
	"context"
	"database/sql"
	"errors"
	"time"
)

// ErrProductNotFound is returned by GetProduct / GetManifest when no row exists.
var ErrProductNotFound = errors.New("productsvc: product not found")

// ErrAssetNotFound is returned by GetAsset / UpdateAssetRole when no row exists.
var ErrAssetNotFound = errors.New("productsvc: asset not found")

// Product is the in-memory shape of a row in products.
type Product struct {
	ID        string
	TenantID  string
	Name      string
	Category  string
	SKU       string
	CreatedBy string
	CreatedAt time.Time
}

// Asset is the in-memory shape of a row in product_assets.
type Asset struct {
	ID          string
	TenantID    string
	ProductID   string
	FileKey     string
	MediaType   string
	Role        string
	QualityScore float64
	AIMetadataJSON string
	UploadedBy  string
	CreatedAt   time.Time
}

// Manifest is the in-memory shape of a row in product_manifests.
type Manifest struct {
	ID            string
	ProductID     string
	Version       int
	AssetsJSON    string
	MissingRolesJSON string
	AIModel       string
	CreatedAt     time.Time
}

// CreateProduct inserts a product row. id must already be minted by the caller.
func CreateProduct(ctx context.Context, db *sql.DB, p Product) error {
	_, err := db.ExecContext(ctx,
		`INSERT INTO products (id, tenant_id, name, category, sku, created_by)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		p.ID, p.TenantID, p.Name, p.Category, p.SKU, p.CreatedBy,
	)
	return err
}

// GetProduct fetches a product by id. Returns ErrProductNotFound when missing.
func GetProduct(ctx context.Context, db *sql.DB, id string) (Product, error) {
	var p Product
	var createdAt string
	err := db.QueryRowContext(ctx,
		`SELECT id, tenant_id, name, category, sku, created_by, created_at
		 FROM products WHERE id = ?`, id,
	).Scan(&p.ID, &p.TenantID, &p.Name, &p.Category, &p.SKU, &p.CreatedBy, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return p, ErrProductNotFound
	}
	if err != nil {
		return p, err
	}
	p.CreatedAt = parseSQLTime(createdAt)
	return p, nil
}

// CreateAsset inserts a product_assets row. id must already be minted.
func CreateAsset(ctx context.Context, db *sql.DB, a Asset) error {
	_, err := db.ExecContext(ctx,
		`INSERT INTO product_assets
		 (id, tenant_id, product_id, file_key, media_type, role, quality_score, ai_metadata_json, uploaded_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		a.ID, a.TenantID, a.ProductID, a.FileKey, a.MediaType, a.Role,
		a.QualityScore, a.AIMetadataJSON, a.UploadedBy,
	)
	return err
}

// ListAssets returns all assets bound to a product, oldest first.
func ListAssets(ctx context.Context, db *sql.DB, productID string) ([]Asset, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT id, tenant_id, product_id, file_key, media_type, role,
		        quality_score, ai_metadata_json, uploaded_by, created_at
		 FROM product_assets WHERE product_id = ?
		 ORDER BY created_at ASC`, productID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []Asset{}
	for rows.Next() {
		var a Asset
		var createdAt string
		if err := rows.Scan(&a.ID, &a.TenantID, &a.ProductID, &a.FileKey,
			&a.MediaType, &a.Role, &a.QualityScore, &a.AIMetadataJSON,
			&a.UploadedBy, &createdAt); err != nil {
			return nil, err
		}
		a.CreatedAt = parseSQLTime(createdAt)
		out = append(out, a)
	}
	return out, rows.Err()
}

// GetAsset fetches a single asset by id. Returns ErrAssetNotFound when missing.
func GetAsset(ctx context.Context, db *sql.DB, id string) (Asset, error) {
	var a Asset
	var createdAt string
	err := db.QueryRowContext(ctx,
		`SELECT id, tenant_id, product_id, file_key, media_type, role,
		        quality_score, ai_metadata_json, uploaded_by, created_at
		 FROM product_assets WHERE id = ?`, id,
	).Scan(&a.ID, &a.TenantID, &a.ProductID, &a.FileKey,
		&a.MediaType, &a.Role, &a.QualityScore, &a.AIMetadataJSON,
		&a.UploadedBy, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return a, ErrAssetNotFound
	}
	if err != nil {
		return a, err
	}
	a.CreatedAt = parseSQLTime(createdAt)
	return a, nil
}

// UpdateAssetRole overwrites the role + quality_score for an asset.
// Used by PUT /api/products/:id/manifest/:asset_id (manual correction).
func UpdateAssetRole(ctx context.Context, db *sql.DB, id, role string, qualityScore float64) error {
	res, err := db.ExecContext(ctx,
		`UPDATE product_assets SET role = ?, quality_score = ? WHERE id = ?`,
		role, qualityScore, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrAssetNotFound
	}
	return nil
}

// GetLatestManifest returns the most recent manifest row for a product, or
// (Manifest{}, ErrProductNotFound) if none. Callers wanting a guaranteed
// manifest should use BuildManifest instead — it falls back to creating one.
func GetLatestManifest(ctx context.Context, db *sql.DB, productID string) (Manifest, error) {
	var m Manifest
	var createdAt string
	err := db.QueryRowContext(ctx,
		`SELECT id, product_id, version, assets_json, missing_roles_json, ai_model, created_at
		 FROM product_manifests WHERE product_id = ?
		 ORDER BY version DESC LIMIT 1`, productID,
	).Scan(&m.ID, &m.ProductID, &m.Version, &m.AssetsJSON,
		&m.MissingRolesJSON, &m.AIModel, &createdAt)
	if errors.Is(err, sql.ErrNoRows) {
		return m, ErrProductNotFound
	}
	if err != nil {
		return m, err
	}
	m.CreatedAt = parseSQLTime(createdAt)
	return m, nil
}

// CreateManifest inserts a new manifest row. id must already be minted.
func CreateManifest(ctx context.Context, db *sql.DB, m Manifest) error {
	_, err := db.ExecContext(ctx,
		`INSERT INTO product_manifests
		 (id, product_id, version, assets_json, missing_roles_json, ai_model)
		 VALUES (?, ?, ?, ?, ?, ?)`,
		m.ID, m.ProductID, m.Version, m.AssetsJSON, m.MissingRolesJSON, m.AIModel,
	)
	return err
}

// parseSQLTime accepts both "YYYY-MM-DD HH:MM:SS" and RFC3339 — sqlite's
// datetime('now') returns the former. We treat any unparseable input as zero
// time rather than failing the whole row.
func parseSQLTime(s string) time.Time {
	if t, err := time.Parse("2006-01-02 15:04:05", s); err == nil {
		return t
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t
	}
	return time.Time{}
}
GOEOF

# ---- 3. 写 internal/productsvc/manifest.go ----
echo "[phase_2] step 3: write internal/productsvc/manifest.go"
cat > internal/productsvc/manifest.go <<'GOEOF'
package productsvc

import (
	"context"
	"database/sql"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
)

// CommonRoles is the fixed 11-role set a "complete" product asset bundle
// should cover. Missing roles surface as gaps in the manifest so the user
// (or downstream agent) knows what to source next.
var CommonRoles = []string{
	"hero_front",
	"hero_45",
	"side",
	"back",
	"detail",
	"lifestyle",
	"logo",
	"open_view",
	"inside",
	"wheel_detail",
	"handle_detail",
}

// ManifestAsset is the per-asset row embedded in the manifest's assets_json.
// Kept as a separate type so the public surface is the same whether built
// from a fresh ListAssets or restored from a stored manifest JSON blob.
type ManifestAsset struct {
	AssetID      string  `json:"asset_id"`
	Role         string  `json:"role"`
	QualityScore float64 `json:"quality_score"`
	FileKey      string  `json:"file_key"`
}

// BuildManifest reads the current asset list for productID, computes which of
// the CommonRoles are missing, and returns a fresh Manifest row (NOT yet
// inserted — caller decides whether to persist via CreateManifest).
//
// BuildManifest is pure: it does NOT bump version based on existing rows.
// Callers that want a versioned history should query GetLatestManifest first
// and pass m.Version = last.Version + 1.
func BuildManifest(ctx context.Context, db *sql.DB, productID string) (Manifest, error) {
	assets, err := ListAssets(ctx, db, productID)
	if err != nil {
		return Manifest{}, err
	}

	seen := map[string]bool{}
	items := []ManifestAsset{}
	for _, a := range assets {
		items = append(items, ManifestAsset{
			AssetID:      a.ID,
			Role:         a.Role,
			QualityScore: a.QualityScore,
			FileKey:      a.FileKey,
		})
		if a.Role != "" && a.Role != "unclassified" {
			seen[a.Role] = true
		}
	}

	missing := []string{}
	for _, r := range CommonRoles {
		if !seen[r] {
			missing = append(missing, r)
		}
	}

	assetsJSON, _ := json.Marshal(items)
	missingJSON, _ := json.Marshal(missing)

	return Manifest{
		ID:              newManifestID(),
		ProductID:       productID,
		Version:         1, // caller bumps
		AssetsJSON:      string(assetsJSON),
		MissingRolesJSON: string(missingJSON),
		AIModel:         "mvp_heuristic_v1",
	}, nil
}

func newManifestID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "pm_" + hex.EncodeToString(b)
}
GOEOF

# ---- 4. 写 internal/productsvc/classify.go ----
echo "[phase_2] step 4: write internal/productsvc/classify.go"
cat > internal/productsvc/classify.go <<'GOEOF'
package productsvc

import (
	"encoding/json"
	"strings"
)

// Heuristic rules for MVP filename-based classification.
// Real visual models land in Phase 5+ via Agent Gateway — until then this is
// good enough to exercise the manifest rebuild path end-to-end.
//
//   filename contains "hero"      → role=hero_front, quality=0.85
//   filename contains "detail"    → role=detail,     quality=0.80
//   filename contains "lifestyle" → role=lifestyle,  quality=0.75
//   otherwise                     → role=unclassified, quality=0.50
//
// Caller-supplied overrides win over the heuristic — explicit user labels
// are higher signal than file name guesses.

type classifyResult struct {
	Role       string
	Quality    float64
	MetadataJSON string
}

// Classify runs the filename heuristic and returns (role, quality, metadata_json).
// overrideRole, when non-empty, replaces the heuristic role.
// overrideQuality, when in (0,1], replaces the heuristic quality.
func Classify(filename, overrideRole string, overrideQuality float64) (string, float64, string) {
	lower := strings.ToLower(filename)

	role := "unclassified"
	quality := 0.50
	heuristic := "none"

	switch {
	case strings.Contains(lower, "hero"):
		role = "hero_front"
		quality = 0.85
		heuristic = "filename_contains_hero"
	case strings.Contains(lower, "detail"):
		role = "detail"
		quality = 0.80
		heuristic = "filename_contains_detail"
	case strings.Contains(lower, "lifestyle"):
		role = "lifestyle"
		quality = 0.75
		heuristic = "filename_contains_lifestyle"
	}

	if overrideRole != "" {
		role = overrideRole
	}
	if overrideQuality > 0 && overrideQuality <= 1 {
		quality = overrideQuality
	}

	meta := map[string]any{
		"filename":     filename,
		"heuristic":    heuristic,
		"role_source":  roleSource(overrideRole != "", heuristic != "none"),
		"quality_set":  quality,
	}
	metaJSON, _ := json.Marshal(meta)

	return role, quality, string(metaJSON)
}

func roleSource(overrideApplied, heuristicHit bool) string {
	switch {
	case overrideApplied:
		return "user_override"
	case heuristicHit:
		return "filename_heuristic"
	default:
		return "default"
	}
}
GOEOF

# ---- 5. 写 cmd/mvp/handlers_product.go ----
echo "[phase_2] step 5: write cmd/mvp/handlers_product.go"
mkdir -p cmd/mvp
cat > cmd/mvp/handlers_product.go <<'GOEOF'
// Package main — Phase 2 product / asset / manifest handlers.
//
// Six routes, all mounted under the `scoped` group (RequireJWT + TenantScope):
//
//   POST  /api/products                         — create a product
//   GET   /api/products/:id                     — read a product (tenant check)
//   POST  /api/products/:id/assets              — upload + classify + register
//   GET   /api/products/:id/assets              — list assets
//   GET   /api/products/:id/manifest            — latest manifest (rebuild if missing)
//   PUT   /api/products/:id/manifest/:asset_id  — manual correction → rebuild
//
// Upload saves bytes to ${MVP_UPLOAD_DIR:-/tmp/mvp_uploads}/<file_key> AND
// registers file_acl so Phase 1's signed-URL flow can serve the file later.
package main

import (
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/filesvc"
	"frameflow-bff/internal/productsvc"
)

// ProductHandler exposes the §17.C routes.
type ProductHandler struct {
	DB *sql.DB
}

func NewProductHandler(db *sql.DB) *ProductHandler { return &ProductHandler{DB: db} }

// productCreateReq is the body for POST /api/products.
type productCreateReq struct {
	Name     string `json:"name" binding:"required"`
	Category string `json:"category"`
	SKU      string `json:"sku"`
}

// productResp is the JSON shape returned by Get (and echoed by Create).
type productResp struct {
	ID       string `json:"id"`
	Name     string `json:"name"`
	Category string `json:"category"`
	SKU      string `json:"sku"`
	TenantID string `json:"tenant_id"`
}

// Create handles POST /api/products — creates a product row.
func (h *ProductHandler) Create(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	if uid == "" || tid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing identity (RequireJWT+TenantScope must run first)"})
		return
	}

	var req productCreateReq
	if err := c.ShouldBindJSON(&req); err != nil || req.Name == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "name required"})
		return
	}
	if req.Category == "" {
		req.Category = "general"
	}

	p := productsvc.Product{
		ID:        newProductID(),
		TenantID:  tid,
		Name:      req.Name,
		Category:  req.Category,
		SKU:       req.SKU,
		CreatedBy: uid,
	}
	if err := productsvc.CreateProduct(c.Request.Context(), h.DB, p); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, productResp{
		ID: p.ID, Name: p.Name, Category: p.Category, SKU: p.SKU, TenantID: p.TenantID,
	})
}

// Get handles GET /api/products/:id — fetches a product, enforces tenant.
func (h *ProductHandler) Get(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	id := c.Param("id")

	p, err := productsvc.GetProduct(c.Request.Context(), h.DB, id)
	if errors.Is(err, productsvc.ErrProductNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		// Cross-tenant probe — return 403 (matches gate expectation).
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}
	c.JSON(http.StatusOK, productResp{
		ID: p.ID, Name: p.Name, Category: p.Category, SKU: p.SKU, TenantID: p.TenantID,
	})
}

// UploadAsset handles POST /api/products/:id/assets — multipart upload.
//
//   file field         — required, the bytes (any type; we accept anything)
//   role form field    — optional, overrides heuristic role
//   quality_score field — optional float (0,1], overrides heuristic quality
//
// Steps:
//   1. Verify product exists AND belongs to caller's tenant.
//   2. Mint file_key "pa_<16hex>" and write bytes to upload dir.
//   3. Register file_acl row (so Phase 1 signed URLs work).
//   4. Run classify heuristic on the uploaded filename.
//   5. Insert product_assets row.
//   6. Rebuild manifest (version = previous+1).
func (h *ProductHandler) UploadAsset(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	if uid == "" || tid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing identity"})
		return
	}
	productID := c.Param("id")

	// 1. tenant check
	p, err := productsvc.GetProduct(c.Request.Context(), h.DB, productID)
	if errors.Is(err, productsvc.ErrProductNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}

	// 2. multipart read
	fh, err := c.FormFile("file")
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "file form field required: " + err.Error()})
		return
	}
	f, err := fh.Open()
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "open failed: " + err.Error()})
		return
	}
	defer f.Close()

	uploadDir := os.Getenv("MVP_UPLOAD_DIR")
	if uploadDir == "" {
		uploadDir = "/tmp/mvp_uploads"
	}
	if err := os.MkdirAll(uploadDir, 0o755); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "mkdir upload dir: " + err.Error()})
		return
	}

	fileKey := newFileKey()
	dst := uploadDir + "/" + fileKey
	out, err := os.Create(dst)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create file: " + err.Error()})
		return
	}
	n, err := io.Copy(out, f)
	out.Close()
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "write file: " + err.Error()})
		return
	}

	// 3. register file_acl
	mediaType := "image"
	if ct := fh.Header.Get("Content-Type"); ct != "" {
		mediaType = ct
	}
	if err := filesvc.Register(c.Request.Context(), h.DB, fileKey, tid, uid, mediaType); err != nil {
		log.Printf("[mvp] UploadAsset: file_acl register failed: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "acl register failed: " + err.Error()})
		return
	}

	// 4. classify
	overrideRole := c.PostForm("role")
	var overrideQuality float64 = 0
	if qs := c.PostForm("quality_score"); qs != "" {
		if v, perr := strconv.ParseFloat(qs, 64); perr == nil {
			overrideQuality = v
		}
	}
	role, quality, metaJSON := productsvc.Classify(fh.Filename, overrideRole, overrideQuality)

	// 5. insert product_assets
	asset := productsvc.Asset{
		ID:             newAssetID(),
		TenantID:       tid,
		ProductID:      productID,
		FileKey:        fileKey,
		MediaType:      mediaType,
		Role:           role,
		QualityScore:   quality,
		AIMetadataJSON: metaJSON,
		UploadedBy:     uid,
	}
	if err := productsvc.CreateAsset(c.Request.Context(), h.DB, asset); err != nil {
		log.Printf("[mvp] UploadAsset: asset insert failed: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "asset insert failed: " + err.Error()})
		return
	}

	// 6. rebuild manifest (best-effort — log on failure but don't fail the upload)
	if err := rebuildManifest(c, h.DB, productID); err != nil {
		log.Printf("[mvp] UploadAsset: manifest rebuild failed: %v", err)
	}

	c.JSON(http.StatusOK, gin.H{
		"asset_id":      asset.ID,
		"file_key":      fileKey,
		"role":          role,
		"quality_score": quality,
		"bytes":         n,
	})
}

// ListAssets handles GET /api/products/:id/assets — list a product's assets.
func (h *ProductHandler) ListAssets(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	productID := c.Param("id")

	p, err := productsvc.GetProduct(c.Request.Context(), h.DB, productID)
	if errors.Is(err, productsvc.ErrProductNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}

	assets, err := productsvc.ListAssets(c.Request.Context(), h.DB, productID)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "list failed: " + err.Error()})
		return
	}

	type assetItem struct {
		AssetID      string  `json:"asset_id"`
		FileKey      string  `json:"file_key"`
		MediaType    string  `json:"media_type"`
		Role         string  `json:"role"`
		QualityScore float64 `json:"quality_score"`
	}
	out := make([]assetItem, 0, len(assets))
	for _, a := range assets {
		out = append(out, assetItem{
			AssetID: a.ID, FileKey: a.FileKey, MediaType: a.MediaType,
			Role: a.Role, QualityScore: a.QualityScore,
		})
	}
	c.JSON(http.StatusOK, gin.H{"assets": out, "count": len(out)})
}

// GetManifest handles GET /api/products/:id/manifest — returns the latest
// manifest, building one if none exists yet.
func (h *ProductHandler) GetManifest(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	productID := c.Param("id")

	p, err := productsvc.GetProduct(c.Request.Context(), h.DB, productID)
	if errors.Is(err, productsvc.ErrProductNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}

	latest, err := productsvc.GetLatestManifest(c.Request.Context(), h.DB, productID)
	var m productsvc.Manifest
	if errors.Is(err, productsvc.ErrProductNotFound) {
		// No manifest yet — build one (version 1) and persist.
		fresh, berr := productsvc.BuildManifest(c.Request.Context(), h.DB, productID)
		if berr != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "build manifest: " + berr.Error()})
			return
		}
		fresh.Version = 1
		if perr := productsvc.CreateManifest(c.Request.Context(), h.DB, fresh); perr != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "persist manifest: " + perr.Error()})
			return
		}
		m = fresh
	} else if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "manifest lookup: " + err.Error()})
		return
	} else {
		m = latest
	}

	// Decode assets_json (stored as JSON string in the DB) back into an array
	// so callers see `assets: [...]` instead of a stringified blob.
	var assetsOut []productsvc.ManifestAsset
	if m.AssetsJSON != "" {
		_ = json.Unmarshal([]byte(m.AssetsJSON), &assetsOut)
	}
	var missingOut []string
	if m.MissingRolesJSON != "" {
		_ = json.Unmarshal([]byte(m.MissingRolesJSON), &missingOut)
	}

	c.JSON(http.StatusOK, gin.H{
		"id":            m.ID,
		"product_id":    m.ProductID,
		"version":       m.Version,
		"assets":        assetsOut,
		"missing_roles": missingOut,
		"ai_model":      m.AIModel,
		"created_at":    m.CreatedAt.Format("2006-01-02 15:04:05"),
	})
}

// CorrectAsset handles PUT /api/products/:id/manifest/:asset_id — overwrite
// the role + quality_score, then rebuild the manifest with version+1.
func (h *ProductHandler) CorrectAsset(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	productID := c.Param("id")
	assetID := c.Param("asset_id")

	// Tenant check via the product row.
	p, err := productsvc.GetProduct(c.Request.Context(), h.DB, productID)
	if errors.Is(err, productsvc.ErrProductNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}

	var req struct {
		Role         string  `json:"role" binding:"required"`
		QualityScore float64 `json:"quality_score"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Role == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "role required"})
		return
	}
	if req.QualityScore <= 0 || req.QualityScore > 1 {
		req.QualityScore = 0.5
	}

	if err := productsvc.UpdateAssetRole(c.Request.Context(), h.DB, assetID, req.Role, req.QualityScore); err != nil {
		if errors.Is(err, productsvc.ErrAssetNotFound) {
			c.JSON(http.StatusNotFound, gin.H{"error": "asset not found"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update failed: " + err.Error()})
		return
	}

	if err := rebuildManifest(c, h.DB, productID); err != nil {
		log.Printf("[mvp] CorrectAsset: manifest rebuild failed: %v", err)
		c.JSON(http.StatusInternalServerError, gin.H{"error": "manifest rebuild: " + err.Error()})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"asset_id":      assetID,
		"role":          req.Role,
		"quality_score": req.QualityScore,
	})
}

// rebuildManifest builds a new manifest with version = previous+1 and persists it.
// If no manifest exists yet, writes version 1.
func rebuildManifest(c *gin.Context, db *sql.DB, productID string) error {
	latest, err := productsvc.GetLatestManifest(c.Request.Context(), db, productID)
	nextVersion := 1
	if err == nil {
		nextVersion = latest.Version + 1
	}
	fresh, err := productsvc.BuildManifest(c.Request.Context(), db, productID)
	if err != nil {
		return err
	}
	fresh.Version = nextVersion
	return productsvc.CreateManifest(c.Request.Context(), db, fresh)
}

func newProductID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "pr_" + hex.EncodeToString(b)
}

func newAssetID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "as_" + hex.EncodeToString(b)
}

func newFileKey() string {
	b := make([]byte, 8) // 16 hex chars
	_, _ = rand.Read(b)
	return "pa_" + hex.EncodeToString(b)
}
GOEOF

# ---- 6. 改写 cmd/mvp/main.go (Phase 0 + Phase 1 + Phase 2 累加) ----
echo "[phase_2] step 6: rewrite cmd/mvp/main.go (extends Phase 0 + 1)"
cat > cmd/mvp/main.go <<'GOEOF'
// Package main is the MVP standalone binary extended across Phases 0, 1, and 2.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
// Phase 2 (2026-08-30): product / asset / manifest CRUD (6 routes).
//
// Runs on a separate port from the production BFF (default :18903 in Phase 2)
// so a crash here can't take down the main server. Doesn't touch main.go.
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
		// Phase 2 default: 18903 (Phase 1 was 18902, Phase 0 was 18901)
		port = "18903"
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
	products := NewProductHandler(db)

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

	// --- Phase 1 + Phase 2 routes requiring JWT + X-Tenant-Id ---
	scoped := api.Group("")
	scoped.Use(middleware.RequireJWT(jwtSvc))
	scoped.Use(middleware.TenantScope(db))
	// Phase 1:
	scoped.POST("/tenants/:id/members", tenants.AddMember)
	scoped.GET("/files/sign", files.Sign)
	// Phase 2:
	scoped.POST("/products", products.Create)
	scoped.GET("/products/:id", products.Get)
	scoped.POST("/products/:id/assets", products.UploadAsset)
	scoped.GET("/products/:id/assets", products.ListAssets)
	scoped.GET("/products/:id/manifest", products.GetManifest)
	scoped.PUT("/products/:id/manifest/:asset_id", products.CorrectAsset)

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_2 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
GOEOF

# ---- 7. go build ----
echo "[phase_2] step 7: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p2"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ]; then
    echo "[phase_2] build FAILED exit=${build_exit}"
    exit 4
fi
echo "[phase_2] build OK → ${BIN}"

# ---- 8. start binary (background) ----
echo "[phase_2] step 8: start binary on :18903"
pkill -f frameflow-bff-mvp-p2 2>/dev/null || true
sleep 1

WEIXIN_MOCK_AUTH=1 MVP_PORT=18903 MVP_DB_PATH="${DB_PATH}" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_2-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_2] server pid=${SERVER_PID}"

# Wait for /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18903/healthz" >/dev/null 2>&1; then
        echo "[phase_2] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_2] /healthz never came up — server log:" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_2-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    exit 5
fi

# ---- 9. run gate ----
echo "[phase_2] step 9: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 10. stop server ----
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_2] gate FAILED exit=${GATE_EXIT}"
    exit 1
fi

echo "[phase_2] DONE — gate green, server stopped"
exit 0
