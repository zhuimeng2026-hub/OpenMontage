// Package jobsvc — Phase 6 runner.
//
// Replaces the 400ms simulated lifecycle with real OpenMontage MCP calls via
// cmd/mvp/internal/mvpclient. The handler sets status=running + *_RENDERING
// synchronously, then fires RunJob — the goroutine drives MCP for the
// remainder of the lifecycle, advancing the project through Advance() and
// stamping artifacts onto the production_jobs row.
//
// Cost handling: the handler is responsible for Reserve(cost) BEFORE
// dispatching. This runner takes the cost as a parameter so it can:
//   - on success: Consume(cost) to convert reserved → consumed
//   - on failure: Refund(cost) to convert reserved → available
//
// Context: the runner always uses context.Background so it outlives the
// HTTP request. The mvpclient.Poller has its own deadline (default 600s),
// so the runner's ctx only serves the lifecycle of the goroutine.
package jobsvc

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"log"
	"time"

	"frameflow-bff/internal/mvpclient"
	"frameflow-bff/internal/quotasvc"
)

// RunJobParams carries everything the runner needs to drive one execution.
//   - DB             — SQLite handle (production_jobs + video_projects).
//   - MCP            — wrapper around upstream video_compose.
//   - Poller         — async handle waiter (uses the same MCP session).
//   - JobID          — production_jobs.id (the row to update).
//   - ProjectID      — video_projects.id (state-machine driver).
//   - JobType        — "storyboard" / "animatic" / "sample" / "render".
//   - TenantID       — for MCP request envelope + quota refund/consume.
//   - Cost           — credits reserved at handler time.
//   - CreatedBy      — for quota ledger attribution.
//   - CurrentStatus  — pre-render project status (the *_RENDERING enum).
//   - BriefJSON      — creative_brief_json snapshot (passed to MCP).
//   - ReferenceFileKey / ReferenceMode — forwarded to MCP.
type RunJobParams struct {
	DB              *sql.DB
	MCP             *mvpclient.Client
	Poller          *mvpclient.Poller
	JobID           string
	ProjectID       string
	JobType         string
	TenantID        string
	Cost            float64
	CreatedBy       string
	CurrentStatus   string
	BriefJSON       string
	ReferenceFileKey string
	ReferenceMode   string
}

// RunJob is the goroutine entry point. Mirrors the old RunJobAsync shape —
// the caller fires it with `go RunJob(...)` and continues.
//
// Failure modes:
//   - mvpclient.ErrUnavailable (MCP unreachable / 5xx):
//       SetJobError, UpdateProjectStatus(FAILED), and for "render" jobs
//       Refund(cost). For non-render stages, no quota is involved.
//   - mvpclient.ErrInvalidResponse / ErrTimeout / parse failure:
//       same fail-loud path.
//   - Advance illegal transition:
//       SetJobError, leave project status alone (handler already moved it).
//
// All errors are LOGGED but never returned — the goroutine cannot bubble
// them up to the HTTP client.
func RunJob(p RunJobParams) {
	if p.DB == nil || p.MCP == nil || p.Poller == nil {
		log.Printf("[jobsvc] RunJob: missing required param (db=%v mcp=%v poller=%v)", p.DB != nil, p.MCP != nil, p.Poller != nil)
		return
	}
	ctx := context.Background()
	req := mvpclient.PrepareRequest{
		ProjectID:         p.ProjectID,
		TenantID:          p.TenantID,
		CreativeBriefJSON: p.BriefJSON,
		ReferenceFileKey:  p.ReferenceFileKey,
		ReferenceMode:     p.ReferenceMode,
	}

	// 1. Dispatch to the right Prepare* method.
	var (
		res mvpclient.PrepareResult
		err error
	)
	switch p.JobType {
	case JobTypeStoryboard:
		res, err = p.MCP.PrepareStoryboard(ctx, req)
	case JobTypeAnimatic:
		res, err = p.MCP.PrepareAnimatic(ctx, req)
	case JobTypeSample:
		res, err = p.MCP.PrepareSample(ctx, req)
	case JobTypeRender:
		res, err = p.MCP.RenderFinal(ctx, req)
	default:
		_ = SetJobError(ctx, p.DB, p.JobID, "unknown job_type: "+p.JobType)
		return
	}
	if err != nil {
		failJob(ctx, p, "prepare: "+err.Error())
		return
	}

	// 2. Stamp the upstream run id immediately so /api/jobs/:id reflects
	//    state even before the run reaches a terminal state.
	if res.Artifact.ExternalRunID != "" {
		if uerr := UpdateJobExternalRunID(ctx, p.DB, p.JobID,
			res.Artifact.ExternalRunID, res.Artifact.OMProjectID); uerr != nil {
			log.Printf("[jobsvc] stamp external_run_id: %v", uerr)
		}
	}

	// 3. Sync fast-path — upstream returned a terminal artifact already.
	if res.Done && res.Artifact.ExternalRunID != "" {
		finalizeSuccess(ctx, p, res.Artifact)
		return
	}

	// 4. Async fast-path — wait for upstream to reach a terminal state.
	artifact, err := p.Poller.WaitFor(ctx, res.Artifact.ExternalRunID)
	if err != nil {
		failJob(ctx, p, "poll: "+err.Error())
		return
	}
	finalizeSuccess(ctx, p, artifact)
}

// finalizeSuccess writes artifacts_json + advances the state machine + consumes quota.
func finalizeSuccess(ctx context.Context, p RunJobParams, art mvpclient.Artifact) {
	// Progress is binary for the MVP — 100% on success.
	_ = UpdateJobProgress(ctx, p.DB, p.JobID, 1.0)

	// Persist the §23 artifact blob.
	blob, err := json.Marshal(art)
	if err != nil {
		// Non-fatal — log and continue; the run is still successful.
		log.Printf("[jobsvc] marshal artifact: %v", err)
	} else {
		if uerr := UpdateJobArtifacts(ctx, p.DB, p.JobID, string(blob)); uerr != nil {
			log.Printf("[jobsvc] update artifacts: %v", uerr)
		}
	}

	// Advance project status via the white-listed transition table.
	// storyboard is special: the project is already at STORYBOARD_READY
	// (no STORYBOARD_RENDERING state in §17.G); Advance(storyboard_done)
	// would return ErrIllegalTransition. Just leave the project alone
	// and stamp the job as succeeded.
	var next string
	if p.JobType == JobTypeStoryboard {
		next = p.CurrentStatus // already at STORYBOARD_READY
	} else {
		next, err = Advance(p.CurrentStatus, p.JobType+"_done")
		if err != nil {
			_ = SetJobError(ctx, p.DB, p.JobID, "advance: "+err.Error())
			return
		}
	}
	if err := UpdateJobStatus(ctx, p.DB, p.JobID, "succeeded"); err != nil {
		log.Printf("[jobsvc] update job %s: %v", p.JobID, err)
		return
	}
	if next != p.CurrentStatus {
		if err := UpdateProjectStatus(ctx, p.DB, p.ProjectID, next); err != nil {
			log.Printf("[jobsvc] update project %s: %v", p.ProjectID, err)
		}
	}

	// Convert the reserved credits into consumed. The handler reserved the
	// cost upfront for "render", so we move it across the ledger.
	if p.JobType == JobTypeRender && p.Cost > 0 {
		if cerr := quotasvc.Consume(ctx, p.DB, p.TenantID, p.Cost, p.CreatedBy); cerr != nil && !errors.Is(cerr, quotasvc.ErrInsufficient) {
			log.Printf("[jobsvc] consume quota: %v", cerr)
		}
	}
}

// failJob is the single failure path: stamp job error, move project to FAILED,
// and refund any reserved credits (render only — preview stages never reserved).
func failJob(ctx context.Context, p RunJobParams, msg string) {
	log.Printf("[jobsvc] job %s FAILED: %s", p.JobID, msg)
	if err := SetJobError(ctx, p.DB, p.JobID, msg); err != nil {
		log.Printf("[jobsvc] SetJobError: %v", err)
	}
	if err := UpdateProjectStatus(ctx, p.DB, p.ProjectID, StatusFailed); err != nil {
		log.Printf("[jobsvc] update project to FAILED: %v", err)
	}
	// Refund reserved credits so the tenant isn't charged for a failed render.
	if p.JobType == JobTypeRender && p.Cost > 0 {
		if rerr := quotasvc.Refund(ctx, p.DB, p.TenantID, p.Cost, p.CreatedBy); rerr != nil {
			log.Printf("[jobsvc] refund: %v", rerr)
		}
	}
	// Give the GET /status poll a beat to observe the failure before
	// the goroutine exits — matters for fast-fail tests.
	time.Sleep(50 * time.Millisecond)
}