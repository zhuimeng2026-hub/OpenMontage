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
