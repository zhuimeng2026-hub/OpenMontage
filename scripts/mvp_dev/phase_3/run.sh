#!/usr/bin/env bash
# Phase 3 — §17.D — Project / Job 管理 (REAL implementation)
#
# 由 orchestrator.sh 调用:bash phase_3/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. schema 迁移 — CREATE TABLE video_projects / production_jobs
#   2. 写入 internal/jobsvc/{types.go,store.go,states.go,runner.go}
#      — Project/Job 类型 + 13+1 档 status 常量 + 状态机 Advance() + MVP runner
#   3. 写入 cmd/mvp/handlers_project.go — 11 条路由 handler
#   4. 改写 cmd/mvp/main.go — 累加 Phase 0+1+2+3 路由,挂 ProjectHandler
#   5. go build → 输出到 /tmp/frameflow-bff-mvp-p3
#   6. 启动 binary(后台, :18904) + 跑 gate.sh
#
# 设计要点(详见 tasks.yaml):
#   - 13+1 档 status 严格按 §17.G 枚举(AllStatuses)。
#   - 状态机 Advance(current, trigger) 只允许白名单内的下一态;非法转移
#     返回 ErrIllegalTransition。
#   - MVP job runner 不接 OpenMontage MCP(Phase 5 Agent Gateway 的活);
#     只 fire goroutine,sleep + UpdateJobProgress + UpdateProjectStatus。
#   - 所有读路径强制 tenant 检查(proj.TenantID == tid),否则 403。
#   - POST /cancel 任何状态都能调,置 CANCELLED(终态,不再前进)。
#   - StartStage 用 c.FullPath() 解析 stage 字段,避免注册 5 个 :stage 路由。

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
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_3-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_3 run.sh start $(date -Iseconds) mode=${1:-?} ==="

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_3] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_3 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# ---- 1. schema 迁移 ----
echo "[phase_3] step 1: schema migration"
DB_PATH="${BFF}/data/frameflow.db"
sqlite3 "${DB_PATH}" <<'SQL'
-- 视频项目
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
CREATE INDEX IF NOT EXISTS idx_video_projects_product ON video_projects(product_id);

-- 生产/预览/渲染 job — 统一 jobs 表,加 job_type 区分(MVP 简化,不分三张表)
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
  error_message     TEXT NOT NULL DEFAULT '',
  created_by        TEXT NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_production_jobs_project ON production_jobs(video_project_id);
CREATE INDEX IF NOT EXISTS idx_production_jobs_tenant ON production_jobs(tenant_id);
SQL
echo "[phase_3] schema verify:"
sqlite3 "${DB_PATH}" ".tables" | tr ' ' '\n' | grep -E "video_projects|production_jobs" | sort -u

# ---- 2. 写 internal/jobsvc/types.go ----
echo "[phase_3] step 2: write internal/jobsvc/types.go"
mkdir -p internal/jobsvc
cat > internal/jobsvc/types.go <<'GOEOF'
// Package jobsvc implements the §17.D project / job data model and state
// machine for the MVP standalone binary.
//
// Two tables (created via CREATE TABLE IF NOT EXISTS in Phase 3 run.sh):
//
//   video_projects   — one row per project, owned by a tenant + product
//   production_jobs  — storyboard / animatic / sample / render jobs
//
// Every read/write keeps a tenant_id stamp so the BFF's TenantScope middleware
// can enforce row-level isolation; cross-tenant reads must return 403 at the
// handler layer (the gate test relies on this for security).
package jobsvc

import "time"

// 13+1 status set (gate §17.G). MVP only *transitions* through a subset —
// CREATED → STORYBOARD_READY → *_RENDERING → *_READY → FINAL_RENDERING →
// COMPLETED, plus CANCELLED/FAILED terminals — but AllStatuses enumerates
// the full §17.G set so other code paths can validate incoming strings.
const (
	StatusCreated            = "CREATED"
	StatusAssetAnalyzing     = "ASSET_ANALYZING"
	StatusReferenceAnalyzing = "REFERENCE_ANALYZING"
	StatusPlanning           = "PLANNING"
	StatusStoryboardReady    = "STORYBOARD_READY"
	StatusAnimaticRendering  = "ANIMATIC_RENDERING"
	StatusAnimaticReady      = "ANIMATIC_READY"
	StatusSampleRendering    = "SAMPLE_RENDERING"
	StatusSampleReady        = "SAMPLE_READY"
	StatusWaitingApproval    = "WAITING_APPROVAL"
	StatusFinalRendering     = "FINAL_RENDERING"
	StatusCompleted          = "COMPLETED"
	StatusFailed             = "FAILED"
	StatusCancelled          = "CANCELLED"
)

// AllStatuses is the full 13+1 state set (gate §17.G).
var AllStatuses = []string{
	StatusCreated, StatusAssetAnalyzing, StatusReferenceAnalyzing,
	StatusPlanning, StatusStoryboardReady, StatusAnimaticRendering,
	StatusAnimaticReady, StatusSampleRendering, StatusSampleReady,
	StatusWaitingApproval, StatusFinalRendering, StatusCompleted,
	StatusFailed, StatusCancelled,
}

// Job types — used as the job_type column on production_jobs.
const (
	JobTypeStoryboard = "storyboard"
	JobTypeAnimatic   = "animatic"
	JobTypeSample     = "sample"
	JobTypeRender     = "render"
)

// Reference modes — controls how the brief + reference video are blended.
// "balanced" is the MVP default; description_first ignores the reference;
// reference_first hews tightly to the uploaded reference.
const (
	ReferenceModeDescriptionFirst = "description_first"
	ReferenceModeBalanced         = "balanced"
	ReferenceModeReferenceFirst   = "reference_first"
)

// Project is the in-memory shape of a row in video_projects.
type Project struct {
	ID                string    `json:"id"`
	TenantID          string    `json:"tenant_id"`
	ProductID         string    `json:"product_id"`
	CreativeBriefJSON string    `json:"creative_brief_json"`
	ReferenceMode     string    `json:"reference_mode"`
	ReferenceFileKey  string    `json:"reference_file_key"`
	Status            string    `json:"status"`
	CreatedBy         string    `json:"created_by"`
	CreatedAt         time.Time `json:"created_at"`
	UpdatedAt         time.Time `json:"updated_at"`
}

// Job is the in-memory shape of a row in production_jobs.
type Job struct {
	ID             string    `json:"id"`
	TenantID       string    `json:"tenant_id"`
	VideoProjectID string    `json:"video_project_id"`
	JobType        string    `json:"job_type"`
	ExternalRunID  string    `json:"external_run_id"`
	OMProjectID    string    `json:"om_project_id"`
	Status         string    `json:"status"`
	Progress       float64   `json:"progress"`
	CostReserved   float64   `json:"cost_reserved"`
	CostActual     float64   `json:"cost_actual"`
	ErrorMessage   string    `json:"error_message"`
	CreatedBy      string    `json:"created_by"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}
GOEOF

# ---- 3. 写 internal/jobsvc/states.go ----
echo "[phase_3] step 3: write internal/jobsvc/states.go"
cat > internal/jobsvc/states.go <<'GOEOF'
package jobsvc

import "errors"

// ErrIllegalTransition is returned by Advance when the trigger is not
// allowed from the current state.
var ErrIllegalTransition = errors.New("jobsvc: illegal state transition")

// Advance computes the next state given current + a job_type trigger.
//
// MVP rule: each trigger has a direct legal transition:
//
//	CREATED              --storyboard--> STORYBOARD_READY
//	  (skipping PLANNING is allowed in MVP)
//	STORYBOARD_READY     --animatic----> ANIMATIC_RENDERING
//	ANIMATIC_RENDERING   --animatic_done--> ANIMATIC_READY
//	ANIMATIC_READY       --sample------> SAMPLE_RENDERING
//	SAMPLE_RENDERING     --sample_done--> SAMPLE_READY
//	SAMPLE_READY         --render------> FINAL_RENDERING
//	FINAL_RENDERING      --render_done--> COMPLETED
//
// Cancel transitions from any non-terminal state to CANCELLED.
// Failed from any non-terminal via SetError (not modeled here — SetError
// writes FAILED directly).
//
// Returns ErrIllegalTransition if the trigger is not allowed from the
// current state. The returned (next, nil) is always the destination state;
// (current, ErrIllegalTransition) signals a reject — caller can decide to
// 409 or ignore.
func Advance(current, trigger string) (string, error) {
	switch trigger {
	case "storyboard":
		if current == StatusCreated || current == StatusPlanning {
			return StatusStoryboardReady, nil
		}
	case "animatic":
		if current == StatusStoryboardReady {
			return StatusAnimaticRendering, nil
		}
	case "animatic_done":
		if current == StatusAnimaticRendering {
			return StatusAnimaticReady, nil
		}
	case "sample":
		if current == StatusAnimaticReady {
			return StatusSampleRendering, nil
		}
	case "sample_done":
		if current == StatusSampleRendering {
			return StatusSampleReady, nil
		}
	case "render":
		if current == StatusSampleReady || current == StatusWaitingApproval {
			return StatusFinalRendering, nil
		}
	case "render_done":
		if current == StatusFinalRendering {
			return StatusCompleted, nil
		}
	case "cancel":
		if !isTerminal(current) {
			return StatusCancelled, nil
		}
	}
	return current, ErrIllegalTransition
}

// IsTerminal returns true if the status cannot transition further.
// Used by Advance("cancel", ...) and by the runner to short-circuit.
func IsTerminal(s string) bool {
	return isTerminal(s)
}

func isTerminal(s string) bool {
	return s == StatusCompleted || s == StatusFailed || s == StatusCancelled
}
GOEOF

# ---- 4. 写 internal/jobsvc/store.go ----
echo "[phase_3] step 4: write internal/jobsvc/store.go"
cat > internal/jobsvc/store.go <<'GOEOF'
package jobsvc

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"time"
)

// ErrProjectNotFound is returned by GetProject when no row exists.
var ErrProjectNotFound = errors.New("jobsvc: project not found")

// ErrJobNotFound is returned by GetJob when no row exists.
var ErrJobNotFound = errors.New("jobsvc: job not found")

// CreateProject inserts a video_projects row. id must already be minted.
func CreateProject(ctx context.Context, db *sql.DB, p Project) error {
	_, err := db.ExecContext(ctx,
		`INSERT INTO video_projects
		 (id, tenant_id, product_id, creative_brief_json, reference_mode,
		  reference_file_key, status, created_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		p.ID, p.TenantID, p.ProductID, p.CreativeBriefJSON, p.ReferenceMode,
		p.ReferenceFileKey, p.Status, p.CreatedBy,
	)
	return err
}

// GetProject fetches a project by id. Returns ErrProjectNotFound when missing.
// Callers MUST verify p.TenantID == caller's tid — store does NOT enforce
// cross-tenant isolation (the handler layer does, so it can return 403).
func GetProject(ctx context.Context, db *sql.DB, id string) (Project, error) {
	var p Project
	var createdAt, updatedAt string
	err := db.QueryRowContext(ctx,
		`SELECT id, tenant_id, product_id, creative_brief_json, reference_mode,
		        reference_file_key, status, created_by, created_at, updated_at
		 FROM video_projects WHERE id = ?`, id,
	).Scan(&p.ID, &p.TenantID, &p.ProductID, &p.CreativeBriefJSON, &p.ReferenceMode,
		&p.ReferenceFileKey, &p.Status, &p.CreatedBy, &createdAt, &updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return p, ErrProjectNotFound
	}
	if err != nil {
		return p, err
	}
	p.CreatedAt = parseSQLTime(createdAt)
	p.UpdatedAt = parseSQLTime(updatedAt)
	return p, nil
}

// UpdateProjectBrief overwrites creative_brief_json + reference_mode.
// brief is marshaled to JSON here so callers pass a plain map.
func UpdateProjectBrief(ctx context.Context, db *sql.DB, id string, brief map[string]any, referenceMode string) error {
	if referenceMode == "" {
		referenceMode = ReferenceModeBalanced
	}
	briefJSON, err := json.Marshal(brief)
	if err != nil {
		return err
	}
	res, err := db.ExecContext(ctx,
		`UPDATE video_projects
		 SET creative_brief_json = ?, reference_mode = ?, updated_at = datetime('now')
		 WHERE id = ?`,
		string(briefJSON), referenceMode, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrProjectNotFound
	}
	return nil
}

// UpdateProjectReference overwrites reference_file_key.
func UpdateProjectReference(ctx context.Context, db *sql.DB, id, fileKey string) error {
	res, err := db.ExecContext(ctx,
		`UPDATE video_projects
		 SET reference_file_key = ?, updated_at = datetime('now')
		 WHERE id = ?`,
		fileKey, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrProjectNotFound
	}
	return nil
}

// UpdateProjectStatus overwrites the status field. Used by the runner to
// push the project forward through the state machine.
func UpdateProjectStatus(ctx context.Context, db *sql.DB, id, status string) error {
	res, err := db.ExecContext(ctx,
		`UPDATE video_projects
		 SET status = ?, updated_at = datetime('now')
		 WHERE id = ?`,
		status, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrProjectNotFound
	}
	return nil
}

// CreateJob inserts a production_jobs row. id must already be minted.
func CreateJob(ctx context.Context, db *sql.DB, j Job) error {
	_, err := db.ExecContext(ctx,
		`INSERT INTO production_jobs
		 (id, tenant_id, video_project_id, job_type, external_run_id,
		  om_project_id, status, progress, cost_reserved, cost_actual,
		  error_message, created_by)
		 VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		j.ID, j.TenantID, j.VideoProjectID, j.JobType, j.ExternalRunID,
		j.OMProjectID, j.Status, j.Progress, j.CostReserved, j.CostActual,
		j.ErrorMessage, j.CreatedBy,
	)
	return err
}

// GetJob fetches a job by id. Returns ErrJobNotFound when missing.
// Callers MUST verify j.TenantID == caller's tid — store does NOT enforce
// cross-tenant isolation.
func GetJob(ctx context.Context, db *sql.DB, id string) (Job, error) {
	var j Job
	var createdAt, updatedAt string
	err := db.QueryRowContext(ctx,
		`SELECT id, tenant_id, video_project_id, job_type, external_run_id,
		        om_project_id, status, progress, cost_reserved, cost_actual,
		        error_message, created_by, created_at, updated_at
		 FROM production_jobs WHERE id = ?`, id,
	).Scan(&j.ID, &j.TenantID, &j.VideoProjectID, &j.JobType, &j.ExternalRunID,
		&j.OMProjectID, &j.Status, &j.Progress, &j.CostReserved, &j.CostActual,
		&j.ErrorMessage, &j.CreatedBy, &createdAt, &updatedAt)
	if errors.Is(err, sql.ErrNoRows) {
		return j, ErrJobNotFound
	}
	if err != nil {
		return j, err
	}
	j.CreatedAt = parseSQLTime(createdAt)
	j.UpdatedAt = parseSQLTime(updatedAt)
	return j, nil
}

// UpdateJobStatus overwrites the job's status field.
func UpdateJobStatus(ctx context.Context, db *sql.DB, id, status string) error {
	res, err := db.ExecContext(ctx,
		`UPDATE production_jobs
		 SET status = ?, updated_at = datetime('now')
		 WHERE id = ?`,
		status, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrJobNotFound
	}
	return nil
}

// UpdateJobProgress overwrites the job's progress field (0..1).
func UpdateJobProgress(ctx context.Context, db *sql.DB, id string, progress float64) error {
	res, err := db.ExecContext(ctx,
		`UPDATE production_jobs
		 SET progress = ?, updated_at = datetime('now')
		 WHERE id = ?`,
		progress, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrJobNotFound
	}
	return nil
}

// SetJobError marks the job as failed and stores the error message.
func SetJobError(ctx context.Context, db *sql.DB, id, msg string) error {
	res, err := db.ExecContext(ctx,
		`UPDATE production_jobs
		 SET status = ?, error_message = ?, updated_at = datetime('now')
		 WHERE id = ?`,
		"failed", msg, id,
	)
	if err != nil {
		return err
	}
	n, _ := res.RowsAffected()
	if n == 0 {
		return ErrJobNotFound
	}
	return nil
}

// ListJobsByProject returns all jobs bound to a project, oldest first.
func ListJobsByProject(ctx context.Context, db *sql.DB, videoProjectID string) ([]Job, error) {
	rows, err := db.QueryContext(ctx,
		`SELECT id, tenant_id, video_project_id, job_type, external_run_id,
		        om_project_id, status, progress, cost_reserved, cost_actual,
		        error_message, created_by, created_at, updated_at
		 FROM production_jobs WHERE video_project_id = ?
		 ORDER BY created_at ASC`, videoProjectID,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	out := []Job{}
	for rows.Next() {
		var j Job
		var createdAt, updatedAt string
		if err := rows.Scan(&j.ID, &j.TenantID, &j.VideoProjectID, &j.JobType,
			&j.ExternalRunID, &j.OMProjectID, &j.Status, &j.Progress,
			&j.CostReserved, &j.CostActual, &j.ErrorMessage, &j.CreatedBy,
			&createdAt, &updatedAt); err != nil {
			return nil, err
		}
		j.CreatedAt = parseSQLTime(createdAt)
		j.UpdatedAt = parseSQLTime(updatedAt)
		out = append(out, j)
	}
	return out, rows.Err()
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

# ---- 5. 写 internal/jobsvc/runner.go ----
echo "[phase_3] step 5: write internal/jobsvc/runner.go"
cat > internal/jobsvc/runner.go <<'GOEOF'
package jobsvc

import (
	"context"
	"database/sql"
	"log"
	"time"
)

// RunJobAsync starts a goroutine that simulates a job lifecycle:
//
//	t=0:        status already set to "running" by caller (in handler).
//	t=200ms:    progress=0.5
//	t=400ms:    progress=1.0, status="succeeded"
//	final:      Advance(currentStatus, jobType+"_done") → next project state
//
// Errors are not returned; they're stored in jobs.error_message.
//
// MVP doesn't call real OM/OpenClaw — this is a placeholder so the gate can
// verify the state machine advances. ctx.Background is used (NOT the
// request ctx) because the runner outlives the HTTP request — when the
// client disconnects, the job keeps going and lands in the terminal state.
func RunJobAsync(ctx context.Context, db *sql.DB, jobID, projectID, jobType, currentStatus string) {
	go func() {
		time.Sleep(200 * time.Millisecond)
		_ = UpdateJobProgress(ctx, db, jobID, 0.5)

		time.Sleep(200 * time.Millisecond)
		_ = UpdateJobProgress(ctx, db, jobID, 1.0)

		next, err := Advance(currentStatus, jobType+"_done")
		if err != nil {
			_ = SetJobError(ctx, db, jobID, "advance failed: "+err.Error())
			return
		}
		if err := UpdateJobStatus(ctx, db, jobID, "succeeded"); err != nil {
			log.Printf("[jobsvc] update job %s: %v", jobID, err)
			return
		}
		if err := UpdateProjectStatus(ctx, db, projectID, next); err != nil {
			log.Printf("[jobsvc] update project %s: %v", projectID, err)
		}
	}()
}
GOEOF

# ---- 6. 写 cmd/mvp/handlers_project.go ----
echo "[phase_3] step 6: write cmd/mvp/handlers_project.go"
cat > cmd/mvp/handlers_project.go <<'GOEOF'
// Package main — Phase 3 project / job handlers.
//
// Eleven routes, all mounted under the `scoped` group (RequireJWT + TenantScope):
//
//	POST  /api/video-projects                    — create a project (linked to product)
//	GET   /api/video-projects/:id                — read a project (tenant check)
//	PUT   /api/video-projects/:id/brief          — update creative_brief + reference_mode
//	POST  /api/video-projects/:id/reference      — record reference_file_key
//	POST  /api/video-projects/:id/storyboard     — advance state (shared handler)
//	POST  /api/video-projects/:id/animatic       — advance state (shared handler)
//	POST  /api/video-projects/:id/sample         — advance state (shared handler)
//	POST  /api/video-projects/:id/render         — advance state (shared handler)
//	POST  /api/video-projects/:id/cancel         — set CANCELLED (terminal)
//	GET   /api/video-projects/:id/status         — current project status
//	GET   /api/jobs/:job_id                      — read a job (tenant check)
//
// Five stage triggers share ONE handler (StartStage). Stage is read from
// c.FullPath() — splitting the registered route template on '/' and taking
// the last segment. This keeps the routing table flat: no `:stage` param,
// no ambiguity with `:id`.
package main

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"errors"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/filesvc"
	"frameflow-bff/internal/jobsvc"
)

// ProjectHandler exposes the §17.D routes.
type ProjectHandler struct {
	DB *sql.DB
}

// NewProjectHandler is the canonical constructor.
func NewProjectHandler(db *sql.DB) *ProjectHandler { return &ProjectHandler{DB: db} }

// createProjectReq is the body for POST /api/video-projects.
type createProjectReq struct {
	ProductID string `json:"product_id" binding:"required"`
}

// projectResp is the JSON shape returned by Create + Get.
type projectResp struct {
	ID                string `json:"id"`
	TenantID          string `json:"tenant_id"`
	ProductID         string `json:"product_id"`
	CreativeBriefJSON string `json:"creative_brief_json"`
	ReferenceMode     string `json:"reference_mode"`
	ReferenceFileKey  string `json:"reference_file_key"`
	Status            string `json:"status"`
}

// updateBriefReq is the body for PUT /api/video-projects/:id/brief.
type updateBriefReq struct {
	CreativeBrief map[string]any `json:"creative_brief" binding:"required"`
	ReferenceMode string         `json:"reference_mode"`
}

// Create handles POST /api/video-projects — creates a project, verifies
// the named product belongs to the caller's tenant.
func (h *ProjectHandler) Create(c *gin.Context) {
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	if uid == "" || tid == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing identity (RequireJWT+TenantScope must run first)"})
		return
	}

	var req createProjectReq
	if err := c.ShouldBindJSON(&req); err != nil || req.ProductID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "product_id required"})
		return
	}

	// Verify product exists AND belongs to caller's tenant.
	var prodTenant string
	err := h.DB.QueryRow(`SELECT tenant_id FROM products WHERE id = ?`, req.ProductID).Scan(&prodTenant)
	if errors.Is(err, sql.ErrNoRows) {
		c.JSON(http.StatusNotFound, gin.H{"error": "product not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "product lookup: " + err.Error()})
		return
	}
	if prodTenant != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "product belongs to another tenant"})
		return
	}

	p := jobsvc.Project{
		ID:                newProjectID(),
		TenantID:          tid,
		ProductID:         req.ProductID,
		CreativeBriefJSON: "{}",
		ReferenceMode:     jobsvc.ReferenceModeBalanced,
		Status:            jobsvc.StatusCreated,
		CreatedBy:         uid,
	}
	if err := jobsvc.CreateProject(c.Request.Context(), h.DB, p); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "create failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, projectResp{
		ID: p.ID, TenantID: p.TenantID, ProductID: p.ProductID,
		CreativeBriefJSON: p.CreativeBriefJSON, ReferenceMode: p.ReferenceMode,
		ReferenceFileKey: p.ReferenceFileKey, Status: p.Status,
	})
}

// Get handles GET /api/video-projects/:id — fetches a project, enforces tenant.
func (h *ProjectHandler) Get(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	id := c.Param("id")

	p, err := jobsvc.GetProject(c.Request.Context(), h.DB, id)
	if errors.Is(err, jobsvc.ErrProjectNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		// Cross-tenant probe — return 403 (matches gate expectation).
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return
	}
	c.JSON(http.StatusOK, projectResp{
		ID: p.ID, TenantID: p.TenantID, ProductID: p.ProductID,
		CreativeBriefJSON: p.CreativeBriefJSON, ReferenceMode: p.ReferenceMode,
		ReferenceFileKey: p.ReferenceFileKey, Status: p.Status,
	})
}

// UpdateBrief handles PUT /api/video-projects/:id/brief — overwrites
// creative_brief (full replace, not patch) + reference_mode.
func (h *ProjectHandler) UpdateBrief(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	id := c.Param("id")

	p, err := jobsvc.GetProject(c.Request.Context(), h.DB, id)
	if errors.Is(err, jobsvc.ErrProjectNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return
	}

	var req updateBriefReq
	if err := c.ShouldBindJSON(&req); err != nil || len(req.CreativeBrief) == 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "creative_brief required"})
		return
	}

	if err := jobsvc.UpdateProjectBrief(c.Request.Context(), h.DB, id, req.CreativeBrief, req.ReferenceMode); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update failed: " + err.Error()})
		return
	}
	// Verify the mode we just stored by re-reading.
	stored, err := jobsvc.GetProject(c.Request.Context(), h.DB, id)
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "re-read: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"id":            id,
		"reference_mode": stored.ReferenceMode,
	})
}

// SetReference handles POST /api/video-projects/:id/reference — binds
// reference_file_key. Verifies file_acl + tenant binding.
func (h *ProjectHandler) SetReference(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	id := c.Param("id")

	var req struct {
		FileKey string `json:"file_key" binding:"required"`
	}
	if err := c.ShouldBindJSON(&req); err != nil || req.FileKey == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "file_key required"})
		return
	}

	p, err := jobsvc.GetProject(c.Request.Context(), h.DB, id)
	if errors.Is(err, jobsvc.ErrProjectNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return
	}

	// Verify file_acl — file must belong to tenant (Phase 1 signed URL flow).
	fileTid, err := filesvc.LookupTenant(c.Request.Context(), h.DB, req.FileKey)
	if errors.Is(err, filesvc.ErrFileNotFound) {
		c.JSON(http.StatusBadRequest, gin.H{"error": "file_key not registered in file_acl"})
		return
	}
	if err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "file_key not registered in file_acl: " + err.Error()})
		return
	}
	if fileTid != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "file belongs to another tenant"})
		return
	}

	if err := jobsvc.UpdateProjectReference(c.Request.Context(), h.DB, id, req.FileKey); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{"id": id, "reference_file_key": req.FileKey})
}

// StartStage handles POST /api/video-projects/:id/<stage> — shared handler
// for storyboard/animatic/sample/render/cancel. Stage is derived from
// c.FullPath() (the registered route template), not c.Param("stage"), so
// we don't need a `:stage` placeholder that would shadow `:id` matching.
//
// Cancel is special: it skips the job runner and just sets CANCELLED.
func (h *ProjectHandler) StartStage(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	uidV, _ := c.Get("internal_user_id")
	uid, _ := uidV.(string)
	projectID := c.Param("id")
	stage := extractStage(c)

	p, err := jobsvc.GetProject(c.Request.Context(), h.DB, projectID)
	if errors.Is(err, jobsvc.ErrProjectNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return
	}

	if stage == "cancel" {
		if err := jobsvc.UpdateProjectStatus(c.Request.Context(), h.DB, projectID, jobsvc.StatusCancelled); err != nil {
			c.JSON(http.StatusInternalServerError, gin.H{"error": "cancel failed: " + err.Error()})
			return
		}
		c.JSON(http.StatusOK, gin.H{
			"project_id": projectID,
			"status":     jobsvc.StatusCancelled,
		})
		return
	}

	// Map stage → job_type. Reject unknown stages up-front.
	var jobType string
	switch stage {
	case "storyboard":
		jobType = jobsvc.JobTypeStoryboard
	case "animatic":
		jobType = jobsvc.JobTypeAnimatic
	case "sample":
		jobType = jobsvc.JobTypeSample
	case "render":
		jobType = jobsvc.JobTypeRender
	default:
		c.JSON(http.StatusBadRequest, gin.H{"error": "unknown stage: " + stage})
		return
	}

	// Validate the state transition BEFORE creating a job row.
	next, err := jobsvc.Advance(p.Status, stage)
	if err != nil {
		c.JSON(http.StatusConflict, gin.H{"error": "illegal transition from " + p.Status + " via " + stage})
		return
	}

	// Create the job row. status="running" — runner flips it to "succeeded".
	job := jobsvc.Job{
		ID:             newJobID(),
		TenantID:       tid,
		VideoProjectID: projectID,
		JobType:        jobType,
		Status:         "running",
		Progress:       0,
		CreatedBy:      uid,
	}
	if err := jobsvc.CreateJob(c.Request.Context(), h.DB, job); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "job create failed: " + err.Error()})
		return
	}

	// Push project forward to the immediate next state.
	//   storyboard: jump straight to STORYBOARD_READY (terminal for that stage — no runner needed)
	//   animatic/sample/render: jump to *_RENDERING (runner will progress to *_READY via *_done)
	var immediateStatus string
	if stage == "storyboard" {
		immediateStatus = jobsvc.StatusStoryboardReady
	} else {
		immediateStatus = next
	}
	if err := jobsvc.UpdateProjectStatus(c.Request.Context(), h.DB, projectID, immediateStatus); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "project status update failed: " + err.Error()})
		return
	}

	// Fire the runner for non-storyboard stages. storyboard is a single
	// synchronous "advance" — Advance() returned STORYBOARD_READY directly.
	if stage != "storyboard" {
		jobsvc.RunJobAsync(context.Background(), h.DB, job.ID, projectID, jobType, immediateStatus)
	}

	c.JSON(http.StatusOK, gin.H{
		"project_id": projectID,
		"job_id":     job.ID,
		"job_type":   jobType,
		"status":     immediateStatus,
	})
}

// GetStatus handles GET /api/video-projects/:id/status — returns the
// current project status (lightweight, no full project row).
func (h *ProjectHandler) GetStatus(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	id := c.Param("id")

	p, err := jobsvc.GetProject(c.Request.Context(), h.DB, id)
	if errors.Is(err, jobsvc.ErrProjectNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"project_id": p.ID,
		"status":     p.Status,
		"updated_at": p.UpdatedAt.Format("2006-01-02 15:04:05"),
	})
}

// GetJob handles GET /api/jobs/:job_id — fetches a job, enforces tenant.
func (h *ProjectHandler) GetJob(c *gin.Context) {
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	id := c.Param("job_id")

	j, err := jobsvc.GetJob(c.Request.Context(), h.DB, id)
	if errors.Is(err, jobsvc.ErrJobNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "job not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "lookup failed: " + err.Error()})
		return
	}
	if j.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "job belongs to another tenant"})
		return
	}
	c.JSON(http.StatusOK, j)
}

// extractStage pulls the last segment of c.FullPath() (e.g. "storyboard").
// Returns "" if the path can't be parsed — caller treats as unknown stage.
func extractStage(c *gin.Context) string {
	fp := c.FullPath()
	if fp == "" {
		return ""
	}
	parts := strings.Split(fp, "/")
	return parts[len(parts)-1]
}

// newProjectID mints a "vp_<24hex>" id for video_projects.
func newProjectID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "vp_" + hex.EncodeToString(b)
}

// newJobID mints a "jb_<24hex>" id for production_jobs.
func newJobID() string {
	b := make([]byte, 12)
	_, _ = rand.Read(b)
	return "jb_" + hex.EncodeToString(b)
}

// _ silences the "imported and not used" check when encoding/json
// becomes unused after future refactors. (Phase 3 doesn't currently need
// it in this file — UpdateBrief marshaling lives in store.go — but
// keeping the import makes the handler safe to extend.)
var _ = json.Marshal
GOEOF

# ---- 7. 改写 cmd/mvp/main.go (Phase 0 + Phase 1 + Phase 2 + Phase 3 累加) ----
echo "[phase_3] step 7: rewrite cmd/mvp/main.go (extends Phase 0+1+2)"
cat > cmd/mvp/main.go <<'GOEOF'
// Package main is the MVP standalone binary extended across Phases 0, 1, 2, and 3.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
// Phase 2 (2026-08-30): product / asset / manifest CRUD (6 routes).
// Phase 3 (2026-08-30): project / job CRUD + state machine (11 routes).
//
// Runs on a separate port from the production BFF (default :18904 in Phase 3)
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
		// Phase 3 default: 18904 (Phase 2 was 18903, 1 was 18902, 0 was 18901)
		port = "18904"
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

	// --- Phase 1 + Phase 2 + Phase 3 routes requiring JWT + X-Tenant-Id ---
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
	// Phase 3 — project / job:
	scoped.POST("/video-projects", projects.Create)
	scoped.GET("/video-projects/:id", projects.Get)
	scoped.PUT("/video-projects/:id/brief", projects.UpdateBrief)
	scoped.POST("/video-projects/:id/reference", projects.SetReference)
	// Five stage triggers share one handler; stage is read from c.FullPath().
	scoped.POST("/video-projects/:id/storyboard", projects.StartStage)
	scoped.POST("/video-projects/:id/animatic", projects.StartStage)
	scoped.POST("/video-projects/:id/sample", projects.StartStage)
	scoped.POST("/video-projects/:id/render", projects.StartStage)
	scoped.POST("/video-projects/:id/cancel", projects.StartStage)
	scoped.GET("/video-projects/:id/status", projects.GetStatus)
	scoped.GET("/jobs/:job_id", projects.GetJob)

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_3 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
GOEOF

# ---- 8. go build ----
echo "[phase_3] step 8: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p3"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ]; then
    echo "[phase_3] build FAILED exit=${build_exit}"
    exit 4
fi
if [ ! -x "${BIN}" ]; then
    echo "[phase_3] build FAILED — binary not produced" >&2
    exit 4
fi
echo "[phase_3] build OK → ${BIN}"

# ---- 9. start binary (background) ----
echo "[phase_3] step 9: start binary on :18904"
pkill -f frameflow-bff-mvp-p3 2>/dev/null || true
sleep 1

WEIXIN_MOCK_AUTH=1 MVP_PORT=18904 MVP_DB_PATH="${DB_PATH}" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_3-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_3] server pid=${SERVER_PID}"

# Wait for /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18904/healthz" >/dev/null 2>&1; then
        echo "[phase_3] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_3] /healthz never came up — server log:" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_3-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    exit 5
fi

# ---- 10. run gate ----
echo "[phase_3] step 10: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 11. stop server ----
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_3] gate FAILED exit=${GATE_EXIT}"
    exit 1
fi

echo "[phase_3] DONE — gate green, server stopped"
exit 0
