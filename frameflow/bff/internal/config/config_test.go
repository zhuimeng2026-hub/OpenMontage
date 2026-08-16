package config

import "testing"

func TestValidateAuthFailsClosed(t *testing.T) {
	if err := Validate(&Config{AuthRequired: true}); err == nil {
		t.Fatal("expected missing WeChat configuration to be rejected")
	}
	if err := Validate(&Config{AuthRequired: true, WechatAppID: "id", WechatAppSecret: "secret"}); err != nil {
		t.Fatalf("valid auth config rejected: %v", err)
	}
}
