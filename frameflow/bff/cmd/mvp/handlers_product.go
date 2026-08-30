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
