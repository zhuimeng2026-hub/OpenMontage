// Package gwsvc is the §17.F Agent Gateway + §17.G state aggregation layer.
//
// verbs.go declares the eight business verbs the gateway exposes (per plan §17.F)
// and the URL segment each one maps to under /api/gateway/. The actual handler
// dispatch lives in cmd/mvp/handlers_gateway.go (Dispatch method); this file is
// pure data so other layers can reference the verb constants and PathSegment table
// without dragging in gin's context type.
//
// Stage-changing verbs (GenerateStoryboard / Animatic / Sample / Render / Cancel)
// are delegated to Phase 3's ProjectHandler (StartStage / Render / Cancel) —
// MVP gateway is a thin business-semantic wrapper. Read-only verbs (Analyze* /
// production-status) return placeholder responses; Phase 6+ wires them to real
// OpenClaw / Hermes skills per plan §21.
package gwsvc

// Verb names per §17.F — the eight business verbs every frontend integrates with.
const (
	VerbAnalyzeProductAssets  = "AnalyzeProductAssets"
	VerbAnalyzeReferenceVideo = "AnalyzeReferenceVideo"
	VerbGenerateStoryboard    = "GenerateStoryboard"
	VerbGenerateAnimatic      = "GenerateAnimatic"
	VerbGenerateSample        = "GenerateSample"
	VerbRenderFinal           = "RenderFinal"
	VerbGetProductionStatus   = "GetProductionStatus"
	VerbCancelProduction      = "CancelProduction"
)

// PathSegment maps verb name → URL segment under /api/gateway/.
//
// Naming convention: kebab-case for the URL (REST-y), PascalCase for the verb
// constant (Go-y). Keep the map entries unique — Dispatch relies on this for
// segment → verb lookup.
var PathSegment = map[string]string{
	VerbAnalyzeProductAssets:  "analyze-product-assets",
	VerbAnalyzeReferenceVideo: "analyze-reference-video",
	VerbGenerateStoryboard:    "generate-storyboard",
	VerbGenerateAnimatic:      "generate-animatic",
	VerbGenerateSample:        "generate-sample",
	VerbRenderFinal:           "render-final",
	VerbCancelProduction:      "cancel-production",
	VerbGetProductionStatus:   "production-status",
}
