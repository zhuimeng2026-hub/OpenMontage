package mcp

import (
	"fmt"
	"sync"
	"time"
)

// RenderJob is one entry in a user's render queue. It is recorded by the BFF
// when a render is submitted and is scoped to the BFF session (ff_sid), so the
// queue endpoint inherently returns only the caller's own jobs.
type RenderJob struct {
	JobID     string    `json:"job_id"`
	Name      string    `json:"name"`
	Res       string    `json:"res"`     // e.g. "9:16" / "1080p"
	Status    string    `json:"status"`  // 渲染中 / 排队 / 已完成 / 失败
	CreatedAt time.Time `json:"created_at"`
}

// SessionStore keeps one long-lived MCP Client per BFF user session so that a
// user's chunk uploads and their create_remotion_video_share call always share
// the same server-side MCP session.
//
// For multi-instance deploys, swap the in-memory map for Redis keyed by the BFF
// session id (the MCP SID would then need to be persisted alongside, or the
// upstream must support session resumption).
type SessionStore struct {
	mu         sync.RWMutex
	clients    map[string]*Client
	assetCount map[string]int // sid -> completed uploads in the CURRENT submission
	renderJobs map[string][]*RenderJob // sid -> own render jobs (newest first)
	baseURL    string
	token      string
}

func NewSessionStore(baseURL, token string) *SessionStore {
	return &SessionStore{
		clients:    make(map[string]*Client),
		assetCount: make(map[string]int),
		renderJobs: make(map[string][]*RenderJob),
		baseURL:    baseURL,
		token:      token,
	}
}

// RecordJob prepends a render job to the session's queue (newest first) and
// caps the retained history so memory stays bounded per session.
func (s *SessionStore) RecordJob(sessionID string, job RenderJob) {
	s.mu.Lock()
	defer s.mu.Unlock()
	jobs := s.renderJobs[sessionID]
	jobs = append([]*RenderJob{&job}, jobs...)
	const maxKeep = 100
	if len(jobs) > maxKeep {
		jobs = jobs[:maxKeep]
	}
	s.renderJobs[sessionID] = jobs
}

// ListJobs returns a copy of the session's render jobs (newest first).
func (s *SessionStore) ListJobs(sessionID string) []*RenderJob {
	s.mu.RLock()
	defer s.mu.RUnlock()
	jobs := s.renderJobs[sessionID]
	out := make([]*RenderJob, len(jobs))
	copy(out, jobs)
	return out
}

// UpdateJobStatus rewrites the status of a single job (used when the upstream
// render reaches a terminal state).
func (s *SessionStore) UpdateJobStatus(sessionID, jobID, status string) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, j := range s.renderJobs[sessionID] {
		if j.JobID == jobID {
			j.Status = status
			return
		}
	}
}

// AssetCount returns how many images the manual upload flow has completed for
// the current submission of a BFF session. It is the basis for the
// per-tier MaxFilesPerSubmission cap enforced in MCPProxy.
func (s *SessionStore) AssetCount(sessionID string) int {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.assetCount[sessionID]
}

// IncAsset records one completed upload for the session's current submission.
func (s *SessionStore) IncAsset(sessionID string) {
	s.mu.Lock()
	s.assetCount[sessionID]++
	s.mu.Unlock()
}

// ResetAsset clears the submission counter — called after a video is created so
// the next submission starts from zero again.
func (s *SessionStore) ResetAsset(sessionID string) {
	s.mu.Lock()
	delete(s.assetCount, sessionID)
	s.mu.Unlock()
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
