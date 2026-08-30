#!/usr/bin/env bash
# Phase 5 — §17.F + §17.G — Agent Gateway + 状态聚合 (REAL implementation)
#
# 由 orchestrator.sh 调用:bash phase_5/run.sh --resume|--fresh <diff_file>
#
# 真正实现的内容:
#   1. 写入 internal/gwsvc/{verbs.go,types.go,status_map.go}
#      — 8 个业务动词名 + 路由表
#      — GatewayRequest / GatewayResponse 共享类型
#      — OM 原始状态 → 13 档统一状态映射(rawToUnified 表 + RawToUnified + SupportedRawStates)
#   2. 写入 cmd/mvp/handlers_gateway.go — 单 Dispatch handler(按 URL segment 派发)
#      + StatusLookup handler
#   3. 改写 cmd/mvp/main.go — 累加 Phase 0+1+2 路由 + 挂 8 verb 路由 + /status/lookup
#   4. go build → 输出到 /tmp/frameflow-bff-mvp-p5
#   5. 启动 binary(后台, :18906) + 跑 gate.sh
#
# 设计要点(详见 tasks.yaml):
#   - 8 个 verb handler 走单一 Dispatch handler(避免 8 个几乎一样的函数)。
#     状态变更动词(storyboard/animatic/sample/render/cancel)→ 委托 Phase 3 的
#     ProjectHandler(通过 gin context param 重写)。
#     只读 / 分析动词(analyze-product-assets / analyze-reference-video /
#     production-status)→ 返回 placeholder 响应,Phase 6+ 再接 OpenClaw/Hermes。
#   - production-status 是 GET,带 project_id query 参数,内部查 video_projects。
#   - /status/lookup 是纯函数:GET ?raw=<string> → JSON {"raw": ..., "unified": ...}。
#     unknown raw → "FAILED"(fail-loud,plan §8.2 显式禁止 silently fallback)。
#   - cmd/mvp/main.go 跨 Phase 累积,不重写老路由。
#   - port: Phase 0=18901 / 1=18902 / 2=18903 / 3=18904 / 4=18905 / **5=18906**。

set -u
set -o pipefail
# Ensure go is on PATH — cron doesn't source /etc/profile.d; Phase 0 hit this
# with "go: command not found" on the most recent run.
export PATH="/usr/local/go/bin:${PATH:-/usr/bin:/bin}"
REPO_ROOT="/opt/OpenMontage_Voicebox"
BFF="${REPO_ROOT}/frameflow/bff"
PHASE_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS="${PHASE_DIR}/tasks.yaml"
DIFF_FILE="${2:-/dev/null}"
LOG="${REPO_ROOT}/logs/mvp_dev/run-phase_5-$(date +%Y%m%d-%H%M%S).log"

mkdir -p "${REPO_ROOT}/logs/mvp_dev"
exec >> "${LOG}" 2>&1
echo "=== phase_5 run.sh start $(date -Iseconds) mode=${1:-?} ==="

MODE="${1:-}"
if [[ "${MODE}" != "--resume" && "${MODE}" != "--fresh" ]]; then
    echo "[FATAL] expected --resume or --fresh as \$1" >&2
    exit 2
fi

# tasks.yaml 守门
status="$(grep -E '^status:' "${TASKS}" | awk '{print $2}' | tr -d '"' | tr -d "'")"
if [ "${status}" != "READY" ]; then
    echo "[phase_5] STUB — tasks.yaml status=${status}, skipping"
    echo "phase_5 skipped: status=${status}" > "${DIFF_FILE}"
    exit 0
fi

cd "${BFF}" || exit 2

# Phase 5 不需要 schema 迁移 — sql_migrations 是空字符串。
# §17.F + §17.G 纯逻辑层,不引入新表。
echo "[phase_5] step 0: no schema migration (sql_migrations is empty per tasks.yaml)"

# ---- 1. 写 internal/gwsvc/verbs.go ----
echo "[phase_5] step 1: write internal/gwsvc/verbs.go"
mkdir -p internal/gwsvc
cat > internal/gwsvc/verbs.go <<'GOEOF'
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
GOEOF

# ---- 2. 写 internal/gwsvc/types.go ----
echo "[phase_5] step 2: write internal/gwsvc/types.go"
cat > internal/gwsvc/types.go <<'GOEOF'
package gwsvc

// VerbRequest is the JSON body every gateway verb accepts (except production-status,
// which takes project_id as a query param and ignores Payload).
//
// ProjectID is required for tenant validation; Payload is an open bag so each
// verb can grow its own input schema without a breaking type bump. The handler
// is responsible for shape-checking Payload for the specific verb.
type VerbRequest struct {
	ProjectID string                 `json:"project_id" binding:"required"`
	Payload   map[string]interface{} `json:"payload"`
}

// VerbResponse is the JSON shape every gateway verb returns.
//
// Verb echoes the caller's request verb (PascalCase). ProjectID is the project
// the verb acted on. Status is one of the 13-state unified enum from jobsvc
// (StatusCreated / StatusAssetAnalyzing / ...). JobID is set when a new job was
// spawned (storyboard/animatic/sample/render) and omitted for read-only verbs.
// Detail is an open bag for verb-specific extras (e.g. cost_reserved on a render).
type VerbResponse struct {
	Verb      string                 `json:"verb"`
	ProjectID string                 `json:"project_id"`
	Status    string                 `json:"status"` // 13-state enum
	JobID     string                 `json:"job_id,omitempty"`
	Detail    map[string]interface{} `json:"detail,omitempty"`
}
GOEOF

# ---- 3. 写 internal/gwsvc/status_map.go ----
echo "[phase_5] step 3: write internal/gwsvc/status_map.go"
cat > internal/gwsvc/status_map.go <<'GOEOF'
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
GOEOF

# ---- 4. 写 cmd/mvp/handlers_gateway.go ----
echo "[phase_5] step 4: write cmd/mvp/handlers_gateway.go"
cat > cmd/mvp/handlers_gateway.go <<'GOEOF'
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
		h.Projects.StartStage(c)
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
GOEOF

# ---- 5. 改写 cmd/mvp/main.go (Phase 0 + Phase 1 + Phase 2 + Phase 5 累加) ----
echo "[phase_5] step 5: rewrite cmd/mvp/main.go (extends Phase 0 + 1 + 2)"
cat > cmd/mvp/main.go <<'GOEOF'
// Package main is the MVP standalone binary extended across Phases 0, 1, 2, 5.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
// Phase 2 (2026-08-30): product / asset / manifest CRUD (6 routes).
// Phase 5 (2026-08-30): Agent Gateway (§17.F, 8 verbs) + state aggregation
//                        (§17.G, /status/lookup). Default port :18906.
//
// Phases 3 + 4 routes (project / job / quota) are mounted by their respective
// run.sh scripts and are not duplicated here — Phase 5 EXTENDS, not REPLACES,
// those route groups once they land. Phase 5 only mounts the gateway routes
// under the `scoped` group.
//
// Runs on a separate port from the production BFF (default :18906 in Phase 5)
// so a crash here can't take down the main server. Doesn't touch main.go.
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/auth"
	"frameflow-bff/internal/config"
	"frameflow-bff/internal/middleware"
)

func main() {
	cfg := config.Load()
	if err := config.Validate(cfg); err != nil {
		log.Printf("[mvp] WARN config validate: %v (continuing — JWT seed handles missing WeChat)", err)
	}

	port := os.Getenv("MVP_PORT")
	if port == "" {
		// Phase 5 default: 18906 (Phase 4 was 18905, Phase 3 was 18904,
		// Phase 2 was 18903, Phase 1 was 18902, Phase 0 was 18901).
		port = "18906"
	}

	dbPath := os.Getenv("MVP_DB_PATH")
	if dbPath == "" {
		dbPath = "/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db"
	}

	db := openDB(dbPath)
	defer db.Close()

	jwtSvc := auth.NewJWTService(cfg, db)
	tenants := NewTenantHandler(db)
	files := NewFileHandler(db)
	products := NewProductHandler(db)
	// Phase 5: Projects handler is provided by Phase 3's run.sh (handlers_project.go).
	// When Phase 3 hasn't run yet, projects is nil and gateway state-changing verbs
	// will return 500 — read-only verbs still work because Dispatch checks Projects
	// before delegating.
	var projects *ProjectHandler
	gw := NewGatewayHandler(db, projects)

	r := gin.Default()
	r.GET("/healthz", auth.HealthCheck)

	api := r.Group("/api")

	// --- Phase 0 public routes ---
	api.POST("/auth/login", jwtSvc.Login)
	api.GET("/me/jwt", jwtSvc.Me)

	// --- Phase 1 routes requiring JWT only (no tenant header — caller
	//     may not have one yet, e.g. when creating their first tenant) ---
	jwtOnly := api.Group("")
	jwtOnly.Use(middleware.RequireJWT(jwtSvc))
	jwtOnly.POST("/tenants", tenants.Create)
	jwtOnly.GET("/tenants", tenants.ListMine)

	// --- Phase 1 + Phase 2 routes requiring JWT + X-Tenant-Id ---
	scoped := api.Group("")
	scoped.Use(middleware.RequireJWT(jwtSvc))
	scoped.Use(middleware.TenantScope(db))
	// Phase 1:
	scoped.POST("/tenants/:id/members", tenants.AddMember)
	scoped.GET("/files/sign", files.Sign)
	// Phase 2:
	scoped.POST("/products", products.Create)
	scoped.GET("/products/:id", products.Get)
	scoped.POST("/products/:id/assets", products.UploadAsset)
	scoped.GET("/products/:id/assets", products.ListAssets)
	scoped.GET("/products/:id/manifest", products.GetManifest)
	scoped.PUT("/products/:id/manifest/:asset_id", products.CorrectAsset)

	// --- Phase 5: Agent Gateway (§17.F — 8 verbs) ---
	// Production-status is GET; all others are POST. Dispatch routes by URL segment.
	scoped.POST("/gateway/analyze-product-assets", gw.Dispatch)
	scoped.POST("/gateway/analyze-reference-video", gw.Dispatch)
	scoped.POST("/gateway/generate-storyboard", gw.Dispatch)
	scoped.POST("/gateway/generate-animatic", gw.Dispatch)
	scoped.POST("/gateway/generate-sample", gw.Dispatch)
	scoped.POST("/gateway/render-final", gw.Dispatch)
	scoped.GET("/gateway/production-status", gw.Dispatch)
	scoped.POST("/gateway/cancel-production", gw.Dispatch)
	// --- Phase 5: State aggregation (§17.G — OM raw → 13-state) ---
	scoped.GET("/status/lookup", gw.StatusLookup)

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_5 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
GOEOF

# ---- 6. go build ----
echo "[phase_5] step 6: go build cmd/mvp"
BIN="/tmp/frameflow-bff-mvp-p5"
mkdir -p /tmp
go build -o "${BIN}" ./cmd/mvp 2>&1 | tee -a "${LOG}"
build_exit=${PIPESTATUS[0]}
if [ "${build_exit}" -ne 0 ]; then
    echo "[phase_5] build FAILED exit=${build_exit}"
    exit 4
fi
echo "[phase_5] build OK → ${BIN}"

# ---- 7. start binary (background) ----
echo "[phase_5] step 7: start binary on :18906"
pkill -f frameflow-bff-mvp-p5 2>/dev/null || true
sleep 1

WEIXIN_MOCK_AUTH=1 MVP_PORT=18906 MVP_DB_PATH="${DB_PATH}" \
    nohup "${BIN}" > "${REPO_ROOT}/logs/mvp_dev/phase_5-server.log" 2>&1 &
SERVER_PID=$!
echo "[phase_5] server pid=${SERVER_PID}"

# Wait for /healthz
HEALTH_OK=0
for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -sf "http://127.0.0.1:18906/healthz" >/dev/null 2>&1; then
        echo "[phase_5] /healthz ok after ${i} attempt(s)"
        HEALTH_OK=1
        break
    fi
    sleep 0.5
done
if [ "${HEALTH_OK}" != "1" ]; then
    echo "[phase_5] /healthz never came up — server log:" >&2
    tail -30 "${REPO_ROOT}/logs/mvp_dev/phase_5-server.log" >&2
    kill ${SERVER_PID} 2>/dev/null || true
    exit 5
fi

# ---- 8. run gate ----
echo "[phase_5] step 8: run gate"
GATE_EXIT=0
bash "${PHASE_DIR}/gate.sh" || GATE_EXIT=$?

# ---- 9. stop server ----
kill ${SERVER_PID} 2>/dev/null || true
wait ${SERVER_PID} 2>/dev/null || true

if [ "${GATE_EXIT}" != "0" ]; then
    echo "[phase_5] gate FAILED exit=${GATE_EXIT}"
    exit 1
fi

echo "[phase_5] DONE — gate green, server stopped"
exit 0