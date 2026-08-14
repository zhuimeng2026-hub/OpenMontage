package limits

import (
	"os"
	"sync"
	"testing"
	"time"

	"frameflow-bff/internal/state"
)

func TestSemaphoreEnforcesGlobalCapacity(t *testing.T) {
	s := NewGlobalSemaphore(2)

	if !s.Acquire("alice") || !s.Acquire("bob") {
		t.Fatal("the first two global acquires should succeed")
	}
	if s.Acquire("carol") {
		t.Fatal("the third acquire should be rejected at the global limit")
	}
	if got := s.GlobalInUse(); got != 2 {
		t.Fatalf("global in-use count = %d, want 2", got)
	}

	s.Release("alice")
	if !s.Acquire("carol") {
		t.Fatal("a released global permit should be reusable")
	}
}

func TestSemaphoreEnforcesPerUserCapacity(t *testing.T) {
	s := NewPerUserSemaphore(1)

	if !s.Acquire("alice") {
		t.Fatal("alice's first acquire should succeed")
	}
	if s.Acquire("alice") {
		t.Fatal("alice's second acquire should be rejected")
	}
	if !s.Acquire("bob") {
		t.Fatal("a different user should have an independent permit")
	}
	if got := s.UserInUse("alice"); got != 1 {
		t.Fatalf("alice in-use count = %d, want 1", got)
	}

	s.Release("alice")
	if !s.TryAcquire("alice") {
		t.Fatal("a released user permit should be reusable")
	}
}

func TestSemaphoreChecksGlobalAndPerUserAtomically(t *testing.T) {
	s := NewSemaphore(SemaphoreConfig{GlobalCapacity: 2, PerUserCapacity: 2})

	if !s.Acquire("alice") || !s.Acquire("alice") {
		t.Fatal("alice should consume both of her permits")
	}
	if s.Acquire("bob") {
		t.Fatal("bob should be rejected by the global limit")
	}
	if got := s.UserInUse("bob"); got != 0 {
		t.Fatalf("failed acquire must not reserve a user permit; got %d", got)
	}

	s.Release("alice")
	if !s.Acquire("bob") {
		t.Fatal("bob should acquire after a global permit is released")
	}
}

func TestSemaphoreReleaseIsSafeWhenUnbalanced(t *testing.T) {
	s := NewSemaphore(SemaphoreConfig{GlobalCapacity: 1, PerUserCapacity: 1})

	s.Release("unknown")
	if got := s.GlobalInUse(); got != 0 {
		t.Fatalf("unbalanced release changed global count to %d", got)
	}
	if !s.Acquire("alice") {
		t.Fatal("acquire should succeed after an unbalanced release")
	}
	s.Release("alice")
	s.Release("alice")
	if got := s.GlobalInUse(); got != 0 || s.UserInUse("alice") != 0 {
		t.Fatalf("unbalanced release left counts global=%d user=%d", got, s.UserInUse("alice"))
	}
}

func TestSemaphoreCapacityConfiguration(t *testing.T) {
	config := SemaphoreConfig{GlobalCapacity: 3, PerUserCapacity: 2}
	s := NewSemaphore(config)
	if got := s.Config(); got != config {
		t.Fatalf("config = %+v, want %+v", got, config)
	}
}

func TestSemaphoreIsSafeForConcurrentAcquireRelease(t *testing.T) {
	s := NewSemaphore(SemaphoreConfig{GlobalCapacity: 8, PerUserCapacity: 2})
	const workers = 64

	var wg sync.WaitGroup
	for i := 0; i < workers; i++ {
		userID := "user-a"
		if i%2 == 1 {
			userID = "user-b"
		}
		wg.Add(1)
		go func() {
			defer wg.Done()
			if s.Acquire(userID) {
				s.Release(userID)
			}
		}()
	}
	wg.Wait()

	if got := s.GlobalInUse(); got != 0 {
		t.Fatalf("global permits leaked after concurrent use: %d", got)
	}
	if got := s.UserInUse("user-a"); got != 0 {
		t.Fatalf("user-a permits leaked after concurrent use: %d", got)
	}
	if got := s.UserInUse("user-b"); got != 0 {
		t.Fatalf("user-b permits leaked after concurrent use: %d", got)
	}
}

func TestNewSemaphoreRejectsNegativeCapacity(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("negative capacity should panic")
		}
	}()
	NewSemaphore(SemaphoreConfig{GlobalCapacity: -1})
}

func TestSQLiteSemaphoreSharesCapacityAcrossInstances(t *testing.T) {
	f, err := os.CreateTemp("", "frameflow-limit-*.db")
	if err != nil {
		t.Fatal(err)
	}
	path := f.Name()
	f.Close()
	defer os.Remove(path)
	db, err := state.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	cfg := SemaphoreConfig{GlobalCapacity: 1, PerUserCapacity: 1, LeaseTTL: time.Minute}
	a, err := NewSQLiteSemaphore(db, cfg)
	if err != nil {
		t.Fatal(err)
	}
	b, err := NewSQLiteSemaphore(db, cfg)
	if err != nil {
		t.Fatal(err)
	}
	if !a.TryAcquireBatch("alice", "batch-a") {
		t.Fatal("first process should claim")
	}
	if b.TryAcquireBatch("bob", "batch-b") {
		t.Fatal("second process must see the shared global limit")
	}
	a.ReleaseBatch("batch-a")
	if !b.TryAcquireBatch("bob", "batch-b") {
		t.Fatal("released lease should be reusable")
	}
}

func TestSQLiteSemaphoreCleansExpiredLease(t *testing.T) {
	f, err := os.CreateTemp("", "frameflow-limit-*.db")
	if err != nil {
		t.Fatal(err)
	}
	path := f.Name()
	f.Close()
	defer os.Remove(path)
	db, err := state.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer db.Close()
	if _, err := db.Exec(`INSERT INTO image_batch_render_leases(batch_id,user_id,owner_id,expires_at,created_at) VALUES(?,?,?,?,?)`, "stale", "alice", "dead", time.Now().Add(-time.Minute).UnixMilli(), time.Now().Add(-2*time.Minute).UnixMilli()); err != nil {
		t.Fatal(err)
	}
	if _, err := NewSQLiteSemaphore(db, SemaphoreConfig{GlobalCapacity: 1, LeaseTTL: time.Minute}); err != nil {
		t.Fatal(err)
	}
	var count int
	if err := db.QueryRow(`SELECT COUNT(*) FROM image_batch_render_leases`).Scan(&count); err != nil {
		t.Fatal(err)
	}
	if count != 0 {
		t.Fatalf("expired leases remaining: %d", count)
	}
}
