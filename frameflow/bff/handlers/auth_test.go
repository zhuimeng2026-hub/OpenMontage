package handlers

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/mcp"
	"frameflow-bff/internal/state"
)

func TestRequireAuthFailsClosedWhenWechatMissing(t *testing.T) {
	gin.SetMode(gin.TestMode)
	r := gin.New()
	h := New(&config.Config{AuthRequired: true}, mcp.NewSessionStore("http://127.0.0.1:1", "", nil), nil, nil, nil)
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

// TestWechatSessionSurvivesInMemoryLoss verifies the fix for "refresh jumps back
// to the login page": a logged-in user must remain authenticated after a BFF
// restart (or on another instance). The in-memory userStore is only a hot cache;
// the durable wechat_users table must restore the session when the cache is cold.
func TestWechatSessionSurvivesInMemoryLoss(t *testing.T) {
	db, err := state.Open(filepath.Join(t.TempDir(), "frameflow.db"))
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()

	prev := userDB
	userDB = db
	defer func() { userDB = prev }()

	h := &Handlers{}
	const sid = "persist-sid"
	h.saveUser(sid, map[string]interface{}{"openid": "wx-persist-1", "nickname": "Alice"})

	// Simulate a BFF restart / another instance: the hot cache is gone.
	userStore.Lock()
	delete(userStore.m, sid)
	userStore.Unlock()

	got := h.loadUser(sid)
	if got == nil {
		t.Fatal("loadUser should restore the login from SQLite, but returned nil (refresh-logout bug not fixed)")
	}
	if got["openid"] != "wx-persist-1" {
		t.Fatalf("restored user openid = %v, want wx-persist-1", got["openid"])
	}
}
