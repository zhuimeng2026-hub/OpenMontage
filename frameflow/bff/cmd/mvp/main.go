// Package main is the MVP §17.A standalone server: POST /api/auth/login,
// GET /api/me/jwt, GET /healthz. It runs alongside the real frameflow-bff
// (different port). Doesn't touch main.go.
package main

import (
	"log"
	"net/http"
	"os"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/auth"
	"frameflow-bff/internal/config"
)

func main() {
	cfg := config.Load()
	if err := config.Validate(cfg); err != nil {
		log.Printf("[mvp] WARN config validate: %v (continuing — JWT seed handles missing WeChat)", err)
	}

	port := os.Getenv("MVP_PORT")
	if port == "" {
		port = "18901"  # avoid 8901 (tweak_server uvicorn) and 8900 (frameflow-bff)
	}

	dbPath := os.Getenv("MVP_DB_PATH")
	if dbPath == "" {
		dbPath = "/opt/OpenMontage_Voicebox/frameflow/bff/data/frameflow.db"
	}

	// Open SQLite directly (not via the real main's setup).
	db := openDB(dbPath)
	defer db.Close()

	jwtSvc := auth.NewJWTService(cfg, db)

	r := gin.Default()
	r.GET("/healthz", auth.HealthCheck)

	api := r.Group("/api")
	api.POST("/auth/login", jwtSvc.Login)
	api.GET("/me/jwt", jwtSvc.Me)

	log.Printf("[mvp] phase_0 server listening on :%s WEIXIN_MOCK_AUTH=%s", port, os.Getenv("WEIXIN_MOCK_AUTH"))
	if err := http.ListenAndServe(":"+port, r); err != nil {
		log.Fatal(err)
	}
}
