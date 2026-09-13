// Package main — Phase 3 §17.D project/job handlers + Phase 4 §17.E
// /render quota hook + Phase 6 real MCP preview rendering.
//
// 11 routes mounted under the `scoped` group.
//
// Phase 4: /render calls quotasvc.Reserve(50) BEFORE writing the job row
// (402 on insufficient credits). Other stages are MVP stubs.
//
// Phase 6: StartStage now fires an async runner that calls OpenMontage MCP
// video_compose via mvpclient. The handler returns immediately with
// status=*_RENDERING + job_id; the runner advances to *_READY and stamps
// artifacts_json on success. Render failures refund the reserved credits.
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

	"frameflow-bff/internal/jobsvc"
	"frameflow-bff/internal/mvpclient"
	"frameflow-bff/internal/productsvc"
	"frameflow-bff/internal/quotasvc"
)

// ProjectHandler exposes the §17.D routes.
//
// Phase 6: MCP + Poller are injected from main() so a missing MCP_BASE_URL
// fails startup loudly (MustNew) rather than silently degrading later.
// DB must always be set; nil MCP/Poller is treated as "MVP stub mode" by
// StartStage (returns 503 immediately).
type ProjectHandler struct {
	DB     *sql.DB
	MCP    *mvpclient.Client
	Poller *mvpclient.Poller
}

// NewProjectHandler is the canonical constructor (Phase 3 compat — no MCP).
// For Phase 6 production wiring, use NewProjectHandlerWithMCP.
func NewProjectHandler(db *sql.DB) *ProjectHandler { return &ProjectHandler{DB: db} }

// NewProjectHandlerWithMCP wires the Phase 6 runner dependencies.
func NewProjectHandlerWithMCP(db *sql.DB, mcpClient *mvpclient.Client, poller *mvpclient.Poller) *ProjectHandler {
	return &ProjectHandler{DB: db, MCP: mcpClient, Poller: poller}
}

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
// should jump to IMMEDIATELY after the handler validates the transition.
//
// Phase 6: with the async runner, the handler sets the project to the
// intermediate *_RENDERING state and the runner pushes it to the *_READY
// state on MCP success. storyboard is the exception — there is no
// STORYBOARD_RENDERING in the §17.G enum, so it goes straight to READY
// and the runner just stamps artifacts onto the job row.
func projectStatusForJob(jobType string) string {
	switch jobType {
	case quotasvc.JobTypeStoryboard:
		return ProjectStatusStoryboardReady
	case quotasvc.JobTypeAnimatic:
		return ProjectStatusAnimaticRendering
	case quotasvc.JobTypeSample:
		return ProjectStatusSampleRendering
	case quotasvc.JobTypeRender:
		return ProjectStatusFinalRendering
	case "cancel":
		return ProjectStatusCancelled
	}
	return ProjectStatusCreated
}

type projectRow struct {
	ID, TenantID, ProductID, BriefJSON, RefMode, RefFileKey,
	Status, CreatedBy               string
	ApprovedBy                       sql.NullString
	CreatedAt, UpdatedAt            time.Time
}

type jobRow struct {
	ID, TenantID, ProjectID, JobType, Status, ReservationID,
	CreatedBy, ErrorMessage         string
	CostReserved, CostActual, Progress float64
	ExternalRunID, OMProjectID, ArtifactsJSON string
	CreatedAt, UpdatedAt            time.Time
}

func (h *ProjectHandler) loadProject(ctx context.Context, id string) (projectRow, error) {
	var p projectRow
	var ca, ua string
	var approvedBy sql.NullString
	err := h.DB.QueryRowContext(ctx,
		`SELECT id, tenant_id, product_id, creative_brief_json, reference_mode,
		        reference_file_key, status, created_by,
		        approved_by, created_at, updated_at
		 FROM video_projects WHERE id = ?`, id,
	).Scan(&p.ID, &p.TenantID, &p.ProductID, &p.BriefJSON, &p.RefMode,
		&p.RefFileKey, &p.Status, &p.CreatedBy,
		&approvedBy, &ca, &ua)
	if errors.Is(err, sql.ErrNoRows) {
		return p, sql.ErrNoRows
	}
	if err != nil {
		return p, err
	}
	p.ApprovedBy = approvedBy
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
	var extRun, omProj, artJSON sql.NullString
	err := h.DB.QueryRowContext(ctx,
		`SELECT id, tenant_id, video_project_id, job_type, status, progress,
		        cost_reserved, cost_actual, reservation_id, error_message,
		        external_run_id, om_project_id, artifacts_json,
		        created_by, created_at, updated_at
		 FROM production_jobs WHERE id = ?`, id,
	).Scan(&j.ID, &j.TenantID, &j.ProjectID, &j.JobType, &j.Status, &j.Progress,
		&j.CostReserved, &j.CostActual, &j.ReservationID, &j.ErrorMessage,
		&extRun, &omProj, &artJSON,
		&j.CreatedBy, &ca, &ua)
	if errors.Is(err, sql.ErrNoRows) {
		return j, sql.ErrNoRows
	}
	if err != nil {
		return j, err
	}
	if extRun.Valid {
		j.ExternalRunID = extRun.String
	}
	if omProj.Valid {
		j.OMProjectID = omProj.String
	}
	if artJSON.Valid {
		j.ArtifactsJSON = artJSON.String
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
//
// Phase 6: the handler inserts a job row with status=running, advances the
// project to the immediate next state (*_RENDERING or STORYBOARD_READY),
// then fires an async runner that talks to upstream MCP. The handler
// returns immediately so the client can poll GET /api/video-projects/:id/status.
func (h *ProjectHandler) StartStage(c *gin.Context, jobType string) {
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

	// Phase 6 guard: if MCP is not wired (e.g. MVP stub mode), fail loud
	// rather than silently 200 the request. Per plan §8.2.
	if h.MCP == nil || h.Poller == nil {
		c.JSON(http.StatusServiceUnavailable, gin.H{
			"error":    "MCP client not configured — start server with MCP_BASE_URL and MCP_API_TOKEN",
			"job_type": jobType,
		})
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

	// Phase 6: status=running + progress=0 — runner advances to succeeded/1.0.
	job := jobRow{
		ID: newJobID(), TenantID: tidStr, ProjectID: id, JobType: jobType,
		Status: "running", Progress: 0,
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

	// Advance video_projects.status to the immediate next state per §17.D
	// state machine. storyboard jumps straight to STORYBOARD_READY (no
	// STORYBOARD_RENDERING state in §17.G); animatic/sample/render enter
	// their *_RENDERING phase for the runner to push to *_READY.
	nextStatus := projectStatusForJob(jobType)
	if _, err := h.DB.ExecContext(c.Request.Context(),
		`UPDATE video_projects SET status = ?, updated_at = datetime('now') WHERE id = ? AND status != ?`,
		nextStatus, id, ProjectStatusCancelled,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "update project status: " + err.Error()})
		return
	}

	// Snapshot the inputs the runner needs (req ctx is gone after handler returns).
	proj, _ := jobsvc.GetProject(c.Request.Context(), h.DB, id)
	go jobsvc.RunJob(jobsvc.RunJobParams{
		DB:               h.DB,
		MCP:              h.MCP,
		Poller:           h.Poller,
		JobID:            job.ID,
		ProjectID:        id,
		JobType:          jobType,
		TenantID:         tidStr,
		Cost:             cost,
		CreatedBy:        uidStr,
		CurrentStatus:    nextStatus,
		BriefJSON:        proj.CreativeBriefJSON,
		ReferenceFileKey: proj.ReferenceFileKey,
		ReferenceMode:    proj.ReferenceMode,
	})

	c.JSON(http.StatusOK, gin.H{
		"job_id":        job.ID,
		"project_id":    id,
		"job_type":      jobType,
		"status":        nextStatus,
		"cost_reserved": cost,
		"async":         true,
	})
}

func (h *ProjectHandler) Storyboard(c *gin.Context) { h.StartStage(c, quotasvc.JobTypeStoryboard) }
func (h *ProjectHandler) Animatic(c *gin.Context)   { h.StartStage(c, quotasvc.JobTypeAnimatic) }
func (h *ProjectHandler) Sample(c *gin.Context)     { h.StartStage(c, quotasvc.JobTypeSample) }
func (h *ProjectHandler) Render(c *gin.Context)     { h.StartStage(c, quotasvc.JobTypeRender) }

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

// Approve handles POST /api/video-projects/:id/approve — Phase 7.
//
//
// Transitions the project from SAMPLE_READY → WAITING_APPROVAL (idempotent:
// calling from WAITING_APPROVAL is a no-op + returns 200). Records the
// approver's internal_user_id + ISO timestamp in approved_by / approved_at.
//
// Does NOT trigger any MCP call — approval is purely a Go-side state
// transition. After approval the client can trigger /render.
//
// Errors:
//   - 401 if RequireJWT didn't set internal_user_id
//   - 403 if the project belongs to another tenant
//   - 404 if the project doesn't exist
//   - 409 if the project is in a state that doesn't allow approval
//     (e.g. CREATED, STORYBOARD_READY, FINAL_RENDERING, COMPLETED, FAILED,
//     CANCELLED) — only SAMPLE_READY and WAITING_APPROVAL are legal.
func (h *ProjectHandler) Approve(c *gin.Context) {
	uid, _ := c.Get("internal_user_id")
	uidStr, _ := uid.(string)
	if uidStr == "" {
		c.JSON(http.StatusUnauthorized, gin.H{"error": "missing identity"})
		return
	}
	id := c.Param("id")
	p, _, ok := h.tenantOf(c, id)
	if !ok {
		return
	}

	next, err := jobsvc.Advance(p.Status, "approve")
	if err != nil {
		c.JSON(http.StatusConflict, gin.H{
			"error":          "illegal transition from " + p.Status + " via approve",
			"allowed_from":   []string{jobsvc.StatusSampleReady, jobsvc.StatusWaitingApproval},
			"current_status": p.Status,
		})
		return
	}

	// Atomically: stamp approved_by/approved_at + advance status. The
	// approved_by column stays stable across re-approval (uses uidStr).
	if _, err := h.DB.ExecContext(c.Request.Context(),
		`UPDATE video_projects
		 SET status = ?, approved_by = ?, approved_at = datetime('now'), updated_at = datetime('now')
		 WHERE id = ?`,
		next, uidStr, id,
	); err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": "approve failed: " + err.Error()})
		return
	}
	c.JSON(http.StatusOK, gin.H{
		"project_id":  id,
		"status":      next,
		"approved_by": uidStr,
		"approved_at": time.Now().UTC().Format(time.RFC3339),
	})
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
		"external_run_id":  j.ExternalRunID,
		"om_project_id":    j.OMProjectID,
		"artifacts_json":   j.ArtifactsJSON,
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
