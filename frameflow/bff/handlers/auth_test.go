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
