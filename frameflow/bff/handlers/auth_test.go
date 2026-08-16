package handlers

import (
	"net/http"
	"net/http/httptest"
	"testing"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/mcp"
)

func TestRequireAuthFailsClosedWhenWechatMissing(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h := New(&config.Config{AuthRequired: true}, mcp.NewSessionStore("http://127.0.0.1:1", ""), nil, nil)
	r.GET("/protected", h.RequireAuth(), func(c *gin.Context) { c.Status(http.StatusNoContent) })
	req := httptest.NewRequest(http.MethodGet, "/protected", nil)
	res := httptest.NewRecorder()
	r.ServeHTTP(res, req)
	if res.Code != http.StatusServiceUnavailable {
		t.Fatalf("status=%d, want %d", res.Code, http.StatusServiceUnavailable)
	}
}

func TestRenderQueueOwnerUsesStableWechatIdentity(t *testing.T) {
	h := &Handlers{}
	h.saveUser("session-a", map[string]interface{}{"openid": "wx-user-1"})
	h.saveUser("session-b", map[string]interface{}{"openid": "wx-user-1"})
	t.Cleanup(func() {
		h.dropUser("session-a")
		h.dropUser("session-b")
	})

	a := renderQueueOwnerID("session-a")
	b := renderQueueOwnerID("session-b")
	if a != b {
		t.Fatalf("same WeChat user received different queue owners: %q != %q", a, b)
	}
	if a == "wx-user-1" || len(a) != 64 {
		t.Fatalf("queue owner must be an opaque SHA-256 key, got %q", a)
	}
}

func TestRenderQueueOwnerSeparatesAnonymousSessions(t *testing.T) {
	if renderQueueOwnerID("session-a") == renderQueueOwnerID("session-b") {
		t.Fatal("different anonymous sessions must not share a queue owner")
	}
}
