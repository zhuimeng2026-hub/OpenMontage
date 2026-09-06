# Phase Decision Items — 待拍板清单（2026-09-02）

> 与 `phase-b-c-d-implementation-report.md` 配套。本文件是**主线程留给用户复核时拍板**的清单。
> 状态说明：这是实施时的历史决策清单；三轮复核已关闭旧 schema、key rotation、v1/v2 reconcile、rollback containment 等代码项，仍有效的上线阻断以 `docs/user-isolation-via-mcp-session.md` 当前结论为准。

---

## 优先级 P0（上线前必须定）

### D.1 namespace_version 默认值

当前 `lib/namespace_version.py:107` 默认值是 `LEGACY`（未设 env var 时）。

| 选项 | 含义 | 风险 |
|---|---|---|
| **legacy**（当前默认）| v2 优先 + v1 fallback | 无强制隔离，存量未迁也能跑 |
| **canary** | 10% 强制 v2 | 灰度可控；需监控 |
| **v2-only** | 强制 v2 | 存量必须先迁完 |

**建议**：保持 `legacy` 直至存量迁移完成 + 跑过 `--apply` 脚本验证。

### D.2 migration 执行时机

| 选项 | 含义 |
|---|---|
| **立刻** | 跑 `scripts/migrate_users_to_namespace_key.py --apply` 一次 |
| **上线前一周** | 灰度期间保留 legacy + 双写 |
| **双轨 2 周** | legacy 默认 + 灰度迁移 + 监控无 v1 命中后切 v2-only |

**建议**：双轨 2 周（先 dry-run 看 audit，识别有问题的 openid 目录，再 apply）。

---

## 优先级 P1（上线后第一周内）

### D.3 tweak_server 改造

`tweak_server/app.py:43,152,305` + `assets.py:51` 用共享 `TWEAK_SERVER_BEARER`，**完全不在 `BearerTokenAuthMiddleware` 范围**，是唯一剩下的 HIGH 风险点。

| 选项 | 含义 |
|---|---|
| **独立 PR**（推荐）| tweak_server 自解析 `X-VClaw-User-Id` + 自己 `principal_registry.bind`（不复用 OM 的 DB）|
| **不做** | 承认 tweak_server 是受信旁路，不加隔离 |

**建议**：独立 PR（tweak_server 跑在独立进程，复用 `lib/principal_registry` 但用自己的 DB 文件）。

### D.4 service principal

`Principal.kind` 当前只有 `"user"`；服务间调用（如 Backlot / BFF / tweakserver）无身份。

| 选项 | 含义 |
|---|---|
| **v2.1**（推荐）| 等 tweak_server 改造落地后再统一设计 service principal |
| **本批次** | 现在加 `kind="service"` + 独立 namespace_key 派生 |
| **不做** | 保持所有调用都是 user |

---

## 优先级 P2（长期改进）

### D.5 namespace_key v2 rotation

`lib/principal_registry.py:131` 已留 `_AVAILABLE_NAMESPACE_KEY_VERSIONS = frozenset({1})` + `compute_namespace_key(principal_id, key_version=)` 接口。**真正的 v2 实现**（不同 secret、不同 digest 或双层 HMAC）待 secret rotation 触发时实施。

### D.6 TTL sweeper

`expires_at` 已写但未强制回收。Phase E 启动时扫一次过期的 binding 删除。

### D.7 render_progress_sse principal 校验

`mcp_server.py:2354` 按 `job_id` fan-out SSE，无 principal 校验（MEDIUM 风险）。需 workbuddy_session 持久化 namespace_key 才能做。

---

## vclaw 端待拍板

### A.5 桌面端 auth.ts 兜底

桌面端走 `/api/mcp/proxy` 已正确（config.ts:61）。**当前无遗漏项**。

### A.x tenant-preserving refresh

refresh tokens 在 `internal/handler/desktop_auth.go:390` 用 `defaultDesktopScopes` + `EnsurePersonalTenant` 已落（`ensureDesktopScopes` 已覆盖）。**当前无遗漏项**。

---

## 上线 checklist

- [ ] 跑 `python scripts/backup_mcp_sessions.py --label pre-migration`
- [ ] 跑 `python scripts/migrate_users_to_namespace_key.py --dry-run` → 检查 audit log
- [ ] 跑 `python scripts/migrate_users_to_namespace_key.py --apply`
- [ ] 跑 `python scripts/rollback_namespace_key.py`（**测试性回滚**，验证 rollback 能跑通）→ 再 apply
- [ ] 跑 `pytest tests/integration/ tests/test_migration_scripts.py`
- [ ] 设置 `OPENMONTAGE_NAMESPACE_VERSION=v2-only`
- [ ] 监控 24h 无 401 错误后，下线 legacy fallback

---

## 跨仓库链接

- vclaw `docs/user-isolation-via-mcp-session.md` (v2)
- OM `docs/user-isolation-via-mcp-session.md` (v2)
- OM `docs/audit-projects-touchpoints.md`
- OM `docs/phase-b-c-d-implementation-report.md`
- OM `docs/phase-decision-items-2026-09-02.md`（本文件）
