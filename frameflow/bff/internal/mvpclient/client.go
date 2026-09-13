package mvpclient

import (
	"context"
	"encoding/json"
	"fmt"

	"frameflow-bff/internal/mcp"
)

// Client is a thin wrapper around mcp.Client that knows how to dispatch
// the four §17.F verbs to upstream video_compose.
//
// One Client owns one upstream MCP session. New() runs the MCP handshake
// eagerly so the first Prepare* call doesn't pay the latency; if the
// upstream is down at startup, Prepare* fails fast with ErrUnavailable
// instead of every request timing out.
type Client struct {
	mcp *mcp.Client
}

// New constructs a Client and runs the MCP `initialize` handshake.
// Returns ErrUnavailable if the upstream is unreachable or returns a
// non-2xx / malformed response.
//
// baseURL is the upstream MCP HTTP endpoint (e.g. http://lanes.ymxt.top:8900/mcp).
// token is the Bearer token; pass "" to skip auth (only safe on localhost).
func New(ctx context.Context, baseURL, token string) (*Client, error) {
	c := mcp.NewClient(baseURL, token)
	if err := c.Initialize(); err != nil {
		return nil, fmt.Errorf("%w: initialize: %v", ErrUnavailable, err)
	}
	return &Client{mcp: c}, nil
}

// MustNew is like New but panics on error — for use in main() where we want
// startup to fail loudly rather than silently degrade.
func MustNew(ctx context.Context, baseURL, token string) *Client {
	c, err := New(ctx, baseURL, token)
	if err != nil {
		panic(err)
	}
	return c
}

// PrepareStoryboard calls video_compose operation=render with the
// storyboard profile. See types.go for the default profile.
//
// Stage state target: STORYBOARD_READY (Advance() from CREATED/PLANNING).
func (c *Client) PrepareStoryboard(ctx context.Context, req PrepareRequest) (PrepareResult, error) {
	return c.dispatch(ctx, "storyboard", req)
}

// PrepareAnimatic calls video_compose with the animatic profile.
//
// Stage state target: ANIMATIC_RENDERING → ANIMATIC_READY (via runner).
func (c *Client) PrepareAnimatic(ctx context.Context, req PrepareRequest) (PrepareResult, error) {
	return c.dispatch(ctx, "animatic", req)
}

// PrepareSample calls video_compose with the sample profile (per-scene
// subset, watermarked). See types.go for default scene selection.
//
// Stage state target: SAMPLE_RENDERING → SAMPLE_READY (via runner).
func (c *Client) PrepareSample(ctx context.Context, req PrepareRequest) (PrepareResult, error) {
	return c.dispatch(ctx, "sample", req)
}

// RenderFinal calls video_compose with the full render profile. This is
// the costliest call (50 credits reserved per §17.E).
//
// Stage state target: FINAL_RENDERING → COMPLETED (via runner).
func (c *Client) RenderFinal(ctx context.Context, req PrepareRequest) (PrepareResult, error) {
	return c.dispatch(ctx, "render", req)
}

// dispatch builds the video_compose arguments envelope and calls upstream.
// Returns ErrUnavailable on transport failure; ErrInvalidResponse if the
// upstream response can't be parsed into our Artifact shape.
func (c *Client) dispatch(ctx context.Context, jobType string, req PrepareRequest) (PrepareResult, error) {
	profile := req.Profile
	if profile == nil {
		profile = PerStageDefaultProfile(jobType)
	}
	if profile == nil {
		return PrepareResult{}, fmt.Errorf("%w: unknown job_type %q", ErrInvalidResponse, jobType)
	}

	args := map[string]interface{}{
		"operation":          "render",
		"project_id":         req.ProjectID,
		"tenant_id":          req.TenantID,
		"creative_brief":     req.CreativeBriefJSON,
		"reference_mode":     req.ReferenceMode,
		"reference_file_key": req.ReferenceFileKey,
		"profile":            profile,
		// Stage tag so upstream can pick the right template / asset subset.
		"stage": jobType,
	}

	resp, err := c.mcp.CallTool("video_compose", args)
	if err != nil {
		return PrepareResult{}, fmt.Errorf("%w: %v", ErrUnavailable, err)
	}
	return parseArtifact(resp)
}

// parseArtifact extracts our Artifact from an MCP tools/call response.
//
// video_compose has been observed to return three response shapes:
//
//   1. Synchronous artifact — resp["done"] == true and resp["artifact"] is
//      fully populated. We return Done=true.
//   2. Async handle — resp["done"] == false and resp["external_run_id"] is
//      set. We return Done=false with just ExternalRunID; the runner
//      resumes via Poller.WaitFor.
//   3. Malformed / unexpected keys — ErrInvalidResponse.
func parseArtifact(resp map[string]interface{}) (PrepareResult, error) {
	if resp == nil {
		return PrepareResult{}, fmt.Errorf("%w: nil response", ErrInvalidResponse)
	}
	if errObj, ok := resp["error"]; ok {
		return PrepareResult{}, fmt.Errorf("%w: upstream error: %v", ErrInvalidResponse, errObj)
	}

	// Marshal + re-unmarshal via a permissive shape so callers can
	// populate either the synchronous or async paths cleanly.
	raw, err := json.Marshal(resp)
	if err != nil {
		return PrepareResult{}, fmt.Errorf("%w: marshal: %v", ErrInvalidResponse, err)
	}
	var probe struct {
		Done          bool   `json:"done"`
		ExternalRunID string `json:"external_run_id"`
		OMProjectID   string `json:"om_project_id"`
		Artifact      struct {
			PreviewURL       string         `json:"preview_url"`
			Scenes           []SceneArtifact `json:"scenes"`
			Files            []string       `json:"files"`
			DurationSeconds  float64        `json:"duration_seconds"`
			Resolution       string         `json:"resolution"`
		} `json:"artifact"`
		// Top-level fallback fields — some versions return them flat.
		PreviewURL       string          `json:"preview_url"`
		Scenes           []SceneArtifact `json:"scenes"`
		Files            []string        `json:"files"`
		DurationSeconds  float64         `json:"duration_seconds"`
		Resolution       string          `json:"resolution"`
	}
	if err := json.Unmarshal(raw, &probe); err != nil {
		return PrepareResult{}, fmt.Errorf("%w: unmarshal: %v", ErrInvalidResponse, err)
	}

	if probe.ExternalRunID == "" {
		return PrepareResult{}, fmt.Errorf("%w: missing external_run_id", ErrInvalidResponse)
	}

	a := Artifact{
		ExternalRunID:   probe.ExternalRunID,
		OMProjectID:     probe.OMProjectID,
		PreviewURL:      pickStr(probe.Artifact.PreviewURL, probe.PreviewURL),
		Scenes:          pickScenes(probe.Artifact.Scenes, probe.Scenes),
		Files:           pickStrSlice(probe.Artifact.Files, probe.Files),
		DurationSeconds: pickFloat(probe.Artifact.DurationSeconds, probe.DurationSeconds),
		Resolution:      pickStr(probe.Artifact.Resolution, probe.Resolution),
	}
	return PrepareResult{Done: probe.Done, Artifact: a}, nil
}

func pickStr(a, b string) string {
	if a != "" {
		return a
	}
	return b
}
func pickFloat(a, b float64) float64 {
	if a != 0 {
		return a
	}
	return b
}
func pickScenes(a, b []SceneArtifact) []SceneArtifact {
	if len(a) > 0 {
		return a
	}
	return b
}
func pickStrSlice(a, b []string) []string {
	if len(a) > 0 {
		return a
	}
	return b
}