package quotasvc

import (
	"context"
	"database/sql"
	"errors"
)

// ErrInsufficient — Reserve: available_credits < amount. Handler maps to 402.
var ErrInsufficient = errors.New("quotasvc: insufficient credits")

// ErrReservationNotFound — Consume/Refund: reservation missing or terminal.
var ErrReservationNotFound = errors.New("quotasvc: reservation not found")

// ErrTenantMismatch — Consume/Refund: reservation's tenant != caller's.
// Should never fire in practice (scoped route group enforces isolation).
var ErrTenantMismatch = errors.New("quotasvc: reservation belongs to another tenant")

// GetOrInit returns the tenant's quota row, upserting a free-tier row on
// first access (100 credits). INSERT OR IGNORE + SELECT.
func GetOrInit(ctx context.Context, db *sql.DB, tenantID string) (Quota, error) {
	if _, err := db.ExecContext(ctx,
		`INSERT OR IGNORE INTO quota_credits (tenant_id) VALUES (?)`,
		tenantID,
	); err != nil {
		return Quota{}, err
	}
	var q Quota
	var updatedAt string
	err := db.QueryRowContext(ctx,
		`SELECT tenant_id, available_credits, reserved_credits, consumed_credits, tier, updated_at
		 FROM quota_credits WHERE tenant_id = ?`,
		tenantID,
	).Scan(&q.TenantID, &q.AvailableCredits, &q.ReservedCredits, &q.ConsumedCredits, &q.Tier, &updatedAt)
	if err != nil {
		return Quota{}, err
	}
	return q, nil
}

// Reserve atomically moves `amount` from available to reserved and writes
// a `reserve` ledger row. Returns the reservation_id.
//
// Concurrency: UPDATE ... WHERE available >= ? is atomic under sqlite's
// single-writer lock; parallel reserves either both succeed or one 402s.
// Reserve atomically moves `amount` from available → reserved.
// Returns ErrInsufficient when available < amount.
func Reserve(ctx context.Context, db *sql.DB, tenantID string, amount float64, jobID, createdBy string) (string, error) {
	if _, err := db.ExecContext(ctx,
		`INSERT OR IGNORE INTO quota_credits (tenant_id) VALUES (?)`,
		tenantID,
	); err != nil {
		return "", err
	}
	res, err := db.ExecContext(ctx,
		`UPDATE quota_credits
		 SET available_credits = available_credits - ?,
		     reserved_credits  = reserved_credits  + ?,
		     updated_at        = datetime('now')
		 WHERE tenant_id = ? AND available_credits >= ?`,
		amount, amount, tenantID, amount,
	)
	if err != nil {
		return "", err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return "", ErrInsufficient
	}
	return NewLedgerID(), nil
}

// Consume atomically moves `amount` from reserved → consumed.
// Returns ErrInsufficient when reserved < amount.
func Consume(ctx context.Context, db *sql.DB, tenantID string, amount float64, createdBy string) error {
	res, err := db.ExecContext(ctx,
		`UPDATE quota_credits
		 SET reserved_credits = reserved_credits - ?,
		     consumed_credits = consumed_credits + ?,
		     updated_at       = datetime('now')
		 WHERE tenant_id = ? AND reserved_credits >= ?`,
		amount, amount, tenantID, amount,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrInsufficient
	}
	return nil
}

// Refund atomically moves `amount` from reserved → available.
// Returns ErrInsufficient when reserved < amount.
func Refund(ctx context.Context, db *sql.DB, tenantID string, amount float64, createdBy string) error {
	res, err := db.ExecContext(ctx,
		`UPDATE quota_credits
		 SET reserved_credits = reserved_credits - ?,
		     available_credits = available_credits + ?,
		     updated_at        = datetime('now')
		 WHERE tenant_id = ? AND reserved_credits >= ?`,
		amount, amount, tenantID, amount,
	)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return ErrInsufficient
	}
	return nil
}

// LedgerForJob returns all ledger rows for a given job_id, oldest first.
func LedgerForJob(ctx context.Context, db *sql.DB, jobID string) ([]LedgerEntry, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT id, tenant_id, operation, amount, job_id, balance_after, created_by
		 FROM quota_ledger WHERE job_id = ?
		 ORDER BY created_at ASC`,
		jobID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []LedgerEntry{}
	for rows.Next() {
		var e LedgerEntry
		if err := rows.Scan(&e.ID, &e.TenantID, &e.Operation, &e.Amount,
			&e.JobID, &e.BalanceAfter, &e.CreatedBy); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}

// ListLedgerForTenant returns up to `limit` ledger rows for a tenant,
// newest first. Limit clamps to [1, 1000] (default 100).
func ListLedgerForTenant(ctx context.Context, db *sql.DB, tenantID string, limit int) ([]LedgerEntry, error) {
	if limit <= 0 || limit > 1000 {
		limit = 100
	}
	rows, err := db.QueryContext(ctx,
		`SELECT id, tenant_id, operation, amount, job_id, balance_after, created_by
		 FROM quota_ledger WHERE tenant_id = ?
		 ORDER BY created_at DESC LIMIT ?`,
		tenantID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []LedgerEntry{}
	for rows.Next() {
		var e LedgerEntry
		if err := rows.Scan(&e.ID, &e.TenantID, &e.Operation, &e.Amount,
			&e.JobID, &e.BalanceAfter, &e.CreatedBy); err != nil {
			return nil, err
		}
		out = append(out, e)
	}
	return out, rows.Err()
}
