// Package gwsvc — §17.G state aggregation: OM raw status → 13-state unified enum.
//
// OpenClaw / Hermes / MCP callbacks can hand back anywhere from 20 to 100+
package gwsvc

import "frameflow-bff/internal/jobsvc"

// rawToUnified maps every raw status string the OM stack has been observed to
// emit (in production or in test fixtures) onto the 13-state unified enum
// declared in jobsvc (StatusCreated … StatusCancelled).
//
// Categories:
//   - 13-state passthrough: raw IS a unified status — return as-is (cheap guard).
//   - OM raw strings: pending / queued / analyzing_assets / ... mapped to the
//     appropriate pipeline stage.
//   - ambiguous / mid-pipeline: running / in_progress / mcp-progress →
//     StatusFinalRendering (latest irreversible active state — fail-loud if
//     we can't tell exactly where we are).
//   - terminal errors: failed / error / error_unknown / aborted / timeout →
//     StatusFailed.
//   - cancellation variants: cancelled / canceled → StatusCancelled.
//
// Unknown / unmapped raw strings fall through to StatusFailed (see RawToUnified),
// not to a silent "unknown" sentinel — per plan §8.2 we must NOT silently
// downgrade unrecognised states.
var rawToUnified = map[string]string{
	// 13-state passthrough — guard against redundant mapping in callers that
	// might already have the unified form.
	"CREATED":             jobsvc.StatusCreated,
	"ASSET_ANALYZING":     jobsvc.StatusAssetAnalyzing,
	"REFERENCE_ANALYZING": jobsvc.StatusReferenceAnalyzing,
	"PLANNING":            jobsvc.StatusPlanning,
	"STORYBOARD_READY":    jobsvc.StatusStoryboardReady,
	"ANIMATIC_RENDERING":  jobsvc.StatusAnimaticRendering,
	"ANIMATIC_READY":      jobsvc.StatusAnimaticReady,
	"SAMPLE_RENDERING":    jobsvc.StatusSampleRendering,
	"SAMPLE_READY":        jobsvc.StatusSampleReady,
	"WAITING_APPROVAL":    jobsvc.StatusWaitingApproval,
	"FINAL_RENDERING":     jobsvc.StatusFinalRendering,
	"COMPLETED":           jobsvc.StatusCompleted,
	"FAILED":              jobsvc.StatusFailed,
	"CANCELLED":           jobsvc.StatusCancelled,

	// OM raw strings → unified
	"pending":             jobsvc.StatusCreated,
	"queued":              jobsvc.StatusPlanning,
	"analyzing_assets":    jobsvc.StatusAssetAnalyzing,
	"analyzing_reference": jobsvc.StatusReferenceAnalyzing,
	"storyboard_pending":  jobsvc.StatusPlanning,
	"storyboard_ready":    jobsvc.StatusStoryboardReady,
	"animatic_rendering":  jobsvc.StatusAnimaticRendering,
	"animatic_ready":      jobsvc.StatusAnimaticReady,
	"sample_rendering":    jobsvc.StatusSampleRendering,
	"sample_ready":        jobsvc.StatusSampleReady,
	"final_rendering":     jobsvc.StatusFinalRendering,
	"render_done":         jobsvc.StatusCompleted,
	"success":             jobsvc.StatusCompleted,
	"succeeded":           jobsvc.StatusCompleted,
	"done":                jobsvc.StatusCompleted,
	"running":             jobsvc.StatusFinalRendering, // ambiguous — pick latest active
	"in_progress":         jobsvc.StatusFinalRendering,
	"failed":              jobsvc.StatusFailed,
	"error":               jobsvc.StatusFailed,
	"error_unknown":       jobsvc.StatusFailed,
	"mcp-raw":             jobsvc.StatusAssetAnalyzing, // treat as still analyzing
	"mcp-progress":        jobsvc.StatusFinalRendering, // mid-pipeline, assume render
	"cancelled":           jobsvc.StatusCancelled,
	"canceled":            jobsvc.StatusCancelled,
	"aborted":             jobsvc.StatusFailed,
	"timeout":             jobsvc.StatusFailed,
}

// RawToUnified returns the unified 13-state status for a raw OM status string.
//
// Behaviour:
//   - Empty input → StatusFailed (fail-loud — we never claim "unknown" silently).
//   - Already a 13-state enum → returned as-is (cheap O(1) lookup).
//   - Known OM raw string → mapped to the unified enum.
//   - Unknown raw string → StatusFailed (plan §8.2 explicitly forbids silent fallback).
//
// The mapping is intentionally total — every input maps to a valid jobsvc
// status. Callers do NOT need to handle an "unknown" sentinel.
func RawToUnified(raw string) string {
	if raw == "" {
		return jobsvc.StatusFailed
	}
	if u, ok := rawToUnified[raw]; ok {
		return u
	}
	return jobsvc.StatusFailed // fail-loud — plan §8.2 explicitly forbids silent fallback
}

// SupportedRawStates returns the list of every raw status the mapper knows.
// Used by /api/status/lookup so callers can probe coverage and audit drift
// between OM and the BFF.
func SupportedRawStates() []string {
	out := make([]string, 0, len(rawToUnified))
	for k := range rawToUnified {
		out = append(out, k)
	}
	return out
}
