package script

import (
	"crypto/sha256"
	"encoding/hex"
	"strings"
	"sync"
	"time"
)

// Script is a user-authored video-generation script (e.g. a Remotion source
// produced via DeepSeek) plus light metadata. The FrameFlow "定义视频脚本"
// surface saves these server-side so the backend can consume them.
type Script struct {
	ID        string    `json:"id"`
	Name      string    `json:"name"`
	Key       string    `json:"key"`
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

// Save creates a new script owned by the session. The stored key combines the
// script name, a per-user tag (derived from the session) and a timestamp, so
// duplicate display names never collide — every save is a distinct, uniquely
// identified script (this is the anti-duplicate-naming safeguard).
func (s *Store) Save(sessionID, name, content string) *Script {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.bySess[sessionID] == nil {
		s.bySess[sessionID] = make(map[string]*Script)
	}
	s.seq++
	now := time.Now()
	sc := &Script{
		ID:        now.Format("20060102150405") + "-" + itoa(s.seq),
		Key:       buildKey(name, sessionID, now, s.seq),
		Name:      name,
		Content:   content,
		SessionID: sessionID,
		CreatedAt: now,
		UpdatedAt: now,
	}
	s.bySess[sessionID][sc.ID] = sc
	return sc
}

// List returns scripts owned by the session (newest first). When limit > 0 the
// result is paginated (offset is 0-based); total is always the unfiltered count
// so the caller can render pager controls.
func (s *Store) List(sessionID string, limit, offset int) ([]*Script, int) {
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
	total := len(out)
	if limit > 0 {
		if offset < 0 {
			offset = 0
		}
		if offset > total {
			offset = total
		}
		end := offset + limit
		if end > total {
			end = total
		}
		out = out[offset:end]
	}
	return out, total
}

// userTag derives a stable, short per-user identifier from the session id.
// Scripts are scoped per session; this tag makes duplicate display names unique
// across users without leaking the raw session id.
func userTag(sessionID string) string {
	sum := sha256.Sum256([]byte(sessionID))
	return hex.EncodeToString(sum[:])[:8]
}

// sanitizeName keeps alphanumerics and CJK, replacing everything else with '-'
// so the key stays readable and reasonably URL/file-safe.
func sanitizeName(name string) string {
	var b strings.Builder
	for _, r := range name {
		if (r >= 'a' && r <= 'z') || (r >= 'A' && r <= 'Z') ||
			(r >= '0' && r <= '9') || (r >= 0x4e00 && r <= 0x9fff) {
			b.WriteRune(r)
		} else {
			b.WriteByte('-')
		}
	}
	return strings.Trim(b.String(), "-")
}

// buildKey produces a globally-unique script key: <name>-<userTag>-<timestamp>-<seq>.
func buildKey(name, sessionID string, t time.Time, seq uint64) string {
	return sanitizeName(name) + "-" + userTag(sessionID) + "-" + t.Format("20060102-150405") + "-" + itoa(seq)
}
