// Package main — Phase 5 gateway handlers (§17.F + §17.G).
//
// Two handlers, both mounted under the `scoped` group (RequireJWT + TenantScope):
//
//   POST/GET  /api/gateway/<segment>   → Dispatch  — single handler that routes
//                                          by URL segment to the right verb.
//   GET       /api/status/lookup       → StatusLookup — pure OM→unified state map.
//
// Dispatch is intentionally a single function (not 8 near-duplicates). It:
//   1. Resolves the URL segment to a verb name via gwsvc.PathSegment.
//   2. Extracts project_id (POST body / GET query).
//   3. Validates tenant ownership via jobsvc.GetProject (404 / 403 / 500).
//   4. For state-changing verbs (storyboard / animatic / sample / render /
//      cancel): rewrites gin params so Phase 3's ProjectHandler.StartStage
//      matches and delegates.
//   5. For read-only or analyze verbs: returns a placeholder VerbResponse with
//      the project's current status (Phase 6+ will wire real OpenClaw calls).
//
// production-status is GET (project_id query); all others are POST (VerbRequest
// body). Both code paths funnel through Dispatch.
package main

import (
	"database/sql"
	"errors"
	"net/http"
	"strings"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/gwsvc"
	"frameflow-bff/internal/jobsvc"
)

// GatewayHandler exposes the §17.F verb dispatch and §17.G state aggregation.
//
// Projects is the Phase 3 ProjectHandler — gateway verbs that change state
// (storyboard / animatic / sample / render / cancel) are delegated to it via
// StartStage / Render / Cancel. Nil Projects is OK for read-only verbs
// (analyze-* / production-status) since those never touch Projects.
type GatewayHandler struct {
	DB       *sql.DB
	Projects *ProjectHandler // delegate to Phase 3 handler
}

// NewGatewayHandler wires the gateway handler with the Phase 3 project handler.
// projects may be nil in tests, but in main() it's always set.
func NewGatewayHandler(db *sql.DB, projects *ProjectHandler) *GatewayHandler {
	return &GatewayHandler{DB: db, Projects: projects}
}

// Dispatch routes one /api/gateway/<segment> request to the right verb.
//
//   - 404 on unknown segment (caught before any tenant lookup).
//   - 400 if project_id is missing.
//   - 404 if project doesn't exist.
//   - 403 if project belongs to another tenant.
//   - 500 on unexpected DB error.
//   - 200 with VerbResponse for state-changing verbs (delegated to Projects)
//     or placeholder for analyze / production-status.
func (h *GatewayHandler) Dispatch(c *gin.Context) {
	fullPath := c.FullPath() // e.g. "/api/gateway/generate-storyboard"
	seg := strings.TrimPrefix(fullPath, "/api/gateway/")

	var verb string
	for k, v := range gwsvc.PathSegment {
		if v == seg {
			verb = k
			break
		}
	}
	if verb == "" {
		c.JSON(http.StatusNotFound, gin.H{"error": "unknown gateway verb: " + seg})
		return
	}

	var req gwsvc.VerbRequest
	// production-status is GET; others are POST. Handle both without forcing
	// the frontend to send a body for the GET case.
	if c.Request.Method == http.MethodGet {
		req.ProjectID = c.Query("project_id")
	} else {
		_ = c.ShouldBindJSON(&req)
	}
	if req.ProjectID == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "project_id required"})
		return
	}

	// Validate project tenant. RequireJWT + TenantScope middleware have already
	// validated the JWT and set tenant_id; we still need to check the project
	// belongs to THIS tenant (not another one the caller could probe).
	tidV, _ := c.Get("tenant_id")
	tid, _ := tidV.(string)
	p, err := jobsvc.GetProject(c.Request.Context(), h.DB, req.ProjectID)
	if errors.Is(err, jobsvc.ErrProjectNotFound) {
		c.JSON(http.StatusNotFound, gin.H{"error": "project not found"})
		return
	}
	if err != nil {
		c.JSON(http.StatusInternalServerError, gin.H{"error": err.Error()})
		return
	}
	if p.TenantID != tid {
		c.JSON(http.StatusForbidden, gin.H{"error": "project belongs to another tenant"})
		return
	}

	// Map verb → inner Phase 3 stage name + HTTP verb.
	// Phase 3's StartStage uses :id (project) and :stage (storyboard / animatic
	// / sample / render / cancel). We rewrite c.Params so its existing handler
	// matches without duplicating logic.
	stage := ""
	switch verb {
	case gwsvc.VerbGenerateStoryboard:
		stage = "storyboard"
	case gwsvc.VerbGenerateAnimatic:
		stage = "animatic"
	case gwsvc.VerbGenerateSample:
		stage = "sample"
	case gwsvc.VerbRenderFinal:
		stage = "render"
	case gwsvc.VerbCancelProduction:
		stage = "cancel"
	}

	if stage != "" && h.Projects != nil {
		// Delegate to Phase 3's StartStage. The handler reads c.Param("id") and
		// c.Param("stage"); we inject both before the call.
		c.Params = append(c.Params,
			gin.Param{Key: "id", Value: req.ProjectID},
			gin.Param{Key: "stage", Value: stage},
		)
		h.Projects.StartStage(c, stage)
		return
	}

	// Read-only / analyze verbs — placeholder response.
	// Phase 6+ will replace this with real OpenClaw / Hermes calls per plan §21.
	resp := gwsvc.VerbResponse{
		Verb:      verb,
		ProjectID: req.ProjectID,
		Status:    p.Status,
		Detail: map[string]interface{}{
			"note": "MVP stub — full impl in Phase 6+ (Agent Gateway wraps OpenClaw/Hermes)",
		},
	}
	c.JSON(http.StatusOK, resp)
}

// StatusLookup handles GET /api/status/lookup?raw=<om_state> — pure function
// that maps a raw OM status string onto the 13-state unified enum (plan §17.G).
//
// Returns:
//   - raw:              the input string (echoed for the caller's log).
//   - unified:          one of the 13 jobsvc statuses (never "unknown").
//   - supported_raw_states: full list of raw strings the mapper knows.
//
// Unknown raw strings return StatusFailed (NOT "unknown" / NOT 400) — plan §8.2
// requires fail-loud mapping. The endpoint is intentionally side-effect-free.
func (h *GatewayHandler) StatusLookup(c *gin.Context) {
	raw := c.Query("raw")
	unified := gwsvc.RawToUnified(raw)
	c.JSON(http.StatusOK, gin.H{
		"raw":                  raw,
		"unified":              unified,
		"supported_raw_states": gwsvc.SupportedRawStates(),
	})
}
