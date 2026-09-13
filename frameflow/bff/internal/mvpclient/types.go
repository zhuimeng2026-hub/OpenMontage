// Package mvpclient wraps the upstream OpenMontage MCP server for the MVP BFF.
//
// Phase 6 of docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md
// replaces the 400ms simulated runner with real MCP calls. The four business
// verbs (storyboard / animatic / sample / render) are dispatched to
// upstream `video_compose` with stage-specific profiles — the wrapper
// layers the §17.D state machine on top.
//
// Why a thin wrapper rather than calling mcp.Client directly?
//
//   - The handler should not know about MCP protocol envelopes — it deals
//     in jobs / projects / states. Calling CallTool("video_compose", ...) raw
//     would leak protocol concerns into the handler layer.
//   - Each job type has its own profile (resolution / bitrate / scene
//     selection). Keeping that mapping in one place avoids drifting
//     duplicates in handlers_project.go and the runner.
//   - The poller (Poller.WaitFor) is also shared — both the runner and any
//     future status-poll endpoint need the same "2s interval, 600s timeout"
//     semantics.
package mvpclient

import "errors"

// ErrUnavailable is returned when the MCP client cannot reach the upstream
// server (network / DNS / 5xx). Handlers should map this to a 503 response
// per plan §8.2 fail-loud — never to a silent *_RENDERING job that hangs.
var ErrUnavailable = errors.New("mvpclient: MCP upstream unavailable")

// ErrInvalidResponse is returned when the upstream returns a payload we
// can't parse — same fail-loud treatment as ErrUnavailable.
var ErrInvalidResponse = errors.New("mvpclient: invalid upstream response")

// Profile controls video_compose's render-time knobs. Per-job-type defaults
// are set by PrepareStoryboard / PrepareAnimatic / PrepareSample / RenderFinal;
// callers can override fields on a per-call basis via PrepareRequest.Profile.
type Profile struct {
	// Resolution in WxH (e.g. "540x960"). Empty means "let upstream pick".
	Resolution string `json:"resolution,omitempty"`
	// Bitrate in kbps. 0 means "let upstream pick".
	BitrateKbps int `json:"bitrate_kbps,omitempty"`
	// MaxDurationSeconds caps the generated clip. 0 = uncapped.
	MaxDurationSeconds int `json:"max_duration_seconds,omitempty"`
	// SceneIDs restricts the operation to a subset of scenes (sample).
	// Empty = all scenes.
	SceneIDs []int `json:"scene_ids,omitempty"`
	// LowQuality forces upstream to skip costly passes (storyboard / animatic).
	LowQuality bool `json:"low_quality,omitempty"`
	// Watermark adds a "preview" overlay (storyboard / animatic / sample).
	Watermark bool `json:"watermark,omitempty"`
}

// PrepareRequest is the input to any Prepare* method.
type PrepareRequest struct {
	// ProjectID is the video_projects.id (used for tenant scoping and
	// downstream OM project binding).
	ProjectID string `json:"project_id"`
	// TenantID scopes the upstream request and is forwarded as a header.
	TenantID string `json:"tenant_id"`
	// CreativeBriefJSON is the verbatim creative_brief_json from the project.
	CreativeBriefJSON string `json:"creative_brief_json"`
	// ReferenceFileKey is the bound reference video (empty = no reference).
	ReferenceFileKey string `json:"reference_file_key,omitempty"`
	// ReferenceMode — "balanced" / "description_first" / "reference_first".
	ReferenceMode string `json:"reference_mode,omitempty"`
	// Profile overrides the per-stage default profile (nil = use default).
	Profile *Profile `json:"profile,omitempty"`
}

// Artifact is the shape we extract from upstream video_compose responses.
//
// Scope §23 defines three preview shapes (storyboard scenes array, animatic
// single clip, sample scene subset). We collapse them into a single
// per-stage JSON that gets stored in production_jobs.artifacts_json.
type Artifact struct {
	// ExternalRunID is the upstream MCP / OM run id (used to resume polling
	// and for support traces).
	ExternalRunID string `json:"external_run_id"`
	// OMProjectID is the upstream OM-side project id (empty until bound).
	OMProjectID string `json:"om_project_id,omitempty"`
	// PreviewURL is the canonical preview URL (animatic, render) or
	// "composite" preview (storyboard — see Scenes for per-scene frames).
	PreviewURL string `json:"preview_url,omitempty"`
	// Scenes is the storyboard scene array (scope §23 storyboard shape).
	Scenes []SceneArtifact `json:"scenes,omitempty"`
	// Files is the per-scene file list (sample, scope §23).
	Files []string `json:"files,omitempty"`
	// Duration / Resolution echoed for client convenience.
	DurationSeconds float64 `json:"duration_seconds,omitempty"`
	Resolution      string  `json:"resolution,omitempty"`
}

// SceneArtifact mirrors scope §23 storyboard scene shape.
type SceneArtifact struct {
	SceneID    int     `json:"scene_id"`
	PreviewURL string  `json:"preview_url"`
	Duration   float64 `json:"duration"`
}

// PrepareResult is what Prepare* returns on success. Both the artifact and
// any polled async-state handle (if upstream returns one synchronously).
type PrepareResult struct {
	// Done=true means upstream returned a terminal artifact. Done=false means
	// upstream returned an async job handle — the runner must poll Poller.
	Done bool `json:"done"`
	// Artifact is populated when Done=true; may also be partially populated
	// when Done=false (ExternalRunID at minimum).
	Artifact Artifact `json:"artifact"`
}

// PerStageDefaultProfile returns the default Profile for a given job_type
// string (storyboard / animatic / sample / render). Empty string returns nil.
//
// Tuned for the MVP gate:
//
//   - storyboard: 540x960, low-quality, watermark — fast (~5–10s).
//   - animatic:   540x960, low-quality, watermark        (~30–60s).
//   - sample:     1080x1920, watermark, 3 scenes        (~60–120s).
//   - render:     1080x1920, no watermark, full quality  (~5–10min).
func PerStageDefaultProfile(jobType string) *Profile {
	switch jobType {
	case "storyboard":
		return &Profile{Resolution: "540x960", LowQuality: true, Watermark: true, MaxDurationSeconds: 30}
	case "animatic":
		return &Profile{Resolution: "540x960", LowQuality: true, Watermark: true, MaxDurationSeconds: 60}
	case "sample":
		return &Profile{Resolution: "1080x1920", Watermark: true, SceneIDs: []int{1, 2, 3}, MaxDurationSeconds: 90}
	case "render":
		return &Profile{Resolution: "1080x1920", MaxDurationSeconds: 600}
	}
	return nil
}