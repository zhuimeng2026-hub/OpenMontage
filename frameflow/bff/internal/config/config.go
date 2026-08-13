package config

import (
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
	}
}
