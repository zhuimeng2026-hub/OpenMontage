package config

import (
	"fmt"
	"os"

	"github.com/joho/godotenv"
)

// Config holds every tunable for the FrameFlow BFF. Secrets (MCP_API_TOKEN,
// WechatAppSecret, ...) must come from the environment / .env, NEVER from the
// browser bundle.
type Config struct {
	MCPBaseURL        string // Streamable-HTTP MCP endpoint
	MCPAPIToken       string // Bearer token for dw.aixifs.com/mcp (server-side only)
	MCPProgressURL    string // base URL for the render-progress SSE endpoint
	WechatAppID       string
	WechatAppSecret   string // server-side only
	WechatRedirectURI string // optional override; defaults to our own callback
	WechatScope       string
	FrontendOrigin    string // allowed CORS origin (for dev with a separate frontend)
	Port              string
	SessionSecure     bool // set true behind HTTPS
	StaticDir         string // directory that holds index.html / config.js / mcp-client.js
	AuthRequired      bool // require a logged-in WeChat session on /api/mcp + /api/render-progress
	RateLimitPerMin   int  // token-bucket refill rate per session/IP (0 => 30)
	// CustomCompositionEnabled gates rendering of user-authored Remotion code.
	// Upstream dw.aixifs.com/mcp does NOT yet accept composition source, so this
	// stays false: a render request with custom code returns 501 + a clear note
	// instead of silently falling back to a template.
	CustomCompositionEnabled bool
}

func Load() *Config {
	_ = godotenv.Load()
	get := func(k, def string) string {
		if v := os.Getenv(k); v != "" {
			return v
		}
		return def
	}
	return &Config{
		MCPBaseURL:        get("MCP_BASE_URL", "https://dw.aixifs.com/mcp"),
		MCPAPIToken:       os.Getenv("MCP_API_TOKEN"),
		MCPProgressURL:    get("MCP_PROGRESS_URL", "https://dw.aixifs.com/render-progress"),
		WechatAppID:       os.Getenv("WECHAT_APP_ID"),
		WechatAppSecret:   os.Getenv("WECHAT_APP_SECRET"),
		WechatRedirectURI: os.Getenv("WECHAT_REDIRECT_URI"),
		WechatScope:       get("WECHAT_SCOPE", "snsapi_userinfo"),
		FrontendOrigin:    get("FRONTEND_ORIGIN", ""),
		Port:              get("BFF_PORT", "8080"),
		SessionSecure:     os.Getenv("SESSION_SECURE") == "true",
		StaticDir:         get("STATIC_DIR", "./web"),
		AuthRequired:      os.Getenv("AUTH_REQUIRED") == "true",
		RateLimitPerMin:   getInt("RATE_LIMIT_PER_MIN", 30),
		CustomCompositionEnabled: os.Getenv("CUSTOM_COMPOSITION_ENABLED") == "true",
	}
}

// getInt reads an env var as int, falling back to def when unset/invalid.
func getInt(k string, def int) int {
	if v := os.Getenv(k); v != "" {
		var n int
		if _, err := fmt.Sscanf(v, "%d", &n); err == nil {
			return n
		}
	}
	return def
}
