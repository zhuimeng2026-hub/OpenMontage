// Package main is the MVP standalone binary extended across Phases 0, 1, and 2.
//
// Phase 0 (2026-08-30): POST /api/auth/login, GET /api/me/jwt, GET /healthz.
// Phase 1 (2026-08-30): tenant CRUD + signed file URLs.
// Phase 2 (2026-08-30): product / asset / manifest CRUD (6 routes).
//
// Runs on a separate port from the production BFF (default :18903 in Phase 2)
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
		// Phase 2 default: 18903 (Phase 1 was 18902, Phase 0 was 18901)
		port = "18903"
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

	// --- Phase 1 signed file serve: NO JWT required (URL itself authorizes) ---
	api.GET("/files/:key", files.Serve)

	log.Printf("[mvp] phase_2 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
