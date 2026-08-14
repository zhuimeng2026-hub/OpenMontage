package mcp

import (
	"database/sql"
	"fmt"
	"time"
)

type RenderJobStore struct{ db *sql.DB }

func NewRenderJobStore(db *sql.DB) *RenderJobStore {
	if db == nil {
		return nil
	}
	return &RenderJobStore{db: db}
}

func (s *RenderJobStore) Record(sessionID string, job RenderJob) error {
	if s == nil || s.db == nil {
		return nil
	}
	if sessionID == "" || job.JobID == "" {
		return fmt.Errorf("render job requires session_id and job_id")
	}
	created := job.CreatedAt
	if created.IsZero() {
		created = time.Now().UTC()
	}
	_, err := s.db.Exec(`INSERT INTO render_jobs(session_id,job_id,batch_id,project_id,name,res,status,created_at)
VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(session_id,job_id) DO UPDATE SET batch_id=excluded.batch_id,project_id=excluded.project_id,name=excluded.name,res=excluded.res,status=excluded.status,created_at=excluded.created_at`, sessionID, job.JobID, job.BatchID, job.ProjectID, job.Name, job.Res, job.Status, created.Format(time.RFC3339Nano))
	return err
}

func (s *RenderJobStore) List(sessionID string) ([]*RenderJob, error) {
	if s == nil || s.db == nil {
		return nil, nil
	}
	rows, err := s.db.Query(`SELECT job_id,batch_id,project_id,name,res,status,created_at FROM render_jobs WHERE session_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100`, sessionID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var jobs []*RenderJob
	for rows.Next() {
		var j RenderJob
		var created string
		if err := rows.Scan(&j.JobID, &j.BatchID, &j.ProjectID, &j.Name, &j.Res, &j.Status, &created); err != nil {
			return nil, err
		}
		j.CreatedAt, err = time.Parse(time.RFC3339Nano, created)
		if err != nil {
			return nil, err
		}
		jobs = append(jobs, &j)
	}
	return jobs, rows.Err()
}

func (s *RenderJobStore) UpdateStatus(sessionID, jobID, status string) error {
	if s == nil || s.db == nil {
		return nil
	}
	_, err := s.db.Exec(`UPDATE render_jobs SET status=? WHERE session_id=? AND job_id=?`, status, sessionID, jobID)
	return err
}
