package mcp

import (
	"fmt"
	"sync"
)

// SessionStore keeps one long-lived MCP Client per BFF user session so that a
// user's chunk uploads and their create_remotion_video_share call always share
// the same server-side MCP session.
//
// For multi-instance deploys, swap the in-memory map for Redis keyed by the BFF
// session id (the MCP SID would then need to be persisted alongside, or the
// upstream must support session resumption).
type SessionStore struct {
	mu      sync.RWMutex
	clients map[string]*Client
	baseURL string
	token   string
}

func NewSessionStore(baseURL, token string) *SessionStore {
	return &SessionStore{
		clients: make(map[string]*Client),
		baseURL: baseURL,
		token:   token,
	}
}

func (s *SessionStore) getOrCreate(sessionID string) (*Client, error) {
	s.mu.RLock()
	c, ok := s.clients[sessionID]
	s.mu.RUnlock()
	if ok {
		return c, nil
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	if c, ok := s.clients[sessionID]; ok {
		return c, nil
	}
	c = NewClient(s.baseURL, s.token)
	if err := c.Initialize(); err != nil {
		return nil, fmt.Errorf("mcp initialize: %w", err)
	}
	s.clients[sessionID] = c
	return c, nil
}

func (s *SessionStore) drop(sessionID string) {
	s.mu.Lock()
	delete(s.clients, sessionID)
	s.mu.Unlock()
}

// Call runs a tool call on the user's MCP client, transparently re-initializing
// once if the upstream reports a stale/expired session.
func (s *SessionStore) Call(sessionID, tool string, args map[string]interface{}) (map[string]interface{}, error) {
	c, err := s.getOrCreate(sessionID)
	if err != nil {
		return nil, err
	}
	res, err := c.CallTool(tool, args)
	if err != nil {
		return nil, err
	}
	if IsSessionError(res) {
		s.drop(sessionID)
		c, err = s.getOrCreate(sessionID)
		if err != nil {
			return nil, err
		}
		return c.CallTool(tool, args)
	}
	return res, nil
}
