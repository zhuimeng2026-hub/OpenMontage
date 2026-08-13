package mcp

import (
	"bytes"
	"crypto/tls"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Client is a single Streamable-HTTP MCP connection.
//
// CRITICAL: the upstream rotates Mcp-Session-Id on EVERY response, and the
// server binds uploaded assets to the session behind that id. So one logical
// user must reuse the SAME Client instance across all calls — otherwise the
// images uploaded by upload_asset_chunk and the later create_remotion_video_share
// land in different sessions and the素材 disappears.
//
// A mutex serializes requests on a client so the evolving SID stays consistent
// even if the browser fires several chunk uploads concurrently.
type Client struct {
	mu         sync.Mutex
	baseURL    string
	token      string
	sid        string
	httpClient *http.Client
	nextID     int
	// authHeader/authPrefix control how the token is sent. Defaults are the
	// standard MCP Bearer scheme; Weiyun's official MCP overrides them with
	// `WyHeader: mcp_token=<key>`.
	authHeader string
	authPrefix string
}

func NewClient(baseURL, token string) *Client {
	return NewClientAuth(baseURL, token, "Authorization", "Bearer ")
}

// NewClientAuth builds a Client with a custom auth header. Weiyun's official
// MCP server requires `WyHeader: mcp_token=<key>` instead of the Bearer scheme.
func NewClientAuth(baseURL, token, authHeader, authPrefix string) *Client {
	return &Client{
		baseURL:    baseURL,
		token:      token,
		authHeader: authHeader,
		authPrefix: authPrefix,
		httpClient: &http.Client{
			Timeout: 120 * time.Second,
			Transport: &http.Transport{
				TLSClientConfig: &tls.Config{InsecureSkipVerify: false},
			},
		},
	}
}

type jsonRPCRequest struct {
	JSONRPC string      `json:"jsonrpc"`
	ID      interface{} `json:"id,omitempty"`
	Method  string      `json:"method"`
	Params  interface{} `json:"params,omitempty"`
}

func (c *Client) id() int {
	c.nextID++
	return c.nextID
}

// do sends one JSON-RPC request, refreshes the rotating Mcp-Session-Id from the
// response header, and parses either a pure-JSON or an SSE ("data:") response.
func (c *Client) do(req jsonRPCRequest) (map[string]interface{}, error) {
	c.mu.Lock()
	defer c.mu.Unlock()

	body, err := json.Marshal(req)
	if err != nil {
		return nil, err
	}
	httpReq, err := http.NewRequest(http.MethodPost, c.baseURL, bytes.NewReader(body))
	if err != nil {
		return nil, err
	}
	httpReq.Header.Set("Content-Type", "application/json")
	httpReq.Header.Set("Accept", "application/json, text/event-stream")
	if c.authHeader != "" {
		httpReq.Header.Set(c.authHeader, c.authPrefix+c.token)
	}
	if c.sid != "" {
		httpReq.Header.Set("Mcp-Session-Id", c.sid)
	}

	resp, err := c.httpClient.Do(httpReq)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if sid := resp.Header.Get("Mcp-Session-Id"); sid != "" {
		c.sid = sid
	}

	raw, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("mcp http %d: %s", resp.StatusCode, strings.TrimSpace(string(raw)))
	}
	return parseResponse(raw)
}

func parseResponse(raw []byte) (map[string]interface{}, error) {
	s := strings.TrimSpace(string(raw))
	if s == "" {
		return nil, nil
	}
	var m map[string]interface{}
	if err := json.Unmarshal([]byte(s), &m); err == nil {
		return m, nil
	}
	// fall back to the last SSE "data:" frame
	var last string
	for _, ln := range strings.Split(s, "\n") {
		ln = strings.TrimSpace(ln)
		if strings.HasPrefix(ln, "data:") {
			last = strings.TrimSpace(strings.TrimPrefix(ln, "data:"))
		}
	}
	if last != "" {
		if err := json.Unmarshal([]byte(last), &m); err == nil {
			return m, nil
		}
		return map[string]interface{}{"_raw": last}, nil
	}
	if len(s) > 500 {
		s = s[:500]
	}
	return map[string]interface{}{"_raw": s}, nil
}

// Initialize performs the MCP handshake. Must run once before any tools/call on
// a fresh session; the rotating SID is captured by do().
func (c *Client) Initialize() error {
	if _, err := c.do(jsonRPCRequest{
		JSONRPC: "2.0",
		ID:      c.id(),
		Method:  "initialize",
		Params: map[string]interface{}{
			"protocolVersion": "2024-11-05",
			"capabilities":    map[string]interface{}{},
			"clientInfo":      map[string]interface{}{"name": "frameflow-bff", "version": "1.0.0"},
		},
	}); err != nil {
		return err
	}
	// notification — no id; server may rotate SID again
	_, err := c.do(jsonRPCRequest{
		JSONRPC: "2.0",
		Method:  "notifications/initialized",
	})
	return err
}

// ListTools returns the upstream MCP tool catalog (names + input schemas). Used
// both for the discoverability endpoint (GET /api/templates) and for the
// one-off schema probe.
func (c *Client) ListTools() (map[string]interface{}, error) {
	return c.do(jsonRPCRequest{
		JSONRPC: "2.0",
		ID:      c.id(),
		Method:  "tools/list",
	})
}

func (c *Client) CallTool(name string, args map[string]interface{}) (map[string]interface{}, error) {
	resp, err := c.do(jsonRPCRequest{
		JSONRPC: "2.0",
		ID:      c.id(),
		Method:  "tools/call",
		Params: map[string]interface{}{
			"name":      name,
			"arguments": args,
		},
	})
	if err != nil {
		return nil, err
	}
	return Extract(resp), nil
}

// Extract mirrors om_mcp_probe.extract: pull result.content[].text and JSON-parse it.
func Extract(resp map[string]interface{}) map[string]interface{} {
	if resp == nil {
		return nil
	}
	if result, ok := resp["result"].(map[string]interface{}); ok {
		if content, ok := result["content"].([]interface{}); ok {
			var sb strings.Builder
			for _, item := range content {
				if m, ok := item.(map[string]interface{}); ok {
					if text, ok := m["text"].(string); ok {
						sb.WriteString(text)
					}
				}
			}
			text := sb.String()
			var parsed map[string]interface{}
			if err := json.Unmarshal([]byte(text), &parsed); err == nil {
				return parsed
			}
			return map[string]interface{}{"_text": text}
		}
		return result
	}
	if errObj, ok := resp["error"]; ok {
		return map[string]interface{}{"error": errObj}
	}
	return resp
}

// IsSessionError reports whether the upstream rejected the call because the MCP
// session is gone (SID rotated away / expired). Used to trigger a re-init.
func IsSessionError(res map[string]interface{}) bool {
	if res == nil {
		return false
	}
	if e, ok := res["error"]; ok {
		s := strings.ToLower(fmt.Sprintf("%v", e))
		if strings.Contains(s, "mcp-session-id") || strings.Contains(s, "session") {
			return true
		}
	}
	return false
}
