package handlers

import (
	"testing"

	"frameflow-bff/internal/config"
)

func TestRenderSubmissionTools(t *testing.T) {
	for _, tool := range []string{
		"create_remotion_video_share",
		"create_captioned_video_share",
		"create_cloned_voice_video_share",
	} {
		if !isRenderSubmissionTool(tool) {
			t.Fatalf("expected %s to be a render submission tool", tool)
		}
	}
	if isRenderSubmissionTool("execute_tool") {
		t.Fatal("generic execute_tool must not be a render submission tool")
	}
}

func TestMCPToolAllowlistDefaultsAndOverrides(t *testing.T) {
	h := &Handlers{Cfg: &config.Config{}}
	if !h.mcpToolAllowed("create_captioned_video_share") {
		t.Fatal("caption workflow should be allowed by default")
	}
	if h.mcpToolAllowed("execute_tool") {
		t.Fatal("generic execute_tool should be denied by default")
	}
	h.Cfg.MCPAllowedTools = []string{"custom_tool"}
	if !h.mcpToolAllowed("custom_tool") || h.mcpToolAllowed("upload_asset") {
		t.Fatal("explicit allowlist must replace defaults")
	}
}

func TestRenderJobIDSupportsBothContracts(t *testing.T) {
	if got := renderJobID(map[string]interface{}{"render_job_id": "r1"}); got != "r1" {
		t.Fatalf("render_job_id=%q", got)
	}
	if got := renderJobID(map[string]interface{}{"job_id": "j1"}); got != "j1" {
		t.Fatalf("job_id=%q", got)
	}
}
