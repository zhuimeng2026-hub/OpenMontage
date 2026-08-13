package main

import (
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strings"

	"github.com/gin-gonic/gin"

	"frameflow-bff/handlers"
	"frameflow-bff/internal/business"
	"frameflow-bff/internal/composition"
	"frameflow-bff/internal/config"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
	"frameflow-bff/internal/template"
)

func main() {
	cfg := config.Load()
	store := mcp.NewSessionStore(cfg.MCPBaseURL, cfg.MCPAPIToken)
	h := handlers.New(cfg, store)
	comps := composition.NewStore()
	ch := handlers.NewCompositionHandler(cfg, comps, store)

	tpls := template.NewStore()
	var fetcher business.Fetcher
	if cfg.WeiyunAPIToken != "" {
		wyClient := mcp.NewClientAuth(cfg.WeiyunMCPURL, cfg.WeiyunAPIToken, "WyHeader", "mcp_token=")
		wyFetcher := business.NewWeiyunFetcher(wyClient)
		if err := wyFetcher.Initialize(); err != nil {
			// Key may be invalid / network may be down. Keep the fetcher wired so
			// a later request surfaces the real Weiyun error instead of silently
			// falling back to stub data.
			log.Printf("[WARN] Weiyun MCP handshake failed — WeiyunFetcher active but calls will error until the key/network is valid: %v", err)
		} else {
			log.Printf("[business] using WeiyunFetcher (official MCP %s)", cfg.WeiyunMCPURL)
		}
		fetcher = wyFetcher
	} else {
		fetcher = business.NewStubFetcher(cfg.BusinessStubJSON)
		log.Println("[business] WEIYUN_API_KEY not set — using StubFetcher (set BUSINESS_STUB_IMAGES or WEIYUN_API_KEY to source real images).")
	}
	tierLimits := limits.NewResolver(cfg.DefaultTier, cfg.TierOverrides)
	usage := limits.NewUsage()
	th := handlers.NewTemplateHandler(cfg, tpls, store, fetcher, tierLimits, usage)

	r := gin.Default()
	r.Use(corsMiddleware(cfg))

	api := r.Group("/api")
	api.Use(h.RateLimit.Middleware())
	{
		// Expensive, upstream-facing routes: rate-limited (group) + auth-gated.
		api.POST("/mcp", h.RequireAuth(), h.MCPProxy)
		api.GET("/render-progress/:jobId", h.RequireAuth(), h.RenderProgress)
		// Public: WeChat OAuth entry, session probe, logout.
		api.GET("/wechat/login", h.WechatLogin)
		api.GET("/wechat/callback", h.WechatCallback)
		api.GET("/me", h.Me)
		api.POST("/logout", h.Logout)
		// Custom Remotion composition editor surface (save/list/get are local;
		// render is upstream-facing and auth-gated).
		api.GET("/compositions", ch.List)
		api.POST("/compositions", ch.Create)
		api.GET("/compositions/:id", ch.Get)
		api.POST("/compositions/:id/render", h.RequireAuth(), ch.Render)
		// Batch-render surface: a reusable Template (fixed script) + Scenarios
		// (per-scenario image sets from the business system) -> N videos.
		api.GET("/templates", th.ListTemplates)
		api.POST("/templates", th.CreateTemplate)
		api.GET("/templates/:id", th.GetTemplate)
		api.POST("/templates/:id/scenarios", th.AddScenario)
		api.GET("/templates/:id/scenarios", th.ListScenarios)
		api.POST("/templates/:id/batch-render", h.RequireAuth(), th.BatchRender)
		api.GET("/templates/:id/batch-render/:jobId", th.GetBatchJob)
		api.GET("/quota", th.GetQuota)
	}

	// Serve the SPA from STATIC_DIR. Put index.html / config.js / mcp-client.js
	// there so the frontend and /api share ONE origin — the session cookie then
	// works with zero CORS friction. Unknown paths fall back to index.html
	// (SPA client routing).
	r.NoRoute(spaFallback(cfg.StaticDir))

	if cfg.AuthRequired && cfg.WechatAppID == "" {
		log.Println("[WARN] AUTH_REQUIRED=true but WechatAppID is not configured — /api/mcp is open. Configure the WeChat service account before launch.")
	}
	if !cfg.AuthRequired {
		log.Println("[WARN] AUTH_REQUIRED is false — /api/mcp is open to anonymous callers. Set AUTH_REQUIRED=true before launch.")
	}
	if !cfg.CustomCompositionEnabled {
		log.Println("[WARN] CUSTOM_COMPOSITION_ENABLED is false — the editor's 渲染此合成 will save the composition but return 501 (upstream MCP does not yet accept custom composition code). Set it true once upstream supports it.")
	}
	log.Printf("[quota] tier resolver ready — default tier=%q (free: 10 files/submission, 10 tasks/day, 10 concurrent). Override per user via TIER_OVERRIDES.", cfg.DefaultTier)

	log.Printf("FrameFlow BFF listening on :%s (static dir: %s)", cfg.Port, cfg.StaticDir)
	if err := r.Run(":" + cfg.Port); err != nil {
		log.Fatal(err)
	}
}

// spaFallback serves real files under dir and falls back to index.html for
// anything else (SPA routes).
func spaFallback(dir string) gin.HandlerFunc {
	return func(c *gin.Context) {
		rel := strings.TrimPrefix(c.Request.URL.Path, "/")
		if rel == "" {
			rel = "index.html"
		}
		full := filepath.Join(dir, filepath.Clean(rel))
		if info, err := os.Stat(full); err == nil && !info.IsDir() {
			c.File(full)
			return
		}
		c.File(filepath.Join(dir, "index.html"))
	}
}

// corsMiddleware allows the configured frontend origin (for dev with a separate
// static server). In the recommended same-origin deploy it is effectively a
// no-op, but it is required when the SPA is served from a different host.
func corsMiddleware(cfg *config.Config) gin.HandlerFunc {
	allowed := []string{}
	if cfg.FrontendOrigin != "" {
		allowed = append(allowed, cfg.FrontendOrigin)
	}
	allowed = append(allowed, "http://localhost:5173", "http://localhost:8080")
	return func(c *gin.Context) {
		origin := c.GetHeader("Origin")
		for _, a := range allowed {
			if a == origin {
				c.Header("Access-Control-Allow-Origin", origin)
				break
			}
		}
		c.Header("Access-Control-Allow-Credentials", "true")
		c.Header("Access-Control-Allow-Headers", "Content-Type, Authorization")
		c.Header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
		if c.Request.Method == http.MethodOptions {
			c.AbortWithStatus(http.StatusNoContent)
			return
		}
		c.Next()
	}
}
