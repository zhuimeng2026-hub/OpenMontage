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
