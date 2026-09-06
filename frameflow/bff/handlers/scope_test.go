package handlers

import (
	"testing"
)

// TestRenderQueueOwnerIDStableAcrossSessions proves the core of the
// cross-machine fix: the same WeChat account resolves to the SAME stable owner
// scope no matter which device (ff_sid) it logs in from. The BFF keys every
// store (upstream MCP session, image batches, templates, compositions, quota)
// by this scope, so two machines logged in as the same account share the same
// server-side namespaces — like email.
func TestRenderQueueOwnerIDStableAcrossSessions(t *testing.T) {
	h := &Handlers{}
	sidA := randHex(16)
	sidB := randHex(16)
	anon := randHex(16)

	// Same WeChat account on two different devices.
	h.saveUser(sidA, map[string]interface{}{"openid": "wx-acct-1", "nickname": "Alice"})
	h.saveUser(sidB, map[string]interface{}{"openid": "wx-acct-1", "nickname": "Alice"})
	defer func() {
		h.dropUser(sidA)
		h.dropUser(sidB)
	}()

	scopeA := renderQueueOwnerID(sidA)
	scopeB := renderQueueOwnerID(sidB)
	if scopeA == "" || scopeB == "" {
		t.Fatal("empty scope for a logged-in session")
	}
	if scopeA != scopeB {
		t.Fatalf("same WeChat account must map to the same scope across devices; got %s vs %s", scopeA, scopeB)
	}

	// A logged-in account's scope must differ from an anonymous device's scope.
	anonScope := renderQueueOwnerID(anon)
	if anonScope == scopeA {
		t.Fatal("anonymous device scope must not equal a logged-in account scope")
	}
}

// TestRenderQueueOwnerIDAnonymousIsolation proves anonymous/dev flows keep the
// previous per-device isolation: two distinct browser sessions never share a
// scope, so one anonymous user cannot see another's data.
func TestRenderQueueOwnerIDAnonymousIsolation(t *testing.T) {
	sidA := randHex(16)
	sidB := randHex(16)
	if renderQueueOwnerID(sidA) == renderQueueOwnerID(sidB) {
		t.Fatal("two distinct anonymous sessions must not share a scope")
	}
}
