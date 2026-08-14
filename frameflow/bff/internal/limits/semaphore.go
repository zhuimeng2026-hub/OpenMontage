package limits

import (
	"context"
	"crypto/rand"
	"database/sql"
	"encoding/hex"
	"fmt"
	"sync"
	"time"
)

type SemaphoreConfig struct {
	GlobalCapacity  int
	PerUserCapacity int
	LeaseTTL        time.Duration
}

// Semaphore uses SQLite leases when a DB is supplied. This makes capacity
// shared by every BFF process opening the same state database.
type Semaphore struct {
	mu          sync.Mutex
	db          *sql.DB
	config      SemaphoreConfig
	owner       string
	globalInUse int
	userInUse   map[string]int
}

func NewSemaphore(config SemaphoreConfig) *Semaphore {
	if config.GlobalCapacity < 0 || config.PerUserCapacity < 0 {
		panic("limits: semaphore capacities must not be negative")
	}
	return &Semaphore{config: config, userInUse: make(map[string]int), owner: newOwner()}
}

func NewGlobalSemaphore(capacity int) *Semaphore {
	return NewSemaphore(SemaphoreConfig{GlobalCapacity: capacity})
}

func NewPerUserSemaphore(capacity int) *Semaphore {
	return NewSemaphore(SemaphoreConfig{PerUserCapacity: capacity})
}

func NewSQLiteSemaphore(db *sql.DB, config SemaphoreConfig) (*Semaphore, error) {
	s := NewSemaphore(config)
	s.db = db
	if err := s.CleanupExpired(context.Background()); err != nil {
		return nil, fmt.Errorf("cleanup render leases: %w", err)
	}
	return s, nil
}

func newOwner() string {
	b := make([]byte, 12)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("pid-%d", time.Now().UnixNano())
	}
	return hex.EncodeToString(b)
}

func (s *Semaphore) Config() SemaphoreConfig    { s.mu.Lock(); defer s.mu.Unlock(); return s.config }
func (s *Semaphore) Acquire(userID string) bool { return s.TryAcquire(userID) }

// TryAcquire is retained for local callers. Batch handlers use the durable API.
func (s *Semaphore) TryAcquire(userID string) bool {
	if s.db != nil {
		return s.tryAcquire(context.Background(), userID, "legacy-"+userID+"-"+fmt.Sprint(time.Now().UnixNano()))
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.acquireMemoryLocked(userID)
}

func (s *Semaphore) TryAcquireBatch(userID, batchID string) bool {
	if s.db == nil {
		return s.TryAcquire(userID)
	}
	return s.tryAcquire(context.Background(), userID, batchID)
}

func (s *Semaphore) tryAcquire(ctx context.Context, userID, batchID string) bool {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.config.GlobalCapacity == 0 && s.config.PerUserCapacity == 0 {
		return true
	}
	now := time.Now().UnixMilli()
	ttl := s.config.LeaseTTL
	if ttl <= 0 {
		ttl = 30 * time.Minute
	}
	expires := now + ttl.Milliseconds()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return false
	}
	defer tx.Rollback()
	// The delete takes SQLite's writer lock before capacity counts are read.
	if _, err = tx.ExecContext(ctx, `DELETE FROM image_batch_render_leases WHERE expires_at <= ?`, now); err != nil {
		return false
	}
	result, err := tx.ExecContext(ctx, `
INSERT INTO image_batch_render_leases(batch_id,user_id,owner_id,expires_at,created_at)
SELECT ?,?,?,?,?
WHERE (? <= 0 OR (SELECT COUNT(*) FROM image_batch_render_leases) < ?)
  AND (? <= 0 OR (SELECT COUNT(*) FROM image_batch_render_leases WHERE user_id=?) < ?)
ON CONFLICT(batch_id) DO UPDATE SET user_id=excluded.user_id, owner_id=excluded.owner_id,
  expires_at=excluded.expires_at, created_at=excluded.created_at
  WHERE image_batch_render_leases.expires_at <= excluded.created_at`,
		batchID, userID, s.owner, expires, now,
		s.config.GlobalCapacity, s.config.GlobalCapacity,
		s.config.PerUserCapacity, userID, s.config.PerUserCapacity)
	if err != nil {
		return false
	}
	rows, err := result.RowsAffected()
	if err != nil || rows != 1 {
		return false
	}
	if err = tx.Commit(); err != nil {
		return false
	}
	return true
}

func (s *Semaphore) Release(userID string) {
	if s.db != nil {
		return
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	s.releaseMemoryLocked(userID)
}

func (s *Semaphore) ReleaseBatch(batchID string) {
	if s.db == nil {
		return
	}
	_, _ = s.db.Exec(`DELETE FROM image_batch_render_leases WHERE batch_id=?`, batchID)
}

func (s *Semaphore) CleanupExpired(ctx context.Context) error {
	if s.db == nil {
		return nil
	}
	_, err := s.db.ExecContext(ctx, `DELETE FROM image_batch_render_leases WHERE expires_at <= ?`, time.Now().UnixMilli())
	return err
}

func (s *Semaphore) acquireMemoryLocked(userID string) bool {
	u := s.userInUse[userID]
	if s.config.GlobalCapacity > 0 && s.globalInUse >= s.config.GlobalCapacity {
		return false
	}
	if s.config.PerUserCapacity > 0 && u >= s.config.PerUserCapacity {
		return false
	}
	s.globalInUse++
	s.userInUse[userID] = u + 1
	return true
}
func (s *Semaphore) releaseMemoryLocked(userID string) {
	u := s.userInUse[userID]
	if u == 0 {
		return
	}
	if s.globalInUse > 0 {
		s.globalInUse--
	}
	if u == 1 {
		delete(s.userInUse, userID)
	} else {
		s.userInUse[userID] = u - 1
	}
}
func (s *Semaphore) GlobalInUse() int {
	if s.db != nil {
		var n int
		_ = s.db.QueryRow(`SELECT COUNT(*) FROM image_batch_render_leases WHERE expires_at > ?`, time.Now().UnixMilli()).Scan(&n)
		return n
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.globalInUse
}
func (s *Semaphore) UserInUse(userID string) int {
	if s.db != nil {
		var n int
		_ = s.db.QueryRow(`SELECT COUNT(*) FROM image_batch_render_leases WHERE user_id=? AND expires_at > ?`, userID, time.Now().UnixMilli()).Scan(&n)
		return n
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.userInUse[userID]
}
