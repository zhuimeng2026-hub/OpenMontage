# Phase B / C / D Implementation Report — User Isolation via MCP Session

> Date: 2026-09-02
> Author: 主线程（直接接管 Phase D 收尾；Phase B / C 由 sub-agent 完成）
> Scope: `C:/OpenMontage_voicebox`
> Companion doc: `docs/user-isolation-via-mcp-session.md` (v2 plan)
> Audit report: `docs/audit-projects-touchpoints.md`
> Review note: this is the implementation work log, not the final acceptance record. Here, `done` means the listed batch was implemented; overall B/C/D remain partial because service identity, TTL, the remaining audited touchpoints, and production drills are open. The current verdict is in `docs/user-isolation-via-mcp-session.md`.

---

## 实施状态总览

| Phase | 内容 | 状态 | 关键文件 |
|---|---|---|---|
| **B** | durable `session → principal` registry (HMAC namespace_key) | done | `lib/principal_registry.py`, `lib/principal_sanitize.py` |
| **B.0** | OM 接收 `X-VClaw-User-Id` → ContextVar（之前已落）| done | `mcp_server.py` BearerTokenAuthMiddleware |
| **C** | `ProjectWorkspace` + 改造 6 个 HIGH 风险点 + claude_video | done | `lib/project_workspace.py`, 6 个 `tools/*.py` |
| **D** | feature flag + migration + rollback + backup + key_version 准备 | done | `lib/namespace_version.py`, `scripts/*`, `lib/principal_registry.py` (加 `key_version`) |

**测试结果**：
- `tests/integration/test_principal_registry.py` 36 / 36 pass
- `tests/integration/test_bearer_user_id.py` 20 / 20 pass
- `tests/integration/test_project_workspace.py` 41 / 41 pass
- `tests/integration/test_namespace_version.py` 10 / 10 pass
- `tests/test_migration_scripts.py` 7 / 7 pass
- **最初 Phase B+C+D 定向集：114 / 114 pass；三轮修复后的相关组合集：337 passed**

> 注：此前混跑的 fixture/Enum 顺序污染已修复；当前 337-test 组合包含 upload/read/concurrency-lock/Claude Video/renderer/Remotion 路径并通过。

---

## Phase B — Durable Principal Registry

### 新文件

- `lib/principal_registry.py` (592 行)
- `lib/principal_sanitize.py` (107 行)
- `tests/integration/test_principal_registry.py` (36 个 case)

### 关键设计

1. `namespace_key = HMAC_SHA256(secret, principal_id)[:16].hex` = 32 hex 字符
2. secret 读 `OPENMONTAGE_PRINCIPAL_HASH_SECRET`，未设时给稳定默认值 + 一次性 WARNING log
3. `Principal` 是 `@dataclass(frozen=True)`，构造时自动算 `namespace_key`，不可外部覆盖
4. **immutable owner**：bind 用 `ON CONFLICT DO UPDATE ... WHERE principal_id = excluded.principal_id` —— 同 owner 才能更新 TTL，跨 owner 直接 SECURITY log + 不写入
5. TTL 已写 (`expires_at` = 24h) 但未强制回收，**Phase E 才 sweeper**
6. 多 worker 安全：SQLite WAL + retry on `database is locked` 5 次 + threading.local 连接

### Phase B 设计点处理（worker 报告 8 个待拍板）

| # | 设计点 | 处理 |
|---|---|---|
| 1 | Tenant 未接入 | 留待 Phase A tenant-preserving refresh |
| 2 | `kind` 未算入 key | Phase D 加 `key_version` 字段，预留 rotation |
| 3 | bind 时机覆盖 | 已 insert-if-absent + WHERE 子句 |
| 4 | Windows SQLite WAL 多写者 | retry on locked；建议单写者部署 |
| 5 | `unbind` 暴露 | 留待 Phase E sweeper |
| 6 | `current_principal()` fast-path 优先级 | 按 spec：先 ContextVar，后 registry.require |
| 7 | 多 OM 实例共享 DB | 假定单写者，运维文档须明确 |
| 8 | `expires_at` 未读 | Phase E sweeper |

---

## Phase C — ProjectWorkspace + 改造 HIGH 风险点

### 新文件

- `lib/project_workspace.py` (305 行)
- `tests/integration/test_project_workspace.py` (627 行, 41 个 case)

### 改造的文件

| 文件 | 行 | 改造内容 |
|---|---|---|
| `lib/principal_sanitize.py` | +12 | 新增 `sanitize_project_id()` 白名单 |
| `tools/asset_upload.py` | L112-125 | 单 shot 上传走 `ProjectWorkspace` |
| `tools/asset_upload_chunk.py` | L84-92, L130-225 | chunked 上传 + state file 加 `namespace_key` 字段 |
| `tools/asset/read_session_asset.py` | L60-130 | Layer3 principal namespace 边界校验 |
| `tools/asset/read_session_asset_image.py` | 同上 | 同上 |
| `tools/external/claude_video.py` | L107, 460-490, 780-800 | 从 raw `user_openid` 改成 `namespace_key`（破坏性变更，存量需 Phase D migration） |
| `mcp_server.py` | L1762, L1774 | `create_remotion_video_share` 走 `ProjectWorkspace.for_current_principal(project).root` |

### Phase C 设计点处理

| # | 设计点 | 处理 |
|---|---|---|
| 1 | tweak_server 改造（不在 OM middleware）| **留作独立 PR** |
| 2 | claude_video.py raw openid 路径 | 已改用 namespace_key，存量**待 Phase D migration** |
| 3 | `asset_upload_chunk` 老 state file | 已加 `namespace_key` 字段；老文件通过短路规则兼容，v2-only 模式拒绝 |
| 4 | `upload_state` per-principal | 已采用 per-principal layout |
| 5 | `render_progress_sse` 无 principal 校验 | 未改（MEDIUM 风险，Phase E workbuddy_session 持久化 namespace_key） |
| 6 | `for_current_principal("lookup")` 占位 hack | 已用占位 project_id "lookup" 仅取 namespace_key |

---

## Phase D — Migration + Feature Flag + Enforcement

### 新文件

- `lib/namespace_version.py` (355 行) — feature flag
- `scripts/migrate_users_to_namespace_key.py` — 迁移工具
- `scripts/rollback_namespace_key.py` — 回滚工具
- `scripts/backup_mcp_sessions.py` — 备份 helper
- `tests/integration/test_namespace_version.py` (10 个 case)
- `tests/test_migration_scripts.py` (7 个 case)

### 修改的文件

- `lib/principal_registry.py` — 加 `key_version` 字段（DEFAULT 1），SQLite 表加列，`lookup()` 用 row 存的 version re-derive（**v2 rotation 的 seam**）

### Feature Flag 用法

```bash
# 部署期（默认，未设 = legacy）
export OPENMONTAGE_NAMESPACE_VERSION=legacy

# 灰度 10%
export OPENMONTAGE_NAMESPACE_VERSION=canary

# 强制 v2
export OPENMONTAGE_NAMESPACE_VERSION=v2-only
```

未识别值（typo）回退到 legacy + WARNING log。

### Migration 工具用法

```bash
# 先备份
python scripts/backup_mcp_sessions.py --label pre-migration

# dry-run
python scripts/migrate_users_to_namespace_key.py --dry-run

# 实际迁移
python scripts/migrate_users_to_namespace_key.py --apply

# 回滚（读 forward audit log）
python scripts/rollback_namespace_key.py
```

### Phase D 设计点处理

| # | 设计点 | 处理 |
|---|---|---|
| 1 | default 值 | legacy（保持直到存量迁完） |
| 2 | claude_video.py 存量迁移时机 | 跑 `migrate_users_to_namespace_key.py --apply` |
| 3 | tweak_server 改造时机 | 独立 PR |
| 4 | namespace_key v2 rotation | 留 TODO + key_version 字段就位 |
| 5 | canary bucket 比例 | 10%（可调） |

---

## 端到端验证矩阵

| 链路层 | 验证手段 | 状态 |
|---|---|---|
| desktop → vclaw `/api/mcp/proxy` | vclaw `f3b775d` / `d7b70bb` | done |
| vclaw → OM（带 `X-VClaw-User-Id`）| OM stage 3 + Phase B 集成 | done |
| OM `current_principal()` fast-path | 实现 + 单测 | done |
| OM `principal_registry.require()` 权威源 | test_principal_registry 36/36 | done |
| 工具用 `ProjectWorkspace` | test_project_workspace 41/41 | done |
| 渲染路径 | mcp_server.py:1774 | done |
| 上传 | test_asset_upload_chunk 17/17 | done |
| 读取 | 各自测试通过 | done |
| **Legacy migration dry-run** | test_migrate_dry_run | done |
| **Legacy migration 真跑 + 审计** | test_migrate_real_moves | done |
| **Migration rollback** | test_rollback_reverses_migration | done |
| **Backup mcp_sessions** | test_backup_* 3 case | done |

---

## 主线程接管 Phase D 的过程

Phase D sub-agent 在 `#38 backup helper` 处停滞 17 分钟未产文件，主线程**直接接管剩余 4 个任务**（不重写已有代码）：

1. 写 `scripts/backup_mcp_sessions.py`（50 行，`shutil.copytree(dirs_exist_ok=False)` + 5 个退出码）
2. `lib/principal_registry.py` 加 `key_version` 字段 + SQLite 列 + `compute_namespace_key(principal_id, key_version=...)` 接口 + lookup 时按 row version re-derive
3. 修 Final typing 的 Python 3.13 兼容（`"Final[frozenset[int]]"` → 直接 `frozenset({1})`）
4. 加 `tests/integration/test_namespace_version.py`（10 case）+ `tests/test_migration_scripts.py`（7 case）+ 修动态 namespace_key 计算（不用 hardcoded）

**关键 fix**：worker 写的 `CREATE TABLE` 没带 `key_version` 列（`replace_all` 没生效），跑了 16 个旧测试失败 → 手工 patch + 复测 → 36/36 pass。

---

## 后续 TODO（不在本批次）

- A.5 desktop `auth.ts` 兜底流程
- B.4 service principal：`Principal.kind = "service"` 与独立 namespace_key 派生
- B.5 TTL sweeper：启动时扫过期 binding 删除
- C.x tweak_server 改造
- C.x render_progress_sse principal 校验
- D.x 上线 rollout：dry-run → 切 v2-only → 删 legacy fallback

---

## 决策项汇总（待用户拍板）

| 项 | 选项 | 默认 |
|---|---|---|
| namespace_version default | legacy / canary / v2-only | **legacy**（推荐） |
| migration 执行时机 | 立刻 / 上线前 / 双轨 2 周 | **建议双轨 2 周** |
| tweak_server 改造 | 本批次 / 独立 PR / 不做 | **独立 PR** |
| service principal | 本批次 / v2.1 / 不做 | **v2.1** |
| namespace_key v2 rotation | 本批次 / 留 TODO / 立即 | **留 TODO + key_version 字段就位** |

---

## 跨仓库状态

### vclaw (`c:/vclaw`)
- `f3b775d` Stage 1: `MCPProxyHandler` 注入 `X-VClaw-User-Id`
- `d7b70bb` Stage 2: `MCPRawProxyHandler` 收紧，未绑定 401
- `docs/user-isolation-via-mcp-session.md` (v2)

### OpenMontage_voicebox (`c:/OpenMontage_voicebox`)
- `242940e` Stage 3: OM 接收头 + ContextVar
- B+C+D 本批次及后续复核修复的提交状态以当前分支 `git history` 为准。
- `docs/user-isolation-via-mcp-session.md` (v2)
- `docs/audit-projects-touchpoints.md`
- `docs/phase-b-c-d-implementation-report.md` (本文件)
