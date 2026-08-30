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
