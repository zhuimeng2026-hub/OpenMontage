package mcp

import (
	"path/filepath"
	"testing"

	"frameflow-bff/internal/state"
)

// TestPersistedUserSessionKeyedByScope verifies the durable upstream-session
// mapping is keyed by the stable owner identity (scope), not the raw device
// ff_sid. Because two machines logged in as the same account derive the SAME
// scope, they resolve to (and resume) the SAME upstream Mcp-Session-Id — which
// is what keeps the uploaded assets and rendered videos consistent across
// machines and across BFF restarts/instances.
func TestPersistedUserSessionKeyedByScope(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "bff_scope_test.db")
	db, err := state.Open(dbPath)
	if err != nil {
		t.Fatalf("open state db: %v", err)
	}
	defer db.Close()

	store := NewSessionStore("http://unused", "tok", db)

	const scope = "owner-scope-wechat-acct-1"
	const upstream = "upstream-sid-abc123"

	if err := store.persistUserSession(scope, upstream); err != nil {
		t.Fatalf("persist: %v", err)
	}
	got, err := store.findPersistedUserSession(scope)
	if err != nil {
		t.Fatalf("find: %v", err)
	}
	if got != upstream {
		t.Fatalf("expected persisted upstream %q for scope, got %q", upstream, got)
	}

	// A different scope (a different account or device) must NOT see the other
	// upstream session — isolation is structural.
	other, err := store.findPersistedUserSession("owner-scope-different")
	if err != nil {
		t.Fatalf("find other: %v", err)
	}
	if other != "" {
		t.Fatalf("scope leak: a different scope resolved to %q", other)
	}

	// Re-persisting the same scope must update rather than duplicate.
	if err := store.persistUserSession(scope, "upstream-sid-rotated"); err != nil {
		t.Fatalf("re-persist: %v", err)
	}
	got, _ = store.findPersistedUserSession(scope)
	if got != "upstream-sid-rotated" {
		t.Fatalf("expected rotated upstream id, got %q", got)
	}
}

// TestGetOrCreateKeyedByScope proves that, once a scope has a persisted upstream
// session, getOrCreate returns the SAME client (and therefore the SAME upstream
// Mcp-Session-Id) for that scope every time — including from a second machine
// that derives the same scope. This is the mechanism that makes a render started
// on one device visible/continuable on another. It exercises the resume path, so
// it needs no live upstream.
func TestGetOrCreateKeyedByScope(t *testing.T) {
	dbPath := filepath.Join(t.TempDir(), "bff_scope_test2.db")
	db, err := state.Open(dbPath)
	if err != nil {
		t.Fatalf("open state db: %v", err)
	}
	defer db.Close()

	store := NewSessionStore("http://unused", "tok", db)
	const scope = "owner-scope-acct-2"
	const upstream = "up-sid-shared"

	if err := store.persistUserSession(scope, upstream); err != nil {
		t.Fatalf("persist: %v", err)
	}

	c1, err := store.getOrCreate(scope)
	if err != nil {
		t.Fatalf("getOrCreate 1: %v", err)
	}
	c2, err := store.getOrCreate(scope)
	if err != nil {
		t.Fatalf("getOrCreate 2: %v", err)
	}
	if c1.SessionID() != upstream || c2.SessionID() != upstream {
		t.Fatalf("expected resumed upstream id %q, got %q and %q", upstream, c1.SessionID(), c2.SessionID())
	}
	if c1 != c2 {
		t.Fatal("expected the same in-memory client for the same scope (cross-machine reuse)")
	}

	// A scope with no persisted upstream must not resolve to anyone else's.
	if got, _ := store.findPersistedUserSession("owner-scope-acct-3"); got != "" {
		t.Fatalf("unexpected upstream for a fresh scope: %q", got)
	}
}
