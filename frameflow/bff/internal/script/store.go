package script

import (
	"sync"
	"time"
)

// Script is a user-authored video-generation script (e.g. a Remotion source
// produced via DeepSeek) plus light metadata. The FrameFlow "定义视频脚本"
// surface saves these server-side so the backend can consume them.
type Script struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Content   string    `json:"content"`
	SessionID string    `json:"-"` // owner session; not exported
	CreatedAt time.Time `json:"created_at"`
	UpdatedAt time.Time `json:"updated_at"`
}

// Store keeps scripts in process memory, keyed by session then id. This mirrors
// the composition store: simple and in-memory, swap for a DB per user in
// production. Multi-instance deploys must share this state.
type Store struct {
	mu     sync.RWMutex
	bySess map[string]map[string]*Script
	seq    uint64
}

func NewStore() *Store {
	return &Store{bySess: make(map[string]map[string]*Script)}
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

// Save creates or updates a script owned by the given session (matched by name).
func (s *Store) Save(sessionID, name, content string) *Script {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.bySess[sessionID] == nil {
		s.bySess[sessionID] = make(map[string]*Script)
	}
	now := time.Now()
	for _, sc := range s.bySess[sessionID] {
		if sc.Name == name {
			sc.Content = content
			sc.UpdatedAt = now
			return sc
		}
	}
	sc := &Script{
		ID:        s.nextID(),
		Name:      name,
		Content:   content,
		SessionID: sessionID,
		CreatedAt: now,
		UpdatedAt: now,
	}
	s.bySess[sessionID][sc.ID] = sc
	return sc
}

// List returns all scripts owned by the session (newest first).
func (s *Store) List(sessionID string) []*Script {
	s.mu.RLock()
	defer s.mu.RUnlock()
	m := s.bySess[sessionID]
	out := make([]*Script, 0, len(m))
	for _, sc := range m {
		out = append(out, sc)
	}
	for i := 0; i < len(out); i++ {
		for j := i + 1; j < len(out); j++ {
			if out[j].CreatedAt.After(out[i].CreatedAt) {
				out[i], out[j] = out[j], out[i]
			}
		}
	}
	return out
}
