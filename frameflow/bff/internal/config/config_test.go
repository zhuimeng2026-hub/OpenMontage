package config

import "testing"

func TestParseCSVDeduplicatesAndTrims(t *testing.T) {
	got := parseCSV(" upload_asset,create_captioned_video_share, upload_asset ,, ")
	if len(got) != 2 || got[0] != "upload_asset" || got[1] != "create_captioned_video_share" {
		t.Fatalf("unexpected parsed allowlist: %#v", got)
	}
}

func TestDefaultAllowlistExcludesGenericExecution(t *testing.T) {
	for _, tool := range DefaultMCPAllowedTools() {
		if tool == "execute_tool" {
			t.Fatal("execute_tool must not be browser-exposed by default")
		}
	}
}
