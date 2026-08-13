package composition

import (
	"sync"
	"time"
)

// Composition is a user-authored Remotion composition (TSX source) plus light
// metadata. It is the unit the FrameFlow editor saves and the renderer (once
// enabled) consumes.
type Composition struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Code      string    `json:"code"`
	SessionID string    `json:"-"` // owner session; not exported
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Store keeps compositions in process memory, keyed by session then id. This is
// deliberately simple: for production, swap this for a DB (each session maps to
// a user). Multi-instance deployments must share this state (e.g. Redis).
type Store struct {
	mu     sync.RWMutex
	bySess map[string]map[string]*Composition
	seq    uint64
}

func NewStore() *Store {
	return &Store{bySess: make(map[string]map[string]*Composition)}
}

func (s *Store) nextID() string {
	s.seq++
	return time.Now().Format("20060102150405") + "-" + itoa(s.seq)
}

func itoa(n uint64) string {
	if n == 0 {
		return "0"
	}
	var buf [20]byte
	i := len(buf)
	for n > 0 {
		i--
		buf[i] = byte('0' + n%10)
		n /= 10
	}
	return string(buf[i:])
}

// Save creates or updates a composition owned by the given session.
func (s *Store) Save(sessionID, name, code string) *Composition {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.bySess[sessionID] == nil {
		s.bySess[sessionID] = make(map[string]*Composition)
	}
	now := time.Now()
	for _, c := range s.bySess[sessionID] {
		if c.Name == name {
			c.Code = code
			c.UpdatedAt = now
			return c
		}
	}
	c := &Composition{
		ID:        s.nextID(),
		Name:      name,
		Code:      code,
		SessionID: sessionID,
		CreatedAt: now,
		UpdatedAt: now,
	}
	s.bySess[sessionID][c.ID] = c
	return c
}

// List returns all compositions owned by the session (newest first).
func (s *Store) List(sessionID string) []*Composition {
	s.mu.RLock()
	defer s.mu.RUnlock()
	m := s.bySess[sessionID]
	out := make([]*Composition, 0, len(m))
	for _, c := range m {
		out = append(out, c)
	}
	// simple newest-first by CreatedAt
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j].CreatedAt.After(out[i].CreatedAt) {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}

// Get returns a composition by id if owned by the session.
func (s *Store) Get(sessionID, id string) *Composition {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if m := s.bySess[sessionID]; m != nil {
		return m[id]
	}
	return nil
}
