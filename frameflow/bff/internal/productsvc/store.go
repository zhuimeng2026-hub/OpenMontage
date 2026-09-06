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
