package mcp

import (
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"sync"
	"testing"
)

func TestIsSessionTransportErrorStrict(t *testing.T) {
	for _, err := range []error{
		errors.New(`mcp http 404: {"message":"Session not found"}`),
		errors.New(`mcp http 410: {"message":"session expired"}`),
	} {
		if !IsSessionTransportError(err) {
			t.Fatalf("expected session transport error: %v", err)
		}
	}
	for _, err := range []error{
		errors.New(`mcp http 404: {"message":"No uploaded image batch found for this MCP session"}`),
		errors.New(`mcp http 500: {"message":"Session not found"}`),
		errors.New("ordinary business error mentioning session"),
	} {
		if IsSessionTransportError(err) {
			t.Fatalf("ordinary error must not reconnect: %v", err)
		}
	}
}

type mcpRecoveryServer struct {
	mu         sync.Mutex
	toolCalls  int
	initCalls  int
	nonSession bool
}

func (s *mcpRecoveryServer) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	var req struct {
		Method string `json:"method"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	w.Header().Set("Content-Type", "application/json")
	s.mu.Lock()
	defer s.mu.Unlock()
	switch req.Method {
	case "initialize":
		s.initCalls++
		w.Header().Set("Mcp-Session-Id", "sid-recovered")
		_, _ = w.Write([]byte(`{"result":{}}`))
	case "notifications/initialized":
		w.Header().Set("Mcp-Session-Id", "sid-recovered")
		_, _ = w.Write([]byte(`{}`))
	case "tools/call":
		s.toolCalls++
		if s.toolCalls == 1 {
			w.WriteHeader(http.StatusNotFound)
			if s.nonSession {
				_, _ = w.Write([]byte(`{"message":"No uploaded image batch found for this MCP session"}`))
			} else {
				_, _ = w.Write([]byte(`{"message":"Session not found"}`))
			}
			return
		}
		_, _ = w.Write([]byte(`{"result":{"content":[{"text":"{\"ok\":true}"}]}}`))
	default:
		w.WriteHeader(http.StatusBadRequest)
	}
}

func (s *mcpRecoveryServer) counts() (int, int) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.toolCalls, s.initCalls
}

func TestCallBatchRecoversFromHTTP404SessionNotFound(t *testing.T) {
	upstream := &mcpRecoveryServer{}
	ts := httptest.NewServer(upstream)
	defer ts.Close()
	store := NewSessionStore(ts.URL, "token")
	if err := store.CreateBatch("sid", "batch-1", "project-1"); err != nil {
		t.Fatal(err)
	}
	res, err := store.CallBatch("sid", "batch-1", "project-1", "retry_render_publish", nil)
	if err != nil || res["ok"] != true {
		t.Fatalf("CallBatch() = %#v, %v", res, err)
	}
	tools, initializes := upstream.counts()
	if tools != 2 || initializes != 2 {
		t.Fatalf("expected 2 tool calls and 2 initialize calls, got tools=%d initialize=%d", tools, initializes)
	}
}

func TestCallRecoversFromHTTP404SessionNotFound(t *testing.T) {
	upstream := &mcpRecoveryServer{}
	ts := httptest.NewServer(upstream)
	defer ts.Close()
	store := NewSessionStore(ts.URL, "token")
	res, err := store.Call("sid", "retry_render_publish", nil)
	if err != nil || res["ok"] != true {
		t.Fatalf("Call() = %#v, %v", res, err)
	}
	tools, initializes := upstream.counts()
	if tools != 2 || initializes != 2 {
		t.Fatalf("expected 2 tool calls and 2 initialize calls, got tools=%d initialize=%d", tools, initializes)
	}
}

func TestCallDoesNotRetryNonSessionHTTP404(t *testing.T) {
	upstream := &mcpRecoveryServer{nonSession: true}
	ts := httptest.NewServer(upstream)
	defer ts.Close()
	store := NewSessionStore(ts.URL, "token")
	if _, err := store.Call("sid", "retry_render_publish", nil); err == nil {
		t.Fatal("non-session 404 must be returned")
	}
	tools, initializes := upstream.counts()
	if tools != 1 || initializes != 1 {
		t.Fatalf("non-session 404 was retried: tools=%d initialize=%d", tools, initializes)
	}
}
