package config

import (
	"fmt"
	"net/url"
	"os"
	"path/filepath"
	"strings"

	"github.com/joho/godotenv"
)

// Validate rejects unsafe production combinations before opening the listener.
func Validate(cfg *Config) error {
	if cfg == nil {
		return fmt.Errorf("config is nil")
	}
	if cfg.AuthRequired && (cfg.WechatAppID == "" || cfg.WechatAppSecret == "") {
		return fmt.Errorf("AUTH_REQUIRED=true requires WECHAT_APP_ID and WECHAT_APP_SECRET")
	}
	return nil
}

// Config holds every tunable for the FrameFlow BFF. Secrets (MCP_API_TOKEN,
// WechatAppSecret, ...) must come from the environment / .env, NEVER from the
// browser bundle.
type Config struct {
	MCPBaseURL        string // Streamable-HTTP MCP endpoint
	MCPAPIToken       string // Bearer token for the upstream MCP (server-side only)
	MCPProgressURL    string // base URL for the render-progress SSE endpoint
	WechatAppID       string
	WechatAppSecret   string // server-side only
	WechatRedirectURI string // optional override; defaults to our own callback
	WechatScope       string
	FrontendOrigin    string // allowed CORS origin (for dev with a separate frontend)
	Port              string
	SessionSecure     bool   // set true behind HTTPS
	StaticDir         string // directory that holds index.html / config.js / mcp-client.js
	StateDBPath       string // SQLite path for durable batches and quotas
	AuthRequired      bool   // require a logged-in WeChat session on /api/mcp + /api/render-progress
	// DevLoginAllowed enables the DEV-ONLY /api/_dev_login session bootstrap
	// (see Handlers.DevLogin). It must stay false in production.
	DevLoginAllowed bool
	RateLimitPerMin int // token-bucket refill rate per session/IP (0 => disabled)
	// CustomCompositionEnabled gates rendering of user-authored Remotion code.
	// The upstream MCP does NOT yet accept composition source, so this
	// stays false: a render request with custom code returns 501 + a clear note
	// instead of silently falling back to a template.
	// RepoRoot is the OpenMontage repository root (the parent of the
	// ``projects/`` directory where uploaded session assets live). It is used
	// to serve uploaded-asset thumbnails at /api/assets. Defaults to three
	// levels above StaticDir (frameflow/bff/web -> repo root) and can be
	// overridden with REPO_ROOT for non-standard deploy layouts.
	RepoRoot                 string
	CustomCompositionEnabled bool
	// BusinessStubJSON is an optional JSON map (business_key -> [{url,name}])
	// used by the default StubFetcher. Replace with a real Fetcher impl to pull
	// per-scenario images from the actual business system.
	BusinessStubJSON string
	// Weiyun official MCP (image source). When WEIYUN_API_KEY is set, the BFF
	// uses WeiyunFetcher (authorized via WyHeader: mcp_token=<key>); otherwise
	// it falls back to the StubFetcher below.
	WeiyunMCPURL   string
	WeiyunAPIToken string
	// Tier-based quota. DEFAULT_TIER sets the level applied to every user
	// (free = 10 files/submission, 10 tasks/day, 10 concurrent). TIER_OVERRIDES
	// is an optional JSON map of userID(fk_sid) -> tier for early-access grants;
	// a real user store / WeChat resolver can replace this later.
	DefaultTier   string
	TierOverrides string

	// ExternalAgentToken is a static bearer token accepted by /api/mcp-raw as
	// an alternative to WeChat auth. When set, external CLI/agent callers can
	// hit the BFF without a browser session. The token is hashed (SHA-256, first
	// 16 hex) and used as the SessionStore scope key, so all calls from the same
	// token share one upstream MCP session. Leave empty to disable the route.
	ExternalAgentToken string

	// VoiceboxUpstreamURL is the Streamable-HTTP MCP endpoint for the local
	// voicebox MCP server, served via OpenMontage's :8900 reverse-proxy mount
	// at /voicebox/mcp/. Proxied verbatim by POST /api/voicebox-mcp with no
	// state, no SessionStore, and no Mcp-Session-Id rotation pinning. The
	// trailing slash is required: voicebox's MCP route is mounted at
	// /voicebox/mcp/ and a bare /voicebox/mcp 301-strips to it. Defaults to
	// the production upstream (lanes.ymxt.top). Override with
	// VOICEBOX_UPSTREAM_URL=http://127.0.0.1:8900/voicebox/mcp/ on dev hosts.
	VoiceboxUpstreamURL string
}

func Load() *Config {
	_ = godotenv.Load()
	get := func(k, def string) string {
		if v := os.Getenv(k); v != "" {
			return v
		}
		return def
	}
	mcpBaseURL := firstNonEmpty(os.Getenv("MCP_BASE_URL"), os.Getenv("UPSTREAM_MCP_URL"), "http://127.0.0.1:8900/mcp")
	mcpProgressURL := strings.TrimSpace(os.Getenv("MCP_PROGRESS_URL"))
	if mcpProgressURL == "" {
		mcpProgressURL = deriveProgressURL(mcpBaseURL)
	}
	return &Config{
		MCPBaseURL:               mcpBaseURL,
		MCPAPIToken:              os.Getenv("MCP_API_TOKEN"),
		MCPProgressURL:           mcpProgressURL,
		WechatAppID:              os.Getenv("WECHAT_APP_ID"),
		WechatAppSecret:          os.Getenv("WECHAT_APP_SECRET"),
		WechatRedirectURI:        os.Getenv("WECHAT_REDIRECT_URI"),
		WechatScope:              get("WECHAT_SCOPE", "snsapi_userinfo"),
		FrontendOrigin:           get("FRONTEND_ORIGIN", ""),
		Port:                     get("BFF_PORT", "8080"),
		SessionSecure:            os.Getenv("SESSION_SECURE") == "true",
		StaticDir:                get("STATIC_DIR", "./web"),
		RepoRoot:                 get("REPO_ROOT", filepath.Join(get("STATIC_DIR", "./web"), "..", "..", "..")),
		StateDBPath:              get("STATE_DB_PATH", "./data/frameflow.db"),
		AuthRequired:             os.Getenv("AUTH_REQUIRED") == "true",
		DevLoginAllowed:          os.Getenv("DEV_LOGIN_ALLOWED") == "true",
		RateLimitPerMin:          getInt("RATE_LIMIT_PER_MIN", 30),
		CustomCompositionEnabled: os.Getenv("CUSTOM_COMPOSITION_ENABLED") == "true",
		BusinessStubJSON:         os.Getenv("BUSINESS_STUB_IMAGES"),
		WeiyunMCPURL:             get("WEIYUN_MCP_URL", "https://www.weiyun.com/api/v3/mcpserver"),
		WeiyunAPIToken:           os.Getenv("WEIYUN_API_KEY"),
		DefaultTier:              get("DEFAULT_TIER", "free"),
		TierOverrides:            os.Getenv("TIER_OVERRIDES"),
		ExternalAgentToken:       strings.TrimSpace(os.Getenv("EXTERNAL_AGENT_TOKEN")),
		VoiceboxUpstreamURL:      strings.TrimRight(get("VOICEBOX_UPSTREAM_URL", "http://lanes.ymxt.top:8900/voicebox/mcp/"), "/") + "/",
	}
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			return value
		}
	}
	return ""
}

// deriveProgressURL keeps the progress endpoint on the same upstream host as
// the MCP endpoint. The optional /mcp suffix is removed before appending the
// progress path; any query or fragment is deliberately discarded.
func deriveProgressURL(mcpBaseURL string) string {
	u, err := url.Parse(strings.TrimSpace(mcpBaseURL))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "http://127.0.0.1:8900/render-progress"
	}
	path := strings.TrimSuffix(u.Path, "/")
	if path == "/mcp" {
		path = ""
	} else if strings.HasSuffix(path, "/mcp") {
		path = strings.TrimSuffix(path, "/mcp")
	}
	u.Path = path + "/render-progress"
	u.RawPath = ""
	u.RawQuery = ""
	u.Fragment = ""
	u.User = nil
	return u.String()
}

// SafeEndpoint returns only non-sensitive URL components for startup logs.
func SafeEndpoint(raw string) string {
	u, err := url.Parse(strings.TrimSpace(raw))
	if err != nil || u.Scheme == "" || u.Host == "" {
		return "<invalid>"
	}
	u.User = nil
	u.RawQuery = ""
	u.Fragment = ""
	return u.String()
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
