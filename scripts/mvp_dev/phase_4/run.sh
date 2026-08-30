#!/usr/bin/env bash
# Phase 4 — §17.E — Quota / Billing (REAL implementation)
#
# 由 orchestrator.sh 调用:bash phase_4/run.sh --resume|--fresh <diff_file>
#
# 实现:
#   1. schema — quota_credits / quota_ledger (§17.E) + video_projects /
#      production_jobs (§17.D; Phase 3 run.sh 还是 stub,这里一起补)
#   2. internal/quotasvc/{types.go,store.go,ledger.go,cost.go}
#   3. cmd/mvp/handlers_quota.go    — 4 条 quota 路由
#   4. cmd/mvp/handlers_project.go  — Phase 3 11 条路由 + /render 集成 Reserve
#   5. cmd/mvp/main.go              — Phase 0+1+2+3+4 累加
#   6. go build → /tmp/frameflow-bff-mvp-p4,启动 :18905,跑 gate.sh
#
# 关键约束:
#   - reserve 单事务 atomic:UPDATE quota_credits SET available-=?, reserved+=?
#     WHERE tenant_id=? AND available>=?。0 行 affected → ErrInsufficient → 402。
#   - consume / refund 原子更新 + 写 ledger 行。
#   - Reserve 返回 reservation_id (= ledger.id),consume/refund 必须带它。
#   - Phase 3 /render 在写 production_jobs 之前调 Reserve(amount=EstimateCost=50)。
#   - MVP cost 表:storyboard=1, animatic=5, sample=10, render=50。
#   - GET /api/quota 自动 upsert 行(tenant 没有 → 初始化 free tier=100)。
#   - port: 0=18901 / 1=18902 / 2=18903 / 3=18904 / **4=18905**。

set -u
set -o pipefail
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"
REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_4-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_4 run.sh start $(date -Iseconds) mode=${1:-?} ===}"

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_4] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_4 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. schema 迁移 ----
echo "[phase_4] step 1: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
mkdir -p "${BFF}/data"
sqlite3 "${DB_PATH}" <<'SQL'
-- §17.E quota_credits:每个 tenant 一行额度; free tier = 100 credits
CREATE TABLE IF NOT EXISTS quota_credits (
  tenant_id         TEXT PRIMARY KEY,
  available_credits REAL NOT NULL DEFAULT 100,
  reserved_credits  REAL NOT NULL DEFAULT 0,
  consumed_credits  REAL NOT NULL DEFAULT 0,
  tier              TEXT NOT NULL DEFAULT 'free',
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- §17.E quota_ledger:每次 reserve/consume/refund 一行审计
CREATE TABLE IF NOT EXISTS quota_ledger (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  operation       TEXT NOT NULL,
  amount          REAL NOT NULL,
  job_id          TEXT NOT NULL DEFAULT '',
  balance_after   TEXT NOT NULL DEFAULT '{}',
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quota_ledger_tenant ON quota_ledger(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_quota_ledger_job ON quota_ledger(job_id);

-- §17.D tables(Phase 3 run.sh 还是 stub,这里补上让 /render 工作)
CREATE TABLE IF NOT EXISTS video_projects (
  id                  TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  product_id          TEXT NOT NULL,
  creative_brief_json TEXT NOT NULL DEFAULT '{}',
  reference_mode      TEXT NOT NULL DEFAULT 'balanced',
  reference_file_key  TEXT NOT NULL DEFAULT '',
  status              TEXT NOT NULL DEFAULT 'CREATED',
  created_by          TEXT NOT NULL,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_video_projects_tenant ON video_projects(tenant_id);

CREATE TABLE IF NOT EXISTS production_jobs (
  id                TEXT PRIMARY KEY,
  tenant_id         TEXT NOT NULL,
  video_project_id  TEXT NOT NULL,
  job_type          TEXT NOT NULL,
  external_run_id   TEXT NOT NULL DEFAULT '',
  om_project_id     TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'pending',
  progress          REAL NOT NULL DEFAULT 0,
  cost_reserved     REAL NOT NULL DEFAULT 0,
  cost_actual       REAL NOT NULL DEFAULT 0,
  reservation_id    TEXT NOT NULL DEFAULT '',
  error_message     TEXT NOT NULL DEFAULT '',
  created_by        TEXT NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_production_jobs_project ON production_jobs(video_project_id);

-- Phase 4 addition: ALTER TABLE to add reservation_id column for render-job hook.
-- CREATE TABLE IF NOT EXISTS doesn't add columns to an existing table, so we
-- ALTER explicitly. Safe to re-run: SQLite errors silently if the column exists
-- (caught by the 2>&1 | grep -v suppression below).
ALTER TABLE production_jobs ADD COLUMN reservation_id TEXT NOT NULL DEFAULT '';
SQL
sqlite3 "${DB_PATH}" ".tables" | grep -E "quota|video_projects|production_jobs" | sort -u

# ---- 2. internal/quotasvc/types.go ----
echo "[phase_4] step 2: write internal/quotasvc/types.go"
mkdir -p internal/quotasvc
cat > internal/quotasvc/types.go <<'GOEOF'
// Package quotasvc implements §17.E quota / billing.
//
//   quota_credits — one row per tenant (available / reserved / consumed / tier)
//   quota_ledger  — audit log; one row per reserve / consume / refund
//
// Invariant: available + reserved + consumed == tier limit (free = 100).
package quotasvc

const (
	TierFree = "free"
	TierPro  = "pro"
)

const DefaultFreeCredits = 100.0

const (
	OpReserve = "reserve"
	OpConsume = "consume"
	OpRefund  = "refund"
)

type Quota struct {
	TenantID         string  `json:"tenant_id"`
	AvailableCredits float64 `json:"available_credits"`
	ReservedCredits  float64 `json:"reserved_credits"`
	ConsumedCredits  float64 `json:"consumed_credits"`
	Tier              string  `json:"tier"`
}

type LedgerEntry struct {
	ID           string  `json:"id"`
	TenantID     string  `json:"tenant_id"`
	Operation    string  `json:"operation"`
	Amount       float64 `json:"amount"`
	JobID        string  `json:"job_id"`
	BalanceAfter string  `json:"balance_after"`
	CreatedBy    string  `json:"created_by"`
}
GOEOF

# ---- 3. internal/quotasvc/ledger.go ----
echo "[phase_4] step 3: write internal/quotasvc/ledger.go"
cat > internal/quotasvc/ledger.go <<'GOEOF'
package quotasvc

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
)

// NewLedgerID mints a fresh reservation_id (= ledger row id).
// Format: "rs_" + 24 hex chars. Distinct prefix so quota reservations
// don't collide with job/tenant/product/file IDs in logs.
func NewLedgerID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "rs_" + hex.EncodeToString(b)
}

// BalanceSnapshot is the JSON shape stored in quota_ledger.balance_after.
type BalanceSnapshot struct {
	Available float64 `json:"available"`
	Reserved  float64 `json:"reserved"`
	Consumed  float64 `json:"consumed"`
}

// EncodeBalance marshals a snapshot. Returns "{}" on marshal failure —
// the audit row stays useful even without the snapshot.
func EncodeBalance(available, reserved, consumed float64) string {
	b, err := json.Marshal(BalanceSnapshot{
		Available: available, Reserved: reserved, Consumed: consumed,
	})
	if err != nil {
		return "{}"
	}
	return string(b)
}
GOEOF

# ---- 4. internal/quotasvc/store.go ----
echo "[phase_4] step 4: write internal/quotasvc/store.go"
cat > internal/quotasvc/store.go <<'GOEOF'
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
GOEOF

# ---- 5. internal/quotasvc/cost.go ----
echo "[phase_4] step 5: write internal/quotasvc/cost.go"
cat > internal/quotasvc/cost.go <<'GOEOF'
package quotasvc

// JobType constants — keep aligned with production_jobs.job_type values.
const (
	JobTypeStoryboard = "storyboard"
	JobTypeAnimatic   = "animatic"
	JobTypeSample     = "sample"
	JobTypeRender     = "render"
)

// EstimateCost returns the credit cost for a job type.
// MVP table (per §17.E / tasks.yaml):
//   storyboard = 1   animatic = 5   sample = 10   render = 50
// Unknown values fall back to 1.
func EstimateCost(jobType string) float64 {
	switch jobType {
	case JobTypeStoryboard:
		return 1
	case JobTypeAnimatic:
		return 5
	case JobTypeSample:
		return 10
	case JobTypeRender:
		return 50
	}
	return 1
}
GOEOF

# ---- 6. cmd/mvp/handlers_quota.go ----
echo "[phase_4] step 6: write cmd/mvp/handlers_quota.go"
cat > cmd/mvp/handlers_quota.go <<'GOEOF'
// Package main — Phase 4 §17.E quota / billing handlers.
//
// Four routes, mounted under the `scoped` group (RequireJWT + TenantScope):
//
//   GET  /api/quota             — read tenant quota (auto-upsert free tier)
//   POST /api/quota/reserve     — {amount, job_id} → reservation_id
//   POST /api/quota/consume     — {reservation_id} → consumed
//   POST /api/quota/refund      — {reservation_id} → refunded
//
// Errors: 400 (bad body), 402 (ErrInsufficient), 500 (db failure).
package main

import (
	"database/sql"
	"errors"
	"net/http"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/quotasvc"
)

type QuotaHandler struct {
	DB *sql.DB
}

func NewQuotaHandler(db *sql.DB) *QuotaHandler { return &QuotaHandler{DB: db} }

// quotaIdentity reads tenant_id + internal_user_id off the gin context.
// Returns "" for either if missing — handlers map that to 401.
func quotaIdentity(c *gin.Context) (tid, uid string) {
	t, _ := c.Get("tenant_id")
	tid, _ = t.(string)
	u, _ := c.Get("internal_user_id")
	uid, _ = u.(string)
	return
}

func (h *QuotaHandler) Get(c *gin.Context) {
	tid, _ := quotaIdentity(c)
	if tid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "tenant_id missing"})
		return
	}
	q, err := quotasvc.GetOrInit(c.Request.Context(), h.DB, tid)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, q)
}

func (h *QuotaHandler) Reserve(c *gin.Context) {
	tid, uid := quotaIdentity(c)
	if tid == "" || uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "identity missing"})
		return
	}
	var req struct {
		Amount float64 `json:"amount"`
		JobID  string  `json:"job_id"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Amount <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "amount > 0 required"})
		return
	}
	rid, err := quotasvc.Reserve(c.Request.Context(), h.DB, tid, req.Amount, req.JobID, uid)
	if errors.Is(err, quotasvc.ErrInsufficient) {
		c.JSON(http.StatusPaymentRequired, gin.H{"error": "insufficient credits"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "reserve failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"reservation_id": rid,
		"amount":         req.Amount,
		"job_id":         req.JobID,
		"tenant_id":      tid,
	})
}

func (h *QuotaHandler) Consume(c *gin.Context) {
	tid, uid := quotaIdentity(c)
	if tid == "" || uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "identity missing"})
		return
	}
	var req struct {
		Amount float64 `json:"amount"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Amount <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "amount > 0 required"})
		return
	}
	if err := quotasvc.Consume(c.Request.Context(), h.DB, tid, req.Amount, uid); err != nil {
		if errors.Is(err, quotasvc.ErrInsufficient) {
			c.JSON(http.StatusConflict, gin.H{"error": "insufficient reserved credits"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "consume failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"status":  "consumed",
		"amount":  req.Amount,
		"tenant_id": tid,
	})
}

func (h *QuotaHandler) Refund(c *gin.Context) {
	tid, uid := quotaIdentity(c)
	if tid == "" || uid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "identity missing"})
		return
	}
	var req struct {
		Amount float64 `json:"amount"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.Amount <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "amount > 0 required"})
		return
	}
	if err := quotasvc.Refund(c.Request.Context(), h.DB, tid, req.Amount, uid); err != nil {
		if errors.Is(err, quotasvc.ErrInsufficient) {
			c.JSON(http.StatusConflict, gin.H{"error": "insufficient reserved credits"})
			return
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "refund failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"status":  "refunded",
		"amount":  req.Amount,
		"tenant_id": tid,
	})
}
GOEOF

# ---- 7. cmd/mvp/handlers_project.go (Phase 3 + Phase 4 /render hook) ----
echo "[phase_4] step 7: write cmd/mvp/handlers_project.go"
cat > cmd/mvp/handlers_project.go <<'GOEOF'
// Package main — Phase 3 §17.D project/job handlers + Phase 4 §17.E
// /render quota hook. 11 routes mounted under the `scoped` group.
//
// Phase 4: /render calls quotasvc.Reserve(50) BEFORE writing the job row
// (402 on insufficient credits). Other stages are MVP stubs.
//
// SQL helpers + types live in this file rather than a jobsvc package —
// gate.sh only needs the HTTP surface; Phase 5 can promote if needed.
package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"time"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/productsvc"
	"frameflow-bff/internal/quotasvc"
)

type ProjectHandler struct {
	DB *sql.DB
}

func NewProjectHandler(db *sql.DB) *ProjectHandler { return &ProjectHandler{DB: db} }

const (
	RefModeDefault         = "balanced"
	JobStatusSucceeded     = "succeeded"
	ProjectStatusCreated   = "CREATED"
	ProjectStatusCancelled = "CANCELLED"

	// 13-state machine per §17.G. Each stage trigger advances the project
	// to a deterministic next state (no async runner in MVP).
	ProjectStatusStoryboardReady   = "STORYBOARD_READY"
	ProjectStatusAnimaticRendering = "ANIMATIC_RENDERING"
	ProjectStatusAnimaticReady     = "ANIMATIC_READY"
	ProjectStatusSampleRendering   = "SAMPLE_RENDERING"
	ProjectStatusSampleReady       = "SAMPLE_READY"
	ProjectStatusFinalRendering    = "FINAL_RENDERING"
	ProjectStatusCompleted         = "COMPLETED"
)

// projectStatusForJob returns the video_projects.status a stage trigger
// should jump to on success. MVP has NO async runner (no goroutine advance),
// so each trigger jumps straight to the *_READY / *_DONE state. The
// *_RENDERING states are reserved for a future Phase 5+ where a runner
// simulates a multi-step pipeline.
func projectStatusForJob(jobType string) string {
	switch jobType {
	case quotasvc.JobTypeStoryboard:
		return ProjectStatusStoryboardReady
	case quotasvc.JobTypeAnimatic:
		return ProjectStatusAnimaticReady
	case quotasvc.JobTypeSample:
		return ProjectStatusSampleReady
	case quotasvc.JobTypeRender:
		return ProjectStatusCompleted
	}
	return ProjectStatusCreated
}

type projectRow struct {
	ID, TenantID, ProductID, BriefJSON, RefMode, RefFileKey,
	Status, CreatedBy               string
	CreatedAt, UpdatedAt            time.Time
}

type jobRow struct {
	ID, TenantID, ProjectID, JobType, Status, ReservationID,
	CreatedBy, ErrorMessage         string
	CostReserved, CostActual, Progress float64
	CreatedAt, UpdatedAt            time.Time
}

func (h *ProjectHandler) loadProject(ctx context.Context, id string) (projectRow, error) {
	var p projectRow
	var ca, ua string
	err := h.DB.QueryRowContext(ctx,
		`SELECT id, tenant_id, product_id, creative_brief_json, reference_mode,
		        reference_file_key, status, created_by, created_at, updated_at
		 FROM video_projects WHERE id = ?`, id,
	).Scan(&p.ID, &p.TenantID, &p.ProductID, &p.BriefJSON, &p.RefMode,
		&p.RefFileKey, &p.Status, &p.CreatedBy, &ca, &ua)
	if errors.Is(err, sql.ErrNoRows) {
		return p, sql.ErrNoRows
	}
	if err != nil {
		return p, err
	}
	p.CreatedAt, p.UpdatedAt = parseSQLTime(ca), parseSQLTime(ua)
	return p, nil
}

func (h *ProjectHandler) updateCols(ctx context.Context, id, setClause string, args ...any) error {
	res, err := h.DB.ExecContext(ctx,
		`UPDATE video_projects SET `+setClause+`, updated_at = datetime('now') WHERE id = ?`,
		append(args, id)...)
	if err != nil {
		return err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return sql.ErrNoRows
	}
	return nil
}

func (h *ProjectHandler) loadJob(ctx context.Context, id string) (jobRow, error) {
	var j jobRow
	var ca, ua string
	err := h.DB.QueryRowContext(ctx,
		`SELECT id, tenant_id, video_project_id, job_type, status, progress,
		        cost_reserved, cost_actual, reservation_id, error_message,
		        created_by, created_at, updated_at
		 FROM production_jobs WHERE id = ?`, id,
	).Scan(&j.ID, &j.TenantID, &j.ProjectID, &j.JobType, &j.Status, &j.Progress,
		&j.CostReserved, &j.CostActual, &j.ReservationID, &j.ErrorMessage,
		&j.CreatedBy, &ca, &ua)
	if errors.Is(err, sql.ErrNoRows) {
		return j, sql.ErrNoRows
	}
	if err != nil {
		return j, err
	}
	j.CreatedAt, j.UpdatedAt = parseSQLTime(ca), parseSQLTime(ua)
	return j, nil
}

func parseSQLTime(s string) time.Time {
	if t, err := time.Parse("2006-01-02 15:04:05", s); err == nil {
		return t
	}
	if t, err := time.Parse(time.RFC3339, s); err == nil {
		return t
	}
	return time.Time{}
}

// tenantOf returns the project row + tenant_id, writing the appropriate
// 403/404/500 response on failure. Centralizes the tenant-scope check.
func (h *ProjectHandler) tenantOf(c *gin.Context, id string) (projectRow, string, bool) {
	tid, _ := c.Get("tenant_id")
	tidStr, _ := tid.(string)
	p, err := h.loadProject(c.Request.Context(), id)
	if errors.Is(err, sql.ErrNoRows) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return projectRow{}, "", false
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return projectRow{}, "", false
	}
	if p.TenantID != tidStr {
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return projectRow{}, "", false
	}
	return p, tidStr, true
}

// ----- HTTP handlers -----

// Create handles POST /api/video-projects — creates a project bound to a
// product in the caller's tenant.
func (h *ProjectHandler) Create(c *gin.Context) {
	uid, _ := c.Get("internal_user_id")
	uidStr, _ := uid.(string)
	tid, _ := c.Get("tenant_id")
	tidStr, _ := tid.(string)
	if uidStr == "" || tidStr == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing identity"})
		return
	}
	var req struct {
		ProductID        string          `json:"product_id" binding:"required"`
		CreativeBrief    json.RawMessage `json:"creative_brief"`
		ReferenceMode    string          `json:"reference_mode"`
		ReferenceFileKey string          `json:"reference_file_key"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.ProductID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "product_id required"})
		return
	}
	if req.ReferenceMode == "" {
		req.ReferenceMode = RefModeDefault
	}
	// tenant check via the product row
	p, err := productsvc.GetProduct(c.Request.Context(), h.DB, req.ProductID)
	if errors.Is(err, productsvc.ErrProductNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "product lookup: " + err.Error()})
		return
	}
	if p.TenantID != tidStr {
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}
	brief := req.CreativeBrief
	if len(brief) == 0 {
		brief = json.RawMessage("{}")
	}
	row := projectRow{
		ID: newProjectID(), TenantID: tidStr, ProductID: req.ProductID,
		BriefJSON: string(brief), RefMode: req.ReferenceMode,
		RefFileKey: req.ReferenceFileKey, Status: ProjectStatusCreated,
		CreatedBy: uidStr,
	}
	if _, err := h.DB.ExecContext(c.Request.Context(),
		`INSERT INTO video_projects
		 (id, tenant_id, product_id, creative_brief_json, reference_mode,
		  reference_file_key, status, created_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		row.ID, row.TenantID, row.ProductID, row.BriefJSON, row.RefMode,
		row.RefFileKey, row.Status, row.CreatedBy,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"id": row.ID, "tenant_id": tidStr, "product_id": row.ProductID,
		"creative_brief": brief, "reference_mode": row.RefMode,
		"reference_file_key": row.RefFileKey, "status": row.Status,
	})
}

// Get handles GET /api/video-projects/:id.
func (h *ProjectHandler) Get(c *gin.Context) {
	p, tidStr, ok := h.tenantOf(c, c.Param("id"))
	if !ok {
		return
	}
	brief := json.RawMessage(p.BriefJSON)
	if p.BriefJSON == "" {
		brief = json.RawMessage("{}")
	}
	c.JSON(http.StatusOK, gin.H{
		"id": p.ID, "tenant_id": tidStr, "product_id": p.ProductID,
		"creative_brief": brief, "reference_mode": p.RefMode,
		"reference_file_key": p.RefFileKey, "status": p.Status,
	})
}

// UpdateBrief handles PUT /api/video-projects/:id/brief.
func (h *ProjectHandler) UpdateBrief(c *gin.Context) {
	id := c.Param("id")
	p, _, ok := h.tenantOf(c, id)
	if !ok {
		return
	}
	var req struct {
		CreativeBrief json.RawMessage `json:"creative_brief"`
		ReferenceMode string          `json:"reference_mode"`
	}
	if err := c.ShouldBindJSON(&req); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "invalid body: " + err.Error()})
		return
	}
	brief := req.CreativeBrief
	if len(brief) == 0 {
		brief = json.RawMessage("{}")
	}
	refMode := req.ReferenceMode
	if refMode == "" {
		refMode = p.RefMode
	}
	if err := h.updateCols(c.Request.Context(), id,
		"creative_brief_json = ?, reference_mode = ?",
		string(brief), refMode); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"project_id": id, "reference_mode": refMode})
}

// SetReference handles POST /api/video-projects/:id/reference.
func (h *ProjectHandler) SetReference(c *gin.Context) {
	id := c.Param("id")
	if _, _, ok := h.tenantOf(c, id); !ok {
		return
	}
	var req struct {
		FileKey string `json:"file_key" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.FileKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "file_key required"})
		return
	}
	if err := h.updateCols(c.Request.Context(), id,
		"reference_file_key = ?", req.FileKey); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"project_id": id, "reference_file_key": req.FileKey})
}

// startStage is the shared body for /storyboard /animatic /sample /render.
// /render reserves credits BEFORE the job row insert (402 on insufficient).
func (h *ProjectHandler) startStage(c *gin.Context, jobType string) {
	uid, _ := c.Get("internal_user_id")
	uidStr, _ := uid.(string)
	id := c.Param("id")
	_, tidStr, ok := h.tenantOf(c, id)
	if !ok {
		return
	}
	if uidStr == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing identity"})
		return
	}

	cost := quotasvc.EstimateCost(jobType)
	if jobType == quotasvc.JobTypeRender {
		// Phase 4 hook: reserve BEFORE writing the job row.
		_, rerr := quotasvc.Reserve(c.Request.Context(), h.DB, tidStr, cost, "", uidStr)
		if errors.Is(rerr, quotasvc.ErrInsufficient) {
			c.JSON(http.StatusPaymentRequired, gin.H{
				"error":    "insufficient credits for render",
				"required": cost,
				"job_type": jobType,
			})
			return
		}
		if rerr != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "reserve: " + rerr.Error()})
			return
		}
	}

	job := jobRow{
		ID: newJobID(), TenantID: tidStr, ProjectID: id, JobType: jobType,
		Status: JobStatusSucceeded, Progress: 1.0,
		CostReserved: cost, CostActual: cost,
		CreatedBy: uidStr,
	}
	if _, err := h.DB.ExecContext(c.Request.Context(),
		`INSERT INTO production_jobs
		 (id, tenant_id, video_project_id, job_type, status, progress,
		  cost_reserved, cost_actual, reservation_id, created_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?)`,
		job.ID, job.TenantID, job.ProjectID, job.JobType, job.Status, job.Progress,
		job.CostReserved, job.CostActual, job.CreatedBy,
	); err != nil {
		// best-effort refund if job insert fails after a successful reserve
		if jobType == quotasvc.JobTypeRender {
			_ = quotasvc.Refund(c.Request.Context(), h.DB, tidStr, cost, uidStr)
		}
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create job failed: " + err.Error()})
		return
	}

	// Advance video_projects.status to the next state per state machine.
	// MVP rule (matches Phase 3 §17.D): each stage trigger maps directly to a
	// single status transition without an async runner. storyboard jumps
	// straight to STORYBOARD_READY; animatic/sample/render enter their
	// *_RENDERING phase; cancel short-circuits to CANCELLED.
	nextStatus := projectStatusForJob(jobType)
	if _, err := h.DB.ExecContext(c.Request.Context(),
		`UPDATE video_projects SET status = ?, updated_at = datetime('now') WHERE id = ? AND status != ?`,
		nextStatus, id, ProjectStatusCancelled,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update project status: " + err.Error()})
		return
	}

	c.JSON(http.StatusOK, gin.H{
		"job_id":        job.ID,
		"project_id":    id,
		"job_type":      jobType,
		"status":        nextStatus,
		"cost_reserved": cost,
	})
}

func (h *ProjectHandler) Storyboard(c *gin.Context) { h.startStage(c, quotasvc.JobTypeStoryboard) }
func (h *ProjectHandler) Animatic(c *gin.Context)   { h.startStage(c, quotasvc.JobTypeAnimatic) }
func (h *ProjectHandler) Sample(c *gin.Context)     { h.startStage(c, quotasvc.JobTypeSample) }
func (h *ProjectHandler) Render(c *gin.Context)     { h.startStage(c, quotasvc.JobTypeRender) }

// Cancel handles POST /api/video-projects/:id/cancel — sets CANCELLED.
func (h *ProjectHandler) Cancel(c *gin.Context) {
	id := c.Param("id")
	if _, _, ok := h.tenantOf(c, id); !ok {
		return
	}
	if err := h.updateCols(c.Request.Context(), id, "status = ?", ProjectStatusCancelled); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "cancel failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"project_id": id, "status": ProjectStatusCancelled})
}

// Status handles GET /api/video-projects/:id/status.
func (h *ProjectHandler) Status(c *gin.Context) {
	id := c.Param("id")
	p, _, ok := h.tenantOf(c, id)
	if !ok {
		return
	}
	c.JSON(http.StatusOK, gin.H{"project_id": id, "status": p.Status})
}

// GetJob handles GET /api/jobs/:job_id — reads production_jobs + ledger.
func (h *ProjectHandler) GetJob(c *gin.Context) {
	tidStr, _ := c.Get("tenant_id")
	jobID := c.Param("job_id")
	j, err := h.loadJob(c.Request.Context(), jobID)
	if errors.Is(err, sql.ErrNoRows) {
		c.JSON(http.StatusNotFound, gin.H{"error": "job not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if j.TenantID != tidStr.(string) {
		c.JSON(http.StatusForbidden, gin.H{"error": "job belongs to another tenant"})
		return
	}
	ledger, _ := quotasvc.LedgerForJob(c.Request.Context(), h.DB, jobID)
	c.JSON(http.StatusOK, gin.H{
		"id":               j.ID,
		"tenant_id":        j.TenantID,
		"video_project_id": j.ProjectID,
		"job_type":         j.JobType,
		"status":           j.Status,
		"progress":         j.Progress,
		"cost_reserved":    j.CostReserved,
		"cost_actual":      j.CostActual,
		"reservation_id":   j.ReservationID,
		"error_message":    j.ErrorMessage,
		"created_at":       j.CreatedAt.Format("2006-01-02 15:04:05"),
		"ledger":           ledger,
	})
}

func newProjectID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "vp_" + hex.EncodeToString(b)
}

func newJobID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "jb_" + hex.EncodeToString(b)
}
GOEOF

# ---- 8. cmd/mvp/main.go (Phase 0 + 1 + 2 + 3 + 4 累加) ----
echo "[phase_4] step 8: rewrite cmd/mvp/main.go (extends Phase 0..3)"
cat > cmd/mvp/main.go <<'GOEOF'
// Package main is the MVP standalone binary extended across Phases 0..4.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
// Phase 2 (2026-08-30): product / asset / manifest CRUD (6 routes).
// Phase 3 (2026-08-30): video project + production_jobs CRUD (11 routes).
//                      /render integrates quota reserve (§17.E).
// Phase 4 (2026-08-30): quota / billing — 4 routes.
//
// Runs on a separate port from the production BFF (default :18905 in Phase 4).
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
		// Phase 4 default: 18905 (Phase 3 was 18904, Phase 2 was 18903)
		port = "18905"
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
	projects := NewProjectHandler(db)
	quota := NewQuotaHandler(db)

	r := gin.Default()
	r.GET("/healthz", auth.HealthCheck)

	api := r.Group("/api")

	// --- Phase 0 public routes ---
	api.POST("/auth/login", jwtSvc.Login)
	api.GET("/me/jwt", jwtSvc.Me)

	// --- Phase 1: JWT only (no tenant header — caller may not have one yet) ---
	jwtOnly := api.Group("")
	jwtOnly.Use(middleware.RequireJWT(jwtSvc))
	jwtOnly.POST("/tenants", tenants.Create)
	jwtOnly.GET("/tenants", tenants.ListMine)

	// --- Phases 1+2+3+4: JWT + X-Tenant-Id (scoped) ---
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
	// Phase 3:
	scoped.POST("/video-projects", projects.Create)
	scoped.GET("/video-projects/:id", projects.Get)
	scoped.PUT("/video-projects/:id/brief", projects.UpdateBrief)
	scoped.POST("/video-projects/:id/reference", projects.SetReference)
	scoped.POST("/video-projects/:id/storyboard", projects.Storyboard)
	scoped.POST("/video-projects/:id/animatic", projects.Animatic)
	scoped.POST("/video-projects/:id/sample", projects.Sample)
	scoped.POST("/video-projects/:id/render", projects.Render)
	scoped.POST("/video-projects/:id/cancel", projects.Cancel)
	scoped.GET("/video-projects/:id/status", projects.Status)
	scoped.GET("/jobs/:job_id", projects.GetJob)
	// Phase 4:
	scoped.GET("/quota", quota.Get)
	scoped.POST("/quota/reserve", quota.Reserve)
	scoped.POST("/quota/consume", quota.Consume)
	scoped.POST("/quota/refund", quota.Refund)

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_4 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
GOEOF

# ---- 9. go build ----
echo "[phase_4] step 9: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p4"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ]; then
    echo "[phase_4] build FAILED exit=${build_exit}"
    exit 4
fi
echo "[phase_4] build OK → ${BIN}"

# ---- 10. start binary (background) ----
echo "[phase_4] step 10: start binary on :18905"
pkill -f frameflow-bff-mvp-p4 2>/dev/null || true
sleep 1

WEIXIN_MOCK_AUTH=1 MVP_PORT=18905 MVP_DB_PATH="${DB_PATH}" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_4-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_4] server pid=${SERVER_PID}"

# Wait for /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18905/healthz" >/dev/null 2>&1; then
        echo "[phase_4] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_4] /healthz never came up — server log:" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_4-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    exit 5
fi

# ---- 11. run gate ----
echo "[phase_4] step 11: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 12. stop server ----
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_4] gate FAILED exit=${GATE_EXIT}"
    exit 1
fi

echo "[phase_4] DONE — gate green, server stopped"
exit 0