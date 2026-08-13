// Package limits models per-user, tier-based quotas for the batch-render
// surface. A "render task" is ONE batch-render submission (one job across one
// or more scenarios). Quotas are deliberately expressed in three independent
// dimensions so each tier can be tuned granularly:
//
//   - MaxFilesPerSubmission : total image files across one submission (job)
//   - MaxRenderTasksPerDay  : batch jobs a user may START per calendar day
//   - MaxConcurrentTasks    : batch jobs a single user may RUN at once
//
// Tiers are a registry: add a higher tier (e.g. "enterprise") in one place and
// it is live immediately. User->tier mapping is a Resolver seam so a future
// user store / WeChat account can drive tiers without touching this package.
package limits

import (
	"encoding/json"
	"sync"
	"time"
)

// Tier is a user level. Add new tiers to the Registry below.
type Tier string

const (
	TierFree Tier = "free"
	TierPro  Tier = "pro"
)

// Limits caps a single user's batch-render usage at one tier.
type Limits struct {
	MaxFilesPerSubmission int `json:"max_files_per_submission"`
	MaxRenderTasksPerDay  int `json:"max_render_tasks_per_day"`
	MaxConcurrentTasks    int `json:"max_concurrent_tasks"`
}

// Registry maps a tier to its Limits. To introduce a higher user level, add an
// entry here (e.g. TierEnterprise: {200, 1000, 100}).
var Registry = map[Tier]Limits{
	TierFree: {MaxFilesPerSubmission: 10, MaxRenderTasksPerDay: 10, MaxConcurrentTasks: 10},
	TierPro:  {MaxFilesPerSubmission: 100, MaxRenderTasksPerDay: 200, MaxConcurrentTasks: 50},
}

// ForTier returns the limits for a tier, falling back to free when unknown.
func ForTier(t Tier) Limits {
	if l, ok := Registry[t]; ok {
		return l
	}
	return Registry[TierFree]
}

// Resolver maps a user id to a tier. Default impl reads a fixed default tier
// plus an optional per-user override map; swap in a user-DB / WeChat resolver
// to drive tiers from real accounts later.
type Resolver interface {
	Resolve(userID string) Tier
}

type configResolver struct {
	defaultTier Tier
	overrides   map[string]Tier
}

// NewResolver builds the default resolver. defaultTier is the tier applied to
// every user that has no explicit override. overridesJSON is an optional
// JSON object mapping userID -> tier (used for local testing and early-access
// grants before the user store exists).
func NewResolver(defaultTier string, overridesJSON string) Resolver {
	dt := Tier(defaultTier)
	if dt == "" {
		dt = TierFree
	}
	ov := map[string]Tier{}
	if overridesJSON != "" {
		_ = json.Unmarshal([]byte(overridesJSON), &ov)
	}
	return &configResolver{defaultTier: dt, overrides: ov}
}

func (r *configResolver) Resolve(userID string) Tier {
	if t, ok := r.overrides[userID]; ok {
		return t
	}
	return r.defaultTier
}

// Snapshot is the current quota view for one user, returned by Acquire/Inspect
// and surfaced to the UI via /api/quota.
type Snapshot struct {
	Tier                  Tier `json:"tier"`
	MaxFilesPerSubmission int  `json:"max_files_per_submission"`
	MaxRenderTasksPerDay  int  `json:"max_render_tasks_per_day"`
	MaxConcurrentTasks    int  `json:"max_concurrent_tasks"`
	FilesInThisSubmission int  `json:"files_in_this_submission"`
	ConcurrentNow         int  `json:"concurrent_now"`
	DailyUsed             int  `json:"daily_used"`
	DailyRemaining        int  `json:"daily_remaining"`
	ConcurrentRemaining   int  `json:"concurrent_remaining"`
}

// Usage tracks per-user concurrent + daily render-task counts in memory. The
// same API can be backed by Redis in multi-instance deploys.
type Usage struct {
	mu         sync.Mutex
	concurrent map[string]int            // userID -> running job count
	daily      map[string]map[string]int // date -> userID -> submitted count
	day        string
}

func NewUsage() *Usage {
	return &Usage{
		concurrent: map[string]int{},
		daily:      map[string]map[string]int{},
		day:        today(),
	}
}

func today() string { return time.Now().Format("2006-01-02") }

func (u *Usage) rollDay() {
	u.day = today()
	u.daily = map[string]map[string]int{}
}

// buildSnap assembles a Snapshot from the live counters + tier limits.
func buildSnap(tier Tier, lim Limits, used, conc int) Snapshot {
	return Snapshot{
		Tier:                  tier,
		MaxFilesPerSubmission: lim.MaxFilesPerSubmission,
		MaxRenderTasksPerDay:  lim.MaxRenderTasksPerDay,
		MaxConcurrentTasks:    lim.MaxConcurrentTasks,
		ConcurrentNow:         conc,
		DailyUsed:             used,
		DailyRemaining:        lim.MaxRenderTasksPerDay - used,
		ConcurrentRemaining:   lim.MaxConcurrentTasks - conc,
	}
}

// Acquire reserves one render-task slot for userID against lim. It fails
// (returning the Snapshot and ok=false) when the concurrent or daily cap is
// already reached. On success the caller MUST call Release when the job ends.
// The returned Snapshot reflects the state AFTER the slot was taken.
func (u *Usage) Acquire(userID string, tier Tier, lim Limits) (Snapshot, bool) {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.day != today() {
		u.rollDay()
	}
	if u.daily[u.day] == nil {
		u.daily[u.day] = map[string]int{}
	}
	used := u.daily[u.day][userID]
	conc := u.concurrent[userID]
	if conc >= lim.MaxConcurrentTasks {
		return buildSnap(tier, lim, used, conc), false
	}
	if used >= lim.MaxRenderTasksPerDay {
		return buildSnap(tier, lim, used, conc), false
	}
	u.concurrent[userID] = conc + 1
	u.daily[u.day][userID] = used + 1
	return buildSnap(tier, lim, u.daily[u.day][userID], u.concurrent[userID]), true
}

// Release frees one concurrent slot for userID. The daily count is permanent
// for the day (a submitted task always counts toward the daily quota).
func (u *Usage) Release(userID string) {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.concurrent[userID] > 0 {
		u.concurrent[userID]--
	}
}

// Inspect returns current usage without reserving a slot.
func (u *Usage) Inspect(userID string, tier Tier, lim Limits) Snapshot {
	u.mu.Lock()
	defer u.mu.Unlock()
	if u.day != today() {
		u.rollDay()
	}
	used := u.daily[u.day][userID]
	conc := u.concurrent[userID]
	return buildSnap(tier, lim, used, conc)
}
