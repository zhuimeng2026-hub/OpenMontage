package config

import (
	"testing"
)

func TestValidateAuthFailsClosed(t *testing.T) {
	if err := Validate(&Config{AuthRequired: true}); err == nil {
		t.Fatal("expected missing WeChat configuration to be rejected")
	}
	if err := Validate(&Config{AuthRequired: true, WechatAppID: "id", WechatAppSecret: "secret"}); err != nil {
		t.Fatalf("valid auth config rejected: %v", err)
	}
}

func TestFirstNonEmptyMCPBaseURLPriority(t *testing.T) {
	if got := firstNonEmpty("  http://primary/mcp ", "http://legacy/mcp", "http://default/mcp"); got != "http://primary/mcp" {
		t.Fatalf("MCP_BASE_URL should win, got %q", got)
	}
}

func TestMCPBaseURLLegacyFallback(t *testing.T) {
	if got := firstNonEmpty("", " http://legacy.example:8900/mcp ", "http://default/mcp"); got != "http://legacy.example:8900/mcp" {
		t.Fatalf("UPSTREAM_MCP_URL fallback mismatch: %q", got)
	}
}

func TestDeriveProgressURLFromRemoteMCP(t *testing.T) {
	for _, tc := range []struct {
		base string
		want string
	}{
		{"https://remote.example:8900/mcp", "https://remote.example:8900/render-progress"},
		{"https://remote.example:8900/mcp?token=secret", "https://remote.example:8900/render-progress"},
		{"https://remote.example:8900/api/mcp", "https://remote.example:8900/api/render-progress"},
	} {
		if got := deriveProgressURL(tc.base); got != tc.want {
			t.Errorf("deriveProgressURL(%q) = %q, want %q", tc.base, got, tc.want)
		}
	}
}

func TestLoadMCPDefaultsToLocalhost(t *testing.T) {
	// Setenv takes precedence over godotenv.Load, while clearing these keys
	// makes this test independent of the developer's shell environment.
	for _, key := range []string{"MCP_BASE_URL", "UPSTREAM_MCP_URL", "MCP_PROGRESS_URL"} {
		t.Setenv(key, "")
	}
	cfg := Load()
	if cfg.MCPBaseURL != "http://127.0.0.1:8900/mcp" {
		t.Fatalf("default MCP base URL = %q", cfg.MCPBaseURL)
	}
	if cfg.MCPProgressURL != "http://127.0.0.1:8900/render-progress" {
		t.Fatalf("default progress URL = %q", cfg.MCPProgressURL)
	}
}

func TestSafeEndpointOmitsSensitiveURLParts(t *testing.T) {
	if got := SafeEndpoint("https://user:pass@remote.example:8900/mcp?token=secret#fragment"); got != "https://remote.example:8900/mcp" {
		t.Fatalf("SafeEndpoint leaked or changed URL parts: %q", got)
	}
}

func TestParseCSVDeduplicatesAndTrims(t *testing.T) {
	got := parseCSV(" upload_asset,create_captioned_video_share, upload_asset ,, ")
	if len(got) != 2 || got[0] != "upload_asset" || got[1] != "create_captioned_video_share" {
		t.Fatalf("unexpected parsed allowlist: %#v", got)
	}
}

// The default allowlist is an explicit closed list, never a wildcard: any tool
// not named here is rejected by the /api/mcp handler. Note that it DOES include
// execute_tool / get_tool_info / list_tools / dry_run_tool — the script-mode
// editor composes the subtitle workflow by calling those, and every
// authorization and resource check still happens on the upstream MCP. Removing
// them breaks script-mode rendering, so the invariant asserted here is
// "no wildcard and no arbitrary tool names", not "no generic execution".
func TestDefaultAllowlistIsExplicitAndClosed(t *testing.T) {
	allowed := DefaultMCPAllowedTools()
	if len(allowed) == 0 {
		t.Fatal("default allowlist must not be empty")
	}
	for _, tool := range allowed {
		if tool == "*" || tool == "" {
			t.Fatalf("default allowlist must not contain a wildcard or empty entry, got %q", tool)
		}
	}
	for _, unexpected := range []string{"session_start", "delete_everything", "shell_exec"} {
		for _, tool := range allowed {
			if tool == unexpected {
				t.Fatalf("default allowlist unexpectedly exposes %q", unexpected)
			}
		}
	}
}
