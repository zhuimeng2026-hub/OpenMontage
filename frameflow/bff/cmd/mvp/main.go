// Package main is the MVP standalone binary extended across Phases 0..4.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
// Phase 2 (2026-08-30): product / asset / manifest CRUD (6 routes).
// Phase 3 (2026-08-30): video project + production_jobs CRUD (11 routes).
//                      /render integrates quota reserve (§17.E).
// Phase 4 (2026-08-30): quota / billing — 4 routes.
//
// Runs on a separate port from the production BFF (default :18905 in Phase 4).
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
		// Phase 4 default: 18905 (Phase 3 was 18904, Phase 2 was 18903)
		port = "18905"
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
	projects := NewProjectHandler(db)
	quota := NewQuotaHandler(db)

	r := gin.Default()
	r.GET("/healthz", auth.HealthCheck)

	api := r.Group("/api")

	// --- Phase 0 public routes ---
	api.POST("/auth/login", jwtSvc.Login)
	api.GET("/me/jwt", jwtSvc.Me)

	// --- Phase 1: JWT only (no tenant header — caller may not have one yet) ---
	jwtOnly := api.Group("")
	jwtOnly.Use(middleware.RequireJWT(jwtSvc))
	jwtOnly.POST("/tenants", tenants.Create)
	jwtOnly.GET("/tenants", tenants.ListMine)

	// --- Phases 1+2+3+4: JWT + X-Tenant-Id (scoped) ---
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
	// Phase 3:
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
	// Phase 4:
	scoped.GET("/quota", quota.Get)
	scoped.POST("/quota/reserve", quota.Reserve)
	scoped.POST("/quota/consume", quota.Consume)
	scoped.POST("/quota/refund", quota.Refund)

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_4 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
