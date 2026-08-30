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
