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
