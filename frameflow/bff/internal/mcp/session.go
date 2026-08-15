package mcp

import (
	"database/sql"
	"fmt"
	"log"
	"sync"
	"time"
)

// RenderJob is one entry in a user's render queue. It is recorded by the BFF
// when a render is submitted and is scoped to the BFF session (ff_sid), so the
// queue endpoint inherently returns only the caller's own jobs.
type RenderJob struct {
	JobID     string    `json:"job_id"`
	Name      string    `json:"name"`
	Res       string    `json:"res"`    // e.g. "9:16" / "1080p"
	Status    string    `json:"status"` // 渲染中 / 排队 / 已完成 / 失败
	CreatedAt time.Time `json:"created_at"`
	BatchID   string    `json:"batch_id,omitempty"`
	ProjectID string    `json:"project_id,omitempty"`
}

// SessionStore keeps the legacy user-scoped MCP Client for manual uploads and
// one independent MCP Client per image batch. Batch clients are addressable by
// both batch_id and project_id so every part of a batch uses one upstream MCP
// session without changing the old user-level flow.
//
// For multi-instance deploys, swap the in-memory map for Redis keyed by the BFF
// session id (the MCP SID would then need to be persisted alongside, or the
// upstream must support session resumption).
type SessionStore struct {
	mu         sync.RWMutex
	clients    map[string]*Client
	batchIDs   map[string]*batchClient
	projects   map[string]*batchClient
	assetCount map[string]int          // sid -> completed uploads in the CURRENT submission
	renderJobs map[string][]*RenderJob // sid -> own render jobs (newest first)
	baseURL    string
	token      string
	db         *sql.DB
	jobStore   *RenderJobStore
}

type batchClient struct {
	client    *Client
	batchID   string
	projectID string
}

func NewSessionStore(baseURL, token string, dbs ...*sql.DB) *SessionStore {
	var db *sql.DB
	if len(dbs) > 0 {
		db = dbs[0]
	}
	return &SessionStore{
		clients:    make(map[string]*Client),
		batchIDs:   make(map[string]*batchClient),
		projects:   make(map[string]*batchClient),
		assetCount: make(map[string]int),
		renderJobs: make(map[string][]*RenderJob),
		baseURL:    baseURL,
		token:      token,
		db:         db,
		jobStore:   NewRenderJobStore(db),
	}
}

type persistedBatchSession struct {
	BatchID           string
	ProjectID         string
	UpstreamSessionID string
}

func (s *SessionStore) persistBatchSession(sessionID, batchID, projectID, upstreamID string) error {
	if s.db == nil {
		return nil
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.Exec(`INSERT INTO mcp_batch_sessions
(session_id,batch_id,project_id,upstream_session_id,created_at,updated_at)
VALUES(?,?,?,?,?,?)
ON CONFLICT(session_id,batch_id) DO UPDATE SET project_id=excluded.project_id,
upstream_session_id=excluded.upstream_session_id,updated_at=excluded.updated_at`,
		sessionID, batchID, projectID, upstreamID, now, now)
	return err
}

func (s *SessionStore) deletePersistedBatchSession(sessionID, batchID, projectID string) error {
	if s.db == nil {
		return nil
	}
	_, err := s.db.Exec(`DELETE FROM mcp_batch_sessions
WHERE session_id=? AND (batch_id=? OR project_id=?)`, sessionID, batchID, projectID)
	return err
}

func (s *SessionStore) findPersistedBatchSession(sessionID, batchID, projectID string) (*persistedBatchSession, error) {
	if s.db == nil {
		return nil, nil
	}
	row := s.db.QueryRow(`SELECT batch_id,project_id,upstream_session_id
FROM mcp_batch_sessions WHERE session_id=? AND (batch_id=? OR project_id=?) LIMIT 1`,
		sessionID, batchID, projectID)
	var out persistedBatchSession
	if err := row.Scan(&out.BatchID, &out.ProjectID, &out.UpstreamSessionID); err != nil {
		if err == sql.ErrNoRows {
			return nil, nil
		}
		return nil, err
	}
	return &out, nil
}

// RecordJob prepends a render job to the session's queue (newest first) and
// caps the retained history so memory stays bounded per session.
func (s *SessionStore) RecordJob(sessionID string, job RenderJob) {
	if s.jobStore != nil {
		if err := s.jobStore.Record(sessionID, job); err == nil {
			return
		}
	}
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
	if s.jobStore != nil {
		if jobs, err := s.jobStore.List(sessionID); err == nil {
			return jobs
		}
	}
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
	if s.jobStore != nil {
		if err := s.jobStore.UpdateStatus(sessionID, jobID, status); err == nil {
			return
		}
	}
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

func batchScope(sessionID, value string) string {
	return sessionID + "\x00" + value
}

func (s *SessionStore) findBatchLocked(sessionID, batchID, projectID string) (*batchClient, error) {
	var byID, byProject *batchClient
	if batchID != "" {
		byID = s.batchIDs[batchScope(sessionID, batchID)]
	}
	if projectID != "" {
		byProject = s.projects[batchScope(sessionID, projectID)]
	}
	if byID != nil && byProject != nil && byID != byProject {
		return nil, fmt.Errorf("batch_id %q and project_id %q belong to different image batches", batchID, projectID)
	}
	if byID != nil {
		return byID, nil
	}
	return byProject, nil
}

func (s *SessionStore) findBatch(sessionID, batchID, projectID string) (*batchClient, error) {
	s.mu.RLock()
	defer s.mu.RUnlock()
	return s.findBatchLocked(sessionID, batchID, projectID)
}

// CreateBatch initializes and registers an MCP client dedicated to one image
// batch. The handshake completes before the routing entries are published.
func (s *SessionStore) CreateBatch(sessionID, batchID, projectID string) error {
	if sessionID == "" || batchID == "" || projectID == "" {
		return fmt.Errorf("mcp batch session requires session, batch, and project ids")
	}

	s.mu.RLock()
	if existing, err := s.findBatchLocked(sessionID, batchID, projectID); err != nil {
		s.mu.RUnlock()
		return err
	} else if existing != nil {
		s.mu.RUnlock()
		if existing.batchID == batchID && existing.projectID == projectID {
			return nil
		}
		return fmt.Errorf("image batch identity is already registered")
	}
	s.mu.RUnlock()

	c := NewClient(s.baseURL, s.token)
	if err := c.Initialize(); err != nil {
		return fmt.Errorf("mcp initialize image batch %q: %w", batchID, err)
	}
	entry := &batchClient{client: c, batchID: batchID, projectID: projectID}
	s.mu.Lock()
	if existing, err := s.findBatchLocked(sessionID, batchID, projectID); err != nil {
		s.mu.Unlock()
		return err
	} else if existing != nil {
		s.mu.Unlock()
		return nil
	}
	s.batchIDs[batchScope(sessionID, batchID)] = entry
	s.projects[batchScope(sessionID, projectID)] = entry
	s.mu.Unlock()
	if err := s.persistBatchSession(sessionID, batchID, projectID, c.SessionID()); err != nil {
		s.DropBatch(sessionID, batchID, projectID)
		return fmt.Errorf("persist mcp batch session %q: %w", batchID, err)
	}
	return nil
}

func (s *SessionStore) removeBatchLocked(sessionID string, entry *batchClient) {
	if entry == nil {
		return
	}
	delete(s.batchIDs, batchScope(sessionID, entry.batchID))
	delete(s.projects, batchScope(sessionID, entry.projectID))
}

// DropBatch removes an in-memory routing entry when durable batch creation
// fails after the MCP handshake. It leaves the legacy user client untouched.
func (s *SessionStore) DropBatch(sessionID, batchID, projectID string) {
	s.mu.Lock()
	entry, _ := s.findBatchLocked(sessionID, batchID, projectID)
	s.removeBatchLocked(sessionID, entry)
	s.mu.Unlock()
	_ = s.deletePersistedBatchSession(sessionID, batchID, projectID)
}

// persistUserSession / findPersistedUserSession / deletePersistedUserSession
// keep a durable record of each ff_sid's upstream Mcp-Session-Id so another
// BFF instance (or a restarted one) can resume the SAME upstream session.

func (s *SessionStore) persistUserSession(sessionID, upstreamID string) error {
	if s.db == nil {
		return nil
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	_, err := s.db.Exec(`INSERT INTO mcp_user_sessions (session_id, upstream_session_id, created_at, updated_at)
VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET upstream_session_id=excluded.upstream_session_id, updated_at=excluded.updated_at`,
		sessionID, upstreamID, now, now)
	return err
}

func (s *SessionStore) deletePersistedUserSession(sessionID string) error {
	if s.db == nil {
		return nil
	}
	_, err := s.db.Exec(`DELETE FROM mcp_user_sessions WHERE session_id=?`, sessionID)
	return err
}

func (s *SessionStore) findPersistedUserSession(sessionID string) (string, error) {
	if s.db == nil {
		return "", nil
	}
	row := s.db.QueryRow(`SELECT upstream_session_id FROM mcp_user_sessions WHERE session_id=? LIMIT 1`, sessionID)
	var up string
	if err := row.Scan(&up); err != nil {
		if err == sql.ErrNoRows {
			return "", nil
		}
		return "", err
	}
	return up, nil
}

// getOrCreate returns the long-lived MCP client for a BFF session. On a cold
// start (no in-memory client) it first tries to RESUME a previously persisted
// upstream session id, so the same ff_sid keeps one upstream Mcp-Session-Id
// across BFF instances/restarts. If none is persisted, it opens a fresh
// upstream session via initialize and persists the new id.
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
	// Cold start: resume the durable upstream session if we have one. The
	// upstream accepts a valid existing Mcp-Session-Id on a fresh connection,
	// so we skip initialize and just pin the id.
	if up, err := s.findPersistedUserSession(sessionID); err == nil && up != "" {
		c := NewClient(s.baseURL, s.token)
		c.SetSessionID(up)
		s.clients[sessionID] = c
		log.Printf("[mcp-route] user_session_resumed sid_hash=%s upstream_sid_hash=%s", shortHash(sessionID), shortHash(up))
		return c, nil
	}
	c = NewClient(s.baseURL, s.token)
	if err := c.Initialize(); err != nil {
		return nil, fmt.Errorf("mcp initialize: %w", err)
	}
	s.clients[sessionID] = c
	_ = s.persistUserSession(sessionID, c.SessionID())
	return c, nil
}

func (s *SessionStore) drop(sessionID string) {
	s.mu.Lock()
	delete(s.clients, sessionID)
	s.mu.Unlock()
}

func (s *SessionStore) recreateBatch(sessionID, batchID, projectID string, expected *batchClient) (*batchClient, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	current, err := s.findBatchLocked(sessionID, batchID, projectID)
	if err != nil {
		return nil, err
	}
	if current != nil && current != expected {
		return current, nil
	}
	s.removeBatchLocked(sessionID, expected)

	c := NewClient(s.baseURL, s.token)
	if err := c.Initialize(); err != nil {
		return nil, fmt.Errorf("mcp reinitialize image batch %q: %w", batchID, err)
	}
	entry := &batchClient{client: c, batchID: batchID, projectID: projectID}
	s.batchIDs[batchScope(sessionID, batchID)] = entry
	s.projects[batchScope(sessionID, projectID)] = entry
	if err := s.persistBatchSession(sessionID, batchID, projectID, c.SessionID()); err != nil {
		return nil, fmt.Errorf("persist mcp batch session %q: %w", batchID, err)
	}
	return entry, nil
}

// CallBatch runs a tool against the client dedicated to one image batch. It
// retries once after an upstream session-expired response, just like the
// legacy user-level Call path.
func (s *SessionStore) CallBatch(sessionID, batchID, projectID, tool string, args map[string]interface{}) (map[string]interface{}, error) {
	entry, err := s.findBatch(sessionID, batchID, projectID)
	if err != nil {
		log.Printf("[mcp-route] batch_lookup_failed tool=%s batch_id=%s project_id=%s sid_hash=%s err=%v", tool, batchID, projectID, shortHash(sessionID), err)
		return nil, err
	}
	if entry == nil {
		persisted, lookupErr := s.findPersistedBatchSession(sessionID, batchID, projectID)
		if lookupErr != nil {
			return nil, lookupErr
		}
		if persisted != nil {
			if err := s.CreateBatch(sessionID, persisted.BatchID, persisted.ProjectID); err != nil {
				return nil, err
			}
			entry, err = s.findBatch(sessionID, persisted.BatchID, persisted.ProjectID)
			if err != nil {
				return nil, err
			}
		}
		if entry == nil {
			log.Printf("[mcp-route] batch_not_found tool=%s batch_id=%s project_id=%s sid_hash=%s", tool, batchID, projectID, shortHash(sessionID))
			return nil, fmt.Errorf("mcp batch session not found for batch %q", batchID)
		}
	}
	log.Printf("[mcp-route] batch_call tool=%s batch_id=%s project_id=%s sid_hash=%s upstream_sid_hash=%s", tool, batchID, projectID, shortHash(sessionID), shortHash(entry.client.SessionID()))
	res, err := entry.client.CallTool(tool, args)
	_ = s.persistBatchSession(sessionID, batchID, projectID, entry.client.SessionID())
	if err != nil {
		log.Printf("[mcp-route] batch_call_failed tool=%s batch_id=%s project_id=%s sid_hash=%s err=%v", tool, batchID, projectID, shortHash(sessionID), err)
		return nil, err
	}
	if !IsSessionError(res) {
		return res, nil
	}
	entry, err = s.recreateBatch(sessionID, batchID, projectID, entry)
	if err != nil {
		return nil, err
	}
	res, err = entry.client.CallTool(tool, args)
	_ = s.persistBatchSession(sessionID, batchID, projectID, entry.client.SessionID())
	return res, err
}

// Call runs a tool call on the user's MCP client, transparently re-initializing
// once if the upstream reports a stale/expired session. If the request carries
// a project_id belonging to an image batch, it is routed to that batch's
// dedicated client; calls for all other projects retain the legacy user-level
// behavior.
func (s *SessionStore) Call(sessionID, tool string, args map[string]interface{}) (map[string]interface{}, error) {
	var batchID, projectID string
	if args != nil {
		batchID, _ = args["batch_id"].(string)
		projectID, _ = args["project_id"].(string)
	}
	if batchID != "" || projectID != "" {
		if entry, err := s.findBatch(sessionID, batchID, projectID); err != nil {
			return nil, err
		} else if entry != nil {
			return s.CallBatch(sessionID, batchID, projectID, tool, args)
		}
		persisted, err := s.findPersistedBatchSession(sessionID, batchID, projectID)
		if err != nil {
			return nil, fmt.Errorf("lookup persisted mcp batch session: %w", err)
		}
		if persisted != nil {
			if err := s.CreateBatch(sessionID, persisted.BatchID, persisted.ProjectID); err != nil {
				return nil, err
			}
			return s.CallBatch(sessionID, persisted.BatchID, persisted.ProjectID, tool, args)
		}
	}

	c, err := s.getOrCreate(sessionID)
	if err != nil {
		return nil, err
	}
	res, err := c.CallTool(tool, args)
	if err != nil {
		return nil, err
	}
	// Keep the durable upstream session id current (the upstream rotates it on
	// every response, so re-persist after each successful call).
	if !IsSessionError(res) {
		_ = s.persistUserSession(sessionID, c.SessionID())
		return res, nil
	}
	// Upstream rejected the session: drop the in-memory client and the durable
	// record, then open a brand-new upstream session and retry once.
	s.drop(sessionID)
	_ = s.deletePersistedUserSession(sessionID)
	c, err = s.getOrCreate(sessionID)
	if err != nil {
		return nil, err
	}
	return c.CallTool(tool, args)
}
