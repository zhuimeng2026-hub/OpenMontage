package mvpclient

import (
	"context"
	"errors"
	"fmt"
	"time"

	"frameflow-bff/internal/mcp"
)

// Default poll interval and timeout. Tuned for MVP — storyboard is fast
// (~5s) so 2s interval catches it within ~3 polls; render takes minutes
// so 600s timeout is the long ceiling.
const (
	DefaultPollInterval = 2 * time.Second
	DefaultPollTimeout  = 600 * time.Second
)

// Poller resumes an async upstream run via video_compose until it reaches
// a terminal state, then returns the artifact. Used by the runner after
// Client.Prepare* returns Done=false (async handle).
//
// The runner always runs with context.Background so the HTTP handler can
// return *_RENDERING to the client immediately. The runner hands its own
// ctx (with deadline) to Poller.
type Poller struct {
	mcp      *mcp.Client
	Interval time.Duration
	Timeout  time.Duration
}

// NewPoller wires a Poller to the same MCP client used by the parent
// Client. interval/timeout are clamped to sane defaults if zero.
func NewPoller(c *Client, interval, timeout time.Duration) *Poller {
	if interval <= 0 {
		interval = DefaultPollInterval
	}
	if timeout <= 0 {
		timeout = DefaultPollTimeout
	}
	return &Poller{mcp: c.mcp, Interval: interval, Timeout: timeout}
}

// ErrTimeout is returned by WaitFor when the upstream run hasn't reached
// a terminal state within Timeout. The caller should mark the production_job
// as failed and the video_projects as FAILED — do NOT silently keep waiting.
var ErrTimeout = errors.New("mvpclient: poll timeout")

// WaitFor polls upstream until the run reported by externalRunID reaches
// a terminal state (succeeded / failed / cancelled). On terminal-success,
// returns the full Artifact. On terminal-failure, returns ErrInvalidResponse
// with the upstream error payload. On poll timeout, returns ErrTimeout.
//
// Polling uses video_compose's status probe — not a separate tool — so we
// don't introduce a new schema dependency.
func (p *Poller) WaitFor(ctx context.Context, externalRunID string) (Artifact, error) {
	deadline := time.Now().Add(p.Timeout)
	lastProgress := -1.0

	for {
		if ctx.Err() != nil {
			return Artifact{}, ctx.Err()
		}
		if time.Now().After(deadline) {
			return Artifact{}, ErrTimeout
		}

		resp, err := p.mcp.CallTool("video_compose", map[string]interface{}{
			"operation":       "status",
			"external_run_id": externalRunID,
		})
		if err != nil {
			return Artifact{}, fmt.Errorf("%w: status poll: %v", ErrUnavailable, err)
		}

		// Probe shape (same conventions as Client.parseArtifact):
		//   {"status": "running"|"succeeded"|"failed", "progress": 0..1,
		//    "artifact": {...}, "error": "..."}
		var probe struct {
			Status   string  `json:"status"`
			Progress float64 `json:"progress"`
			Error    string  `json:"error"`
			Artifact Artifact `json:"artifact"`
		}
		// Allow response to be the artifact at top level too (some versions).
		if resp != nil {
			// Be permissive — copy what fits.
			if s, ok := resp["status"].(string); ok {
				probe.Status = s
			}
			if pf, ok := resp["progress"].(float64); ok {
				probe.Progress = pf
			}
			if e, ok := resp["error"].(string); ok {
				probe.Error = e
			}
			if a, ok := resp["artifact"].(map[string]interface{}); ok {
				if rrid, ok := a["external_run_id"].(string); ok {
					probe.Artifact.ExternalRunID = rrid
				}
				if opid, ok := a["om_project_id"].(string); ok {
					probe.Artifact.OMProjectID = opid
				}
				if purl, ok := a["preview_url"].(string); ok {
					probe.Artifact.PreviewURL = purl
				}
			}
		}

		// Terminal-state branches.
		switch probe.Status {
		case "succeeded", "completed", "done", "ready":
			if probe.Artifact.ExternalRunID == "" {
				probe.Artifact.ExternalRunID = externalRunID
			}
			return probe.Artifact, nil
		case "failed", "error":
			return probe.Artifact, fmt.Errorf("%w: upstream failed: %s", ErrInvalidResponse, probe.Error)
		case "cancelled", "canceled":
			return probe.Artifact, fmt.Errorf("%w: upstream cancelled", ErrInvalidResponse)
		}

		// Progress callback hook — left as a future extension point.
		// For now we just no-op so we don't bloat the surface.
		_ = lastProgress

		select {
		case <-ctx.Done():
			return Artifact{}, ctx.Err()
		case <-time.After(p.Interval):
		}
	}
}