package limits

import "testing"

func TestForTierFallsBackToFree(t *testing.T) {
	l := ForTier("does-not-exist")
	if l.MaxFilesPerSubmission != 10 || l.MaxRenderTasksPerDay != 10 || l.MaxConcurrentTasks != 10 {
		t.Fatalf("unknown tier should fall back to free (10/10/10), got %+v", l)
	}
	if ForTier(TierPro).MaxRenderTasksPerDay != 200 {
		t.Fatal("pro tier should allow 200 tasks/day")
	}
}

func TestAcquireAndInspect(t *testing.T) {
	u := NewUsage()
	snap, ok := u.Acquire("u1", TierFree, ForTier(TierFree))
	if !ok {
		t.Fatal("first acquire should succeed")
	}
	if snap.DailyUsed != 1 || snap.ConcurrentNow != 1 {
		t.Fatalf("expected daily=1 concurrent=1, got daily=%d concurrent=%d", snap.DailyUsed, snap.ConcurrentNow)
	}
	ins := u.Inspect("u1", TierFree, ForTier(TierFree))
	if ins.DailyUsed != 1 || ins.ConcurrentNow != 1 {
		t.Fatalf("inspect mismatch: daily=%d concurrent=%d", ins.DailyUsed, ins.ConcurrentNow)
	}
	u.Release("u1")
	ins2 := u.Inspect("u1", TierFree, ForTier(TierFree))
	if ins2.ConcurrentNow != 0 {
		t.Fatalf("release should free the concurrent slot, got %d", ins2.ConcurrentNow)
	}
	if ins2.DailyUsed != 1 {
		t.Fatalf("daily count is permanent for the day, got %d", ins2.DailyUsed)
	}
}

func TestDailyExhaustion(t *testing.T) {
	u := NewUsage()
	lim := ForTier(TierFree)
	for i := 0; i < 10; i++ {
		if _, ok := u.Acquire("u2", TierFree, lim); !ok {
			t.Fatalf("acquire #%d should succeed (free allows 10/day)", i+1)
		}
	}
	snap, ok := u.Acquire("u2", TierFree, lim)
	if ok {
		t.Fatal("11th daily acquire must be rejected")
	}
	if snap.DailyRemaining != 0 {
		t.Fatalf("daily_remaining should be 0, got %d", snap.DailyRemaining)
	}
	if snap.DailyUsed != 10 {
		t.Fatalf("daily_used should be 10, got %d", snap.DailyUsed)
	}
}

func TestConcurrentExhaustion(t *testing.T) {
	u := NewUsage()
	// Use pro tier (50 concurrent / 200 daily) so the concurrent cap can be
	// exercised independently of the daily cap.
	lim := ForTier(TierPro)
	for i := 0; i < 50; i++ {
		if _, ok := u.Acquire("u3", TierPro, lim); !ok {
			t.Fatalf("concurrent acquire #%d should succeed", i+1)
		}
	}
	snap, ok := u.Acquire("u3", TierPro, lim)
	if ok {
		t.Fatal("51st concurrent acquire must be rejected")
	}
	if snap.ConcurrentRemaining != 0 {
		t.Fatalf("concurrent_remaining should be 0, got %d", snap.ConcurrentRemaining)
	}
	// releasing one frees a concurrent slot
	u.Release("u3")
	if _, ok := u.Acquire("u3", TierPro, lim); !ok {
		t.Fatal("after releasing one slot, acquire should succeed again")
	}
}

func TestResolverOverride(t *testing.T) {
	r := NewResolver("free", `{"abc":"pro"}`)
	if r.Resolve("abc") != TierPro {
		t.Fatal("override should map abc -> pro")
	}
	if r.Resolve("other") != TierFree {
		t.Fatal("unmapped user should get the default tier")
	}
}
