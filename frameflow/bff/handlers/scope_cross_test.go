package handlers

import (
	"net/http"
	"net/http/httptest"
	"path/filepath"
	"testing"

	"github.com/gin-gonic/gin"

	"frameflow-bff/internal/config"
	"frameflow-bff/internal/imagebatch"
	"frameflow-bff/internal/limits"
	"frameflow-bff/internal/mcp"
	"frameflow-bff/internal/state"
)

// 跨用户隔离回归：每个测试都构造 Alice 与 Bob 两个 scope，验证 server-side store
// 不会让 Bob 看到 Alice 的数据。所有测试都不依赖真实上游 MCP —— 用
// mcp.NewSessionStore("http://127.0.0.1:1", …) 拿到惰性 store，需要上游时用
// httptest.NewServer 起本地 stub。
//
// 覆盖矩阵参见 docs/frameflow-user-isolation-audit-2026-08-19.md §4。

// newCrossScopeDB opens a fresh SQLite file under t.TempDir() and registers a
// cleanup that closes the db and restores the package-level userDB pointer.
func newCrossScopeDB(t *testing.T) (*config.Config, *mcp.SessionStore, *imagebatch.Store) {
	t.Helper()
	db, err := state.Open(filepath.Join(t.TempDir(), "scope_cross.db"))
	if err != nil {
		t.Fatalf("state.Open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })

	cfg := &config.Config{
		AuthRequired:    false,
		DevLoginAllowed: false,
		SessionSecure:   false,
	}
	store := mcp.NewSessionStore("http://127.0.0.1:1", "", db)
	batches := imagebatch.NewStore(db)
	return cfg, store, batches
}

// crossScopeHandlers builds a Handlers bundle wired to the cross-scope DB so
// renderQueueOwnerID(...) + h.saveUser(...) works consistently across tests.
func crossScopeHandlers(t *testing.T, cfg *config.Config, store *mcp.SessionStore, batches *imagebatch.Store) *Handlers {
	t.Helper()
	db, err := state.Open(filepath.Join(t.TempDir(), "scope_cross_handlers.db"))
	if err != nil {
		t.Fatalf("state.Open: %v", err)
	}
	t.Cleanup(func() { _ = db.Close() })
	prev := userDB
	userDB = db
	t.Cleanup(func() { userDB = prev })

	lim := limits.NewResolver("free", "")
	h := New(cfg, store, lim, batches, db)
	t.Cleanup(func() {
		// 清空 hot cache，避免后续测试被前一个测试的 WeChat 登录污染 scope 计算。
		userStore.Lock()
		for k := range userStore.m {
			delete(userStore.m, k)
		}
		userStore.Unlock()
	})
	return h
}

// callAs issues a Gin-served request with the ff_sid cookie set to the given
// session id. Use this for HTTP-level cross-scope assertions.
func callAs(r *gin.Engine, method, path, sid string) *httptest.ResponseRecorder {
	gin.SetMode(gin.TestMode)
	req := httptest.NewRequest(method, path, nil)
	if sid != "" {
		req.AddCookie(&http.Cookie{Name: "ff_sid", Value: sid, Path: "/"})
	}
	w := httptest.NewRecorder()
	r.ServeHTTP(w, req)
	return w
}

// ---- imagebatch.Store 隔离 ----------------------------------------------------

// TestImageBatch_OtherWechatUserCannotList：Alice 与 Bob 在同一 BFF 上各自
// 创建一个 batch，Bob 的 List 必须不出现 Alice 的 batch id。
func TestImageBatch_OtherWechatUserCannotList(t *testing.T) {
	_, _, batches := newCrossScopeDB(t)
	sidAlice := randHex(16)
	sidBob := randHex(16)
	scopeAlice := renderQueueOwnerID(sidAlice)
	scopeBob := renderQueueOwnerID(sidBob)
	if scopeAlice == scopeBob {
		t.Fatalf("distinct anonymous sids must produce distinct scopes, both got %s", scopeAlice)
	}

	// Alice 上传完一组图片创建一个 batch。
	aliceBatch, err := batches.Create(scopeAlice, "batch-alice", "frameflow-batch-alice", "photo-ken-burns")
	if err != nil {
		t.Fatalf("alice create: %v", err)
	}
	// Bob 同一时刻建一个自己的 batch。
	bobBatch, err := batches.Create(scopeBob, "batch-bob", "frameflow-batch-bob", "cinematic-montage")
	if err != nil {
		t.Fatalf("bob create: %v", err)
	}

	// Bob 的 List 必须只看到自己的。
	bobList, err := batches.List(scopeBob)
	if err != nil {
		t.Fatalf("bob list: %v", err)
	}
	for _, b := range bobList {
		if b.ID == aliceBatch.ID {
			t.Fatalf("Bob's List leaked Alice's batch %q (scope=%s)", aliceBatch.ID, scopeBob)
		}
	}
	// 反向亦然。
	aliceList, err := batches.List(scopeAlice)
	if err != nil {
		t.Fatalf("alice list: %v", err)
	}
	for _, b := range aliceList {
		if b.ID == bobBatch.ID {
			t.Fatalf("Alice's List leaked Bob's batch %q (scope=%s)", bobBatch.ID, scopeAlice)
		}
	}
	// 烟雾测试：每个 scope 至少能拿到自己的那一条。
	if len(bobList) != 1 || bobList[0].ID != bobBatch.ID {
		t.Fatalf("Bob List returned %v; want only bob's batch", bobList)
	}
	if len(aliceList) != 1 || aliceList[0].ID != aliceBatch.ID {
		t.Fatalf("Alice List returned %v; want only alice's batch", aliceList)
	}
}

// TestImageBatch_Get_ScopedToOwner：Bob 用 Alice 的 batch id 调 Get，必须返回
// nil（NOT a permission error in the body — just absent）。
func TestImageBatch_Get_ScopedToOwner(t *testing.T) {
	_, _, batches := newCrossScopeDB(t)
	scopeAlice := renderQueueOwnerID(randHex(16))
	scopeBob := renderQueueOwnerID(randHex(16))

	aliceBatch, err := batches.Create(scopeAlice, "batch-alice", "frameflow-batch-alice", "photo-ken-burns")
	if err != nil {
		t.Fatalf("alice create: %v", err)
	}

	// Bob 拿不到 Alice 的 batch。
	got, err := batches.Get(scopeBob, aliceBatch.ID)
	if err != nil {
		t.Fatalf("bob Get returned err: %v", err)
	}
	if got != nil {
		t.Fatalf("Bob's Get(aliceID) leaked Alice's batch: %+v", got)
	}
	// Alice 自己能拿到。
	got, err = batches.Get(scopeAlice, aliceBatch.ID)
	if err != nil {
		t.Fatalf("alice Get returned err: %v", err)
	}
	if got == nil || got.ID != aliceBatch.ID {
		t.Fatalf("Alice's Get(aliceID) returned %v; want alice's batch", got)
	}
}

// TestImageBatch_ByProject_CrossScopeReturnsOwnersRow：两个 scope 各自用同一个
// project_id 建一个 batch，ByProject 必须各自返回自己 scope 的 row，而不是混
// 淆。UNIQUE INDEX(session_id, project_id) 保证这一点。
func TestImageBatch_ByProject_CrossScopeReturnsOwnersRow(t *testing.T) {
	_, _, batches := newCrossScopeDB(t)
	scopeAlice := renderQueueOwnerID(randHex(16))
	scopeBob := renderQueueOwnerID(randHex(16))
	const sharedProject = "frameflow-batch-shared"

	a, err := batches.Create(scopeAlice, "batch-alice", sharedProject, "photo-ken-burns")
	if err != nil {
		t.Fatalf("alice create: %v", err)
	}
	b, err := batches.Create(scopeBob, "batch-bob", sharedProject, "cinematic-montage")
	if err != nil {
		t.Fatalf("bob create: %v", err)
	}

	rowA, err := batches.ByProject(scopeAlice, sharedProject)
	if err != nil {
		t.Fatalf("alice ByProject: %v", err)
	}
	rowB, err := batches.ByProject(scopeBob, sharedProject)
	if err != nil {
		t.Fatalf("bob ByProject: %v", err)
	}
	if rowA == nil || rowA.ID != a.ID {
		t.Fatalf("alice ByProject returned %+v; want %s", rowA, a.ID)
	}
	if rowB == nil || rowB.ID != b.ID {
		t.Fatalf("bob ByProject returned %+v; want %s", rowB, b.ID)
	}
	if rowA.ID == rowB.ID {
		t.Fatalf("ByProject returned same id across scopes: %s", rowA.ID)
	}
}

// TestImageBatch_Update_ScopedToOwner：Bob 试图 Update Alice 的 batch id 必须
// 返回 nil 且不影响 Alice 的数据。这是 S3 修复的镜像面 —— store.Update 把
// sessionID 当作 WHERE 条件的一部分。
func TestImageBatch_Update_ScopedToOwner(t *testing.T) {
	_, _, batches := newCrossScopeDB(t)
	scopeAlice := renderQueueOwnerID(randHex(16))
	scopeBob := renderQueueOwnerID(randHex(16))

	aliceBatch, err := batches.Create(scopeAlice, "batch-alice", "frameflow-batch-alice", "photo-ken-burns")
	if err != nil {
		t.Fatalf("alice create: %v", err)
	}

	// Bob 用 aliceBatch.ID 调 Update —— 必须返回 nil。
	updated, err := batches.Update(scopeBob, aliceBatch.ID, func(b *imagebatch.Batch) {
		b.Status = "rendering"
	})
	if err != nil {
		t.Fatalf("bob Update returned err: %v", err)
	}
	if updated != nil {
		t.Fatalf("Bob's Update(aliceID) leaked: %+v", updated)
	}

	// Alice 的 batch 状态必须没变。
	stillAlice, err := batches.Get(scopeAlice, aliceBatch.ID)
	if err != nil {
		t.Fatalf("alice Get after bob's Update: %v", err)
	}
	if stillAlice == nil {
		t.Fatalf("alice batch vanished after bob's Update")
	}
	if stillAlice.Status == "rendering" {
		t.Fatalf("Bob's Update mutated Alice's batch: status=%q", stillAlice.Status)
	}
}

// ---- 登录 / 登出 / scope 派生 -----------------------------------------------

// TestSessionScope_LoginLogoutDoesNotPromoteAnonToWechat：匿名用户上传了一批
// 图片，登录 WeChat 后这些匿名 batch 不应出现在 WeChat scope 下。这是 B8+B11
// 的关键不变量：scope 派生器只读 loadUserMap 的快照，登录不会把匿名数据"继
// 承"给 WeChat scope。
func TestSessionScope_LoginLogoutDoesNotPromoteAnonToWechat(t *testing.T) {
	cfg, store, batches := newCrossScopeDB(t)
	h := crossScopeHandlers(t, cfg, store, batches)

	sid := randHex(16)
	anonScope := renderQueueOwnerID(sid)
	if anonScope == "" {
		t.Fatal("empty scope for anon sid")
	}

	// 1) 匿名态上传一个 batch。
	anonBatch, err := batches.Create(anonScope, "batch-anon", "frameflow-batch-anon", "photo-ken-burns")
	if err != nil {
		t.Fatalf("anon create: %v", err)
	}

	// 2) 同一 sid 上登录 WeChat。openid 与之前无关 —— 必须派生新 scope。
	const openid = "wx-login-test"
	h.saveUser(sid, map[string]interface{}{"openid": openid})
	t.Cleanup(func() { h.dropUser(sid) })

	wechatScope := renderQueueOwnerID(sid)
	if wechatScope == anonScope {
		t.Fatalf("scope did not change after WeChat login: still %s", wechatScope)
	}

	// 3) WeChat scope 看不到匿名 batch。
	wechatList, err := batches.List(wechatScope)
	if err != nil {
		t.Fatalf("wechat list: %v", err)
	}
	for _, b := range wechatList {
		if b.ID == anonBatch.ID {
			t.Fatalf("WeChat scope leaked anon batch %q", anonBatch.ID)
		}
	}

	// 4) 登出。dropUserMap 必须清掉 userStore，且 anonScope 下原来的数据仍在
	//    （dropUser 不删数据，只是解绑登录态）。
	h.dropUser(sid)
	if renderQueueOwnerID(sid) != anonScope {
		t.Fatalf("post-logout scope should be anon scope %s, got %s",
			anonScope, renderQueueOwnerID(sid))
	}
	if _, err := batches.Get(anonScope, anonBatch.ID); err != nil {
		t.Fatalf("anon batch vanished after logout: %v", err)
	}
}

// TestLogout_AnonymousSessionHasNoAccessToWechatScopeBatches：Alice 在
// WeChat scope 下创建一个 batch，登出后换一个全新的匿名 sid 派生新 scope，
// 该 scope 不能读到 Alice 的数据（B9）。
func TestLogout_AnonymousSessionHasNoAccessToWechatScopeBatches(t *testing.T) {
	cfg, store, batches := newCrossScopeDB(t)
	h := crossScopeHandlers(t, cfg, store, batches)

	// 1) Alice 登录 + 建 batch。
	aliceSID := randHex(16)
	h.saveUser(aliceSID, map[string]interface{}{"openid": "wx-alice-b9"})
	t.Cleanup(func() { h.dropUser(aliceSID) })
	aliceScope := renderQueueOwnerID(aliceSID)

	aliceBatch, err := batches.Create(aliceScope, "batch-alice-b9", "frameflow-batch-b9", "photo-ken-burns")
	if err != nil {
		t.Fatalf("alice create: %v", err)
	}

	// 2) Alice 登出（dropUserMap + 清 cookie 模拟）。匿名 sid 仍未销毁。
	h.dropUser(aliceSID)
	// 此时 aliceSID 派生出来的 scope 是匿名 scope（不再是 wechat）。
	postLogoutScope := renderQueueOwnerID(aliceSID)
	if postLogoutScope == aliceScope {
		t.Fatalf("scope unchanged after dropUser: %s", postLogoutScope)
	}

	// 3) 用该匿名 scope 取 Alice 的 batch —— 必须找不到。
	got, err := batches.Get(postLogoutScope, aliceBatch.ID)
	if err != nil {
		t.Fatalf("anon Get after logout: %v", err)
	}
	if got != nil {
		t.Fatalf("post-logout anon scope leaked Alice's batch: %+v", got)
	}
}

// ---- RenderProgress / S5 ----------------------------------------------------

// TestRenderProgress_EmptySIDReturns404：RenderProgress 必须用 OwnsJob 拒绝
// 任何未持有 ff_sid 的请求。空 cookie → renderQueueOwnerID("") → OwnsJob 直
// 接 false。这是 S5 的回归测试。
func TestRenderProgress_EmptySIDReturns404(t *testing.T) {
	cfg, store, batches := newCrossScopeDB(t)
	h := crossScopeHandlers(t, cfg, store, batches)

	gin.SetMode(gin.TestMode)
	r := gin.New()
	r.GET("/api/render-progress/:jobId", h.RenderProgress)

	w := callAs(r, http.MethodGet, "/api/render-progress/any-job-id", "")
	if w.Code != http.StatusNotFound {
		t.Fatalf("empty sid should 404, got %d (body=%s)", w.Code, w.Body.String())
	}
}

// ---- auth 缓存再校期 / S3 ---------------------------------------------------

// TestAuthLoadUserMapRecheckExpiryAfterCacheWrite：DB 里持久化的 expires_at 在
// 读取与缓存写入之间过期的情况，必须不被当作登录态。这锁定 S3 的 belt-and-
// braces isExpired 二次检查（同时也是首次 isExpired 检查的回归测试 —— 只要 DB
// 行是过期的，loadUserMap 必须返回 nil 并清缓存）。
func TestAuthLoadUserMapRecheckExpiryAfterCacheWrite(t *testing.T) {
	cfg, store, batches := newCrossScopeDB(t)
	h := crossScopeHandlers(t, cfg, store, batches)

	// 1) 通过 persistUser 写入合法 user（它会强制把 expires_at 设成 now+12h，
	//    不接受外部传入的过期值）。然后手动 UPDATE 把 expires_at 改成过去时间，
	//    模拟「写入合法 → 之后过期」的窗口。
	sid := randHex(16)
	liveUser := map[string]interface{}{
		"openid":   "wx-expired",
		"nickname": "expired",
	}
	if err := persistUser(userDB, sid, liveUser); err != nil {
		t.Fatalf("persistUser: %v", err)
	}
	if _, err := userDB.Exec(`UPDATE wechat_users SET expires_at = ? WHERE ff_sid = ?`,
		"2020-01-01T00:00:00Z", sid); err != nil {
		t.Fatalf("force expire: %v", err)
	}

	// userStore hot cache 不能命中（用全新 sid）。
	userStore.RLock()
	if _, ok := userStore.m[sid]; ok {
		userStore.RUnlock()
		t.Fatalf("sid %s already in hot cache; test setup invalid", sid)
	}
	userStore.RUnlock()

	// 2) 调用 loadUserMap —— 必须返回 nil（被 isExpired 拦截）。
	got := h.loadUser(sid)
	if got != nil {
		t.Fatalf("expired user was returned as logged-in: %+v", got)
	}
	// 3) hot cache 也不应保留这条记录。
	userStore.RLock()
	if _, ok := userStore.m[sid]; ok {
		userStore.RUnlock()
		t.Fatalf("expired user landed in hot cache: %+v", userStore.m[sid])
	}
	userStore.RUnlock()
}

// ---- 表驱动隔离巡检 ---------------------------------------------------------

// TestCrossScopeIsolation：对所有以 (sessionID, …) 为 key 的 store 做一次
// Alice↔Bob 烟雾测试。任何一条失败都会让该次断言详细报错，方便日后新增 store
// 时复用本测试脚手架。
func TestCrossScopeIsolation(t *testing.T) {
	_, _, batches := newCrossScopeDB(t)
	sidAlice := randHex(16)
	sidBob := randHex(16)
	scopeAlice := renderQueueOwnerID(sidAlice)
	scopeBob := renderQueueOwnerID(sidBob)
	if scopeAlice == scopeBob {
		t.Fatalf("alice/bob scopes collapsed: %s", scopeAlice)
	}

	a, err := batches.Create(scopeAlice, "batch-alice", "frameflow-batch-alice", "photo-ken-burns")
	if err != nil {
		t.Fatalf("alice create: %v", err)
	}
	b, err := batches.Create(scopeBob, "batch-bob", "frameflow-batch-bob", "cinematic-montage")
	if err != nil {
		t.Fatalf("bob create: %v", err)
	}

	// 每个 case 直接断言，不再包一层 generic helper —— tuple expansion 在
	// struct literal 内行为不可靠，且可读性更差。
	t.Run("list_alice_omits_bob", func(t *testing.T) {
		list, err := batches.List(scopeAlice)
		if err != nil {
			t.Fatalf("alice list: %v", err)
		}
		for _, x := range list {
			if x.ID == b.ID {
				t.Fatalf("alice scope leaked bob's batch %q", b.ID)
			}
		}
		found := false
		for _, x := range list {
			if x.ID == a.ID {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("alice scope missing her own batch %q; got %d entries", a.ID, len(list))
		}
	})
	t.Run("list_bob_omits_alice", func(t *testing.T) {
		list, err := batches.List(scopeBob)
		if err != nil {
			t.Fatalf("bob list: %v", err)
		}
		for _, x := range list {
			if x.ID == a.ID {
				t.Fatalf("bob scope leaked alice's batch %q", a.ID)
			}
		}
		found := false
		for _, x := range list {
			if x.ID == b.ID {
				found = true
				break
			}
		}
		if !found {
			t.Fatalf("bob scope missing his own batch %q; got %d entries", b.ID, len(list))
		}
	})
	t.Run("get_alice_via_alice", func(t *testing.T) {
		got, err := batches.Get(scopeAlice, a.ID)
		if err != nil {
			t.Fatalf("alice get alice: %v", err)
		}
		if got == nil || got.ID != a.ID {
			t.Fatalf("alice get alice = %+v", got)
		}
	})
	t.Run("get_bob_via_bob", func(t *testing.T) {
		got, err := batches.Get(scopeBob, b.ID)
		if err != nil {
			t.Fatalf("bob get bob: %v", err)
		}
		if got == nil || got.ID != b.ID {
			t.Fatalf("bob get bob = %+v", got)
		}
	})
	t.Run("cross_get_alice_via_bob", func(t *testing.T) {
		got, err := batches.Get(scopeBob, a.ID)
		if err != nil {
			t.Fatalf("bob get alice: %v", err)
		}
		if got != nil {
			t.Fatalf("bob scope leaked alice's batch via Get: %+v", got)
		}
	})
	t.Run("cross_get_bob_via_alice", func(t *testing.T) {
		got, err := batches.Get(scopeAlice, b.ID)
		if err != nil {
			t.Fatalf("alice get bob: %v", err)
		}
		if got != nil {
			t.Fatalf("alice scope leaked bob's batch via Get: %+v", got)
		}
	})
}