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
	if !h.mcpToolAllowed("create_cloned_voice_video_share") {
		t.Fatal("cloned voice workflow should be allowed by default")
	}
	if !h.mcpToolAllowed("upload_asset_chunk") || !h.mcpToolAllowed("create_remotion_video_share") || !h.mcpToolAllowed("get_render_status") {
		t.Fatal("the core upload/render/status surface must stay allowed by default")
	}
	// execute_tool IS in the default allowlist on purpose: the script-mode
	// editor composes the subtitle workflow by calling execute_tool with
	// tool_name=transcriber|translator|subtitle_gen|remotion_caption_burn|
	// tts_selector (see frameflow/bff/web/index.html). The BFF only relays the
	// call — every authorization and resource check happens on the upstream
	// MCP. Removing it here silently breaks script mode, so the deployment-time
	// way to tighten this is MCP_ALLOWED_TOOLS, not a code change.
	if !h.mcpToolAllowed("execute_tool") {
		t.Fatal("execute_tool must stay allowed by default: script-mode subtitle workflow depends on it")
	}
	// The list stays closed: an arbitrary tool is still rejected.
	if h.mcpToolAllowed("some_arbitrary_tool") {
		t.Fatal("an unknown tool must be denied by default")
	}
	h.Cfg.MCPAllowedTools = []string{"custom_tool"}
	if !h.mcpToolAllowed("custom_tool") || h.mcpToolAllowed("upload_asset") {
		t.Fatal("explicit allowlist must replace defaults")
	}
	// ...and that is also how a deployment opts execute_tool back out.
	h.Cfg.MCPAllowedTools = []string{"upload_asset_chunk", "create_remotion_video_share"}
	if h.mcpToolAllowed("execute_tool") {
		t.Fatal("MCP_ALLOWED_TOOLS must be able to opt execute_tool out")
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
