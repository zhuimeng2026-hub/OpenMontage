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
	// Phase 5: Projects handler is provided by Phase 3/4's run.sh (handlers_project.go
	// on disk). Must ALWAYS be initialized — route registration below captures
	// the method value, and a nil receiver would panic at request time.
	projects := NewProjectHandler(db)
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
	// Phase 3 (project/job): mounted on `scoped` so /api/video-projects
	// handlers are reachable for the gate's setup chain (POST/GET project,
	// POST storyboard/cancel/etc.) AND for the gateway Dispatch delegates.
	scoped.POST("/video-projects", projects.Create)
	scoped.GET("/video-projects/:id", projects.Get)
	scoped.PUT("/video-projects/:id/brief", projects.UpdateBrief)
	scoped.POST("/video-projects/:id/reference", projects.SetReference)
	scoped.POST("/video-projects/:id/storyboard", projects.Storyboard)
	scoped.POST("/video-projects/:id/animatic", projects.Animatic)
	scoped.POST("/video-projects/:id/sample", projects.Sample)
	scoped.POST("/video-projects/:id/render", projects.Render)
	scoped.POST("/video-projects/:id/cancel", projects.Cancel)
	scoped.GET("/video-projects/:id/status", projects.Status)
	scoped.GET("/jobs/:job_id", projects.GetJob)

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
