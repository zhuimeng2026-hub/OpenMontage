# Phase 3 — §17.D — Project / Job 管理

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.1(§17.D) + §17.G(状态枚举)
上游 gate:Phase 0 + Phase 1 + Phase 2 全绿(§17.B `TenantScope` + §17.C `products` / `product_assets` / `file_acl` 已就绪)

## TL;DR

Phase 3 把"一个 `video_project` + 它的生产 job + 14 档状态机"这块 SaaS 数据模型落到 2 张表 + 11 条路由。所有改动在 **独立 binary `cmd/mvp/`**(默认 `:18904`,顺接 Phase 2 `:18903`),不碰生产 BFF `main.go`(端口 8900)。Job runner 不接 OM / MCP / OpenClaw — POST `/storyboard|animatic|sample|render` 启一个 in-process goroutine,sleep + 直接写状态,MVP gate 只看终态。

## §17.D — Project/Job

`video_projects` 是租户内一个视频项目实体,每个 project 链接到一个 Phase 2 的 `product`(同 tenant 校验),带 `creative_brief_json` + `reference_mode`(`description_first` / `balanced` / `reference_first`,默认 `balanced`) + `reference_file_key`(`file_acl` 校验)。`production_jobs` 是统一 jobs 表(`job_type ∈ storyboard | animatic | sample | render`),MVP 简化不分 `production_jobs` / `preview_jobs` / `render_jobs` 三张表(§17.D 列出但 MVP gate 不要求)。状态机按 §17.G 14 档:`CREATED → ASSET_ANALYZING → REFERENCE_ANALYZING → PLANNING → STORYBOARD_READY → ANIMATIC_RENDERING → ANIMATIC_READY → SAMPLE_RENDERING → SAMPLE_READY → WAITING_APPROVAL → FINAL_RENDERING → COMPLETED`,旁路 `FAILED` / `CANCELLED` 任意态可入,任意态终态。所有路由挂在 Phase 1 的 `RequireJWT + TenantScope` scoped group —— handler 直接消费 ctx 里的 `tenant_id` / `internal_user_id`。

## Files created / modified

| File | Created / Modified | Purpose |
|---|---|---|
| `frameflow/bff/internal/jobsvc/types.go` | created | `VideoProject` / `ProductionJob` struct + 14 档 `Status` 常量 + `ReferenceMode` / `JobType` 枚举 |
| `frameflow/bff/internal/jobsvc/store.go` | created | `video_projects` + `production_jobs` DB CRUD;`Get*` / `Update*` 强制 tenant 匹配(403 on mismatch) |
| `frameflow/bff/internal/jobsvc/states.go` | created | `AllowedTransitions` 邻接表 + `Advance()` 白名单推进 + `AdvanceByType()` 跳跃式标记 |
| `frameflow/bff/internal/jobsvc/runner.go` | created | MVP job runner —— `go func + sleep + Advance`;不调 OM / MCP |
| `frameflow/bff/cmd/mvp/handlers_project.go` | created | 11 条路由 HTTP handler |
| `frameflow/bff/cmd/mvp/main.go` | modified | 追加 11 条路由到 `scoped` 分组(不动 Phase 0/1/2 路由) |

`jobsvc/` 与 `productsvc/` 同级,沿用 Phase 2 的 store 风格(plain `*sql.DB` + `sqlx`-style 命名参数;无 ORM)。

## Schema migrations

由 `phase_3/run.sh` step 1 用 `sqlite3` 应用到 `${BFF}/data/frameflow.db`(同 Phase 0/1/2 数据库)。两条 `CREATE TABLE IF NOT EXISTS` + 索引 `IF NOT EXISTS`,幂等:

```sql
-- 视频项目
CREATE TABLE IF NOT EXISTS video_projects (
  id                  TEXT PRIMARY KEY,
  tenant_id           TEXT NOT NULL,
  product_id          TEXT NOT NULL,
  creative_brief_json TEXT NOT NULL DEFAULT '{}',
  reference_mode      TEXT NOT NULL DEFAULT 'balanced',
  reference_file_key  TEXT NOT NULL DEFAULT '',
  status              TEXT NOT NULL DEFAULT 'CREATED',
  created_by          TEXT NOT NULL,
  created_at          TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at          TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_video_projects_tenant ON video_projects(tenant_id);
CREATE INDEX IF NOT EXISTS idx_video_projects_product ON video_projects(product_id);

-- 生产/预览/渲染 job — 统一 jobs 表,加 job_type 区分(MVP 简化,不分三张表)
CREATE TABLE IF NOT EXISTS production_jobs (
  id                TEXT PRIMARY KEY,
  tenant_id         TEXT NOT NULL,
  video_project_id  TEXT NOT NULL,
  job_type          TEXT NOT NULL,             -- storyboard | animatic | sample | render
  external_run_id   TEXT NOT NULL DEFAULT '',
  om_project_id     TEXT NOT NULL DEFAULT '',
  status            TEXT NOT NULL DEFAULT 'pending',
  progress          REAL NOT NULL DEFAULT 0,
  cost_reserved     REAL NOT NULL DEFAULT 0,
  cost_actual       REAL NOT NULL DEFAULT 0,
  error_message     TEXT NOT NULL DEFAULT '',
  created_by        TEXT NOT NULL,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_production_jobs_project ON production_jobs(video_project_id);
CREATE INDEX IF NOT EXISTS idx_production_jobs_tenant ON production_jobs(tenant_id);
```

迁移 source-of-truth 在 `scripts/mvp_dev/phase_3/tasks.yaml` 的 `sql_migrations` 段;`run.sh` 用 here-doc 复制。所有 `tenant_id` / `product_id` / `video_project_id` / `file_key` / `internal_user_id` 仍不走 FK(同 Phase 1/2 notes:SQLite + 跨 Phase 增量迁移,Phase 4 收尾)。

## Routes

`frameflow/bff/cmd/mvp/main.go` 挂载,运行在 `:18904`(Phase 0=18901 / 1=18902 / 2=18903 / **3=18904**)。

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/video-projects` | JWT + `X-Tenant-Id` | 创建 project(必须 link 到同 tenant 的 `product_id`,否则 403) |
| GET | `/api/video-projects/:id` | JWT + `X-Tenant-Id` | 读 project 元数据(403 if 非同 tenant) |
| PUT | `/api/video-projects/:id/brief` | JWT + `X-Tenant-Id` | 替换 `creative_brief_json` + `reference_mode` |
| POST | `/api/video-projects/:id/reference` | JWT + `X-Tenant-Id` | 记录 `reference_file_key`;`file_acl` 校验(403 if 跨租户) |
| POST | `/api/video-projects/:id/storyboard` | JWT + `X-Tenant-Id` | 启 storyboard job(状态 → `STORYBOARD_READY`) |
| POST | `/api/video-projects/:id/animatic` | JWT + `X-Tenant-Id` | 启 animatic job(状态 → `ANIMATIC_RENDERING` → `ANIMATIC_READY`) |
| POST | `/api/video-projects/:id/sample` | JWT + `X-Tenant-Id` | 启 sample job(状态 → `SAMPLE_RENDERING` → `SAMPLE_READY`) |
| POST | `/api/video-projects/:id/render` | JWT + `X-Tenant-Id` | 启 final render(状态 → `FINAL_RENDERING` → `COMPLETED`) |
| POST | `/api/video-projects/:id/cancel` | JWT + `X-Tenant-Id` | 任意状态 → `CANCELLED`(终态) |
| GET | `/api/video-projects/:id/status` | JWT + `X-Tenant-Id` | 读当前 `status`(给前端轮询) |
| GET | `/api/jobs/:job_id` | JWT + `X-Tenant-Id` | 读 job 详情(`progress` / `error_message`) |

`/api/auth/login`、`/api/me/jwt`、`/api/tenants/*`、`/api/files/sign`、`/healthz` 来自 Phase 0 + Phase 1,保留不动;`/api/products/*` 来自 Phase 2,保留不动。

## State machine

`jobsvc/states.go` 定义 14 档状态 + 邻接表 `AllowedTransitions`,`Advance()` 只允许白名单内的下一态,非法转移返回 error(状态机 monotonic)。MVP gate 允许"跳跃式"标记 —— `AdvanceByType(job_type)` 由 `job_type` 决定合法的跳跃路径,例如 `POST /storyboard` 直接 `CREATED → STORYBOARD_READY`(略过 `PLANNING`),给 gate 跑端到端时不必等所有中间态。

| 当前状态 | 合法下一态(MVP 推进) | 触发动作 |
|---|---|---|
| `CREATED` | `STORYBOARD_READY` / `CANCELLED` | `POST /storyboard` / `POST /cancel` |
| `STORYBOARD_READY` | `ANIMATIC_RENDERING` / `CANCELLED` | `POST /animatic` / `POST /cancel` |
| `ANIMATIC_RENDERING` | `ANIMATIC_READY` / `FAILED` | runner goroutine 完成 / 失败 |
| `ANIMATIC_READY` | `SAMPLE_RENDERING` / `CANCELLED` | `POST /sample` / `POST /cancel` |
| `SAMPLE_RENDERING` | `SAMPLE_READY` / `FAILED` | runner goroutine 完成 / 失败 |
| `SAMPLE_READY` | `FINAL_RENDERING` / `WAITING_APPROVAL` / `CANCELLED` | `POST /render` / 留 Phase 5+ approve-* / `POST /cancel` |
| `FINAL_RENDERING` | `COMPLETED` / `FAILED` | runner goroutine 完成 / 失败 |
| `COMPLETED` | (终态) | — |
| `FAILED` | (终态) | — |
| `CANCELLED` | (终态) | — |
| `ASSET_ANALYZING` / `REFERENCE_ANALYZING` / `PLANNING` / `WAITING_APPROVAL` | (MVP 不主动推进) | 留 Phase 5+ 接 OM / MCP |

`POST /cancel` 在 `jobsvc/states.go` 显式枚举合法入态(任何非终态都可),`AdvanceByType` 内部分支按 `job_type` 选跳跃路径(例如 `storyboard` 走 `CREATED → STORYBOARD_READY`,`render` 走 `SAMPLE_READY → FINAL_RENDERING → COMPLETED`)。`production_jobs.status` 自身只走 `pending → running → succeeded | failed`,跟 `video_projects.status` 解耦 —— runner 推进 video_project status 的同时改自己 job 行。

## MVP job runner

`jobsvc/runner.go` 实现最简版 job runner,**不调任何外部系统**(OM / OpenClaw / MCP 全部留 Phase 5)。典型 storyboard 路径:

1. handler 收到 `POST /api/video-projects/:id/storyboard` → 校验 status 允许跳跃 → `InsertJob(job_type=storyboard, status=pending)` 返回 `job_id`。
2. handler `go jobsvc.RunJob(job_id)` 起一个 goroutine,立即返回 202。
3. goroutine 内 `UpdateJobStatus(job_id, "running")` → `sleep` 几百 ms(`MVP_JOB_RUNTIME_MS` env,默认 300ms)→ 调 `AdvanceByType("storyboard")` 把 video_project 推到 `STORYBOARD_READY` → `UpdateJobStatus(job_id, "succeeded")`。

`animatic` / `sample` / `render` 走类似路径,但 `AdvanceByType` 会推进两档(例如 animatic: `STORYBOARD_READY → ANIMATIC_RENDERING → ANIMATIC_READY`),runner 内部用两个 sleep 段 + 两次 Advance 模拟。失败路径留 `MVP_JOB_FAILURE_RATE` env(MVP 不启用,默认 0),当前 gate 不要求。

**关键简化**:runner 跑在 Gin 进程 in-process,无外部 broker / queue。重启即丢 —— Phase 4 收尾再上 durable job table + retry(见"Known limitations")。handler 永不阻塞,前端通过 `GET /api/video-projects/:id/status` 轮询。

## Tenant cross-product reference check

`POST /api/video-projects` 校验:`product_id` 必须存在 + `product.tenant_id == ctx.tenant_id`,否则 403(`jobsvc/store.go` 内 `LoadProductForTenant(productID, tenantID)`,行级 tenant 过滤)。`POST /api/video-projects/:id/reference` 校验:`reference_file_key` 必须已在 Phase 1 的 `file_acl` 注册 + 绑定 `tenant_id == ctx.tenant_id`,否则 403(`file_acl.Check` 调用,handler 内复用 Phase 1 的 ACL helper)。`video_projects.Get/Update` 一律走 `tenant_id` 过滤,handler 拿到 row 后再 `assert row.tenant_id == ctx.tenant_id`,防 SQL 层拼接绕过。`production_jobs.Get/Update` 同样强制 —— `GET /api/jobs/:job_id` 跨 tenant 直接 404(不暴露存在性)。

## How to run manually

```bash
cd /opt/OpenMontage_Voicebox && bash scripts/mvp_dev/phase_3/run.sh --fresh /tmp/phase_3_diff.txt
# gate.sh 自己启动 :18904 并端到端 curl — 不需要先手起服务:
bash scripts/mvp_dev/phase_3/gate.sh
```

`run.sh --fresh /tmp/phase_3_diff.txt` 全流程:`tasks.yaml` READY 守门 → 两张表 schema 迁移 → 写 5 个新 Go 文件 + 改 `cmd/mvp/main.go`(只追加 project + jobs 路由,不动既有路由) → `go build -o /tmp/frameflow-bff-mvp-p3 ./cmd/mvp` → 后台启 `:18904`(`WEIXIN_MOCK_AUTH=1 MVP_PORT=18904`) → 等 `/healthz` 通。日志落 `logs/mvp_dev/run-phase_3-<timestamp>.log` + `logs/mvp_dev/phase_3-server.log`。

`gate.sh` 对照 `tasks.yaml` 的 `gate_min_verification` 跑端到端 curl:创建 product(借 Phase 2)→ 创建 video_project → 启 storyboard → 轮询 `status` 看状态机单调推进 → 跨 tenant 调 → `GET /api/jobs/:job_id` 读 job 详情。

通过后退出 0;`orchestrator.sh` 看到 `state/phase_3.json` 24h 内绿就跳过。

## What's NOT done in this phase

明确推迟到 Phase 4+:

- **`quota` 视图 + reserve / consume 语义**(Phase 4 — §17.E):`production_jobs.cost_reserved` / `cost_actual` 字段已留,但 MVP 不写值、不拦截超限。
- **Agent Gateway 8 个业务动词(analyze / storyboard / animatic / sample / render / cancel / status)**(Phase 5 — §17.F):当前 4 个 stage trigger 路由(`/storyboard|animatic|sample|render`)是 MVP 直跑,不经过 Agent Gateway 层。
- **真实 OM / OpenClaw / MCP 集成**(Phase 5):runner 不调任何外部服务,`external_run_id` / `om_project_id` 字段留空。
- **OM MCP 状态码 → 统一 14 档 mapping table**(Phase 5 — §17.G):当前 `Advance()` 只用本进程内枚举,Phase 5+ 接 MCP 后再加 inbound mapper。
- **approve-* 端点**(`approve-storyboard` / `approve-animatic` / `approve-sample`,plan §10):`tasks.yaml` 的 `gate_endpoints` 没列,scope §17.D 也不要求,留 Phase 5+ 与 WAITING_APPROVAL 一起接。Phase 3 `SAMPLE_READY` 可直跳 `FINAL_RENDERING`。
- **`production_jobs` / `preview_jobs` / `render_jobs` 三表拆分**(plan §17.D):MVP 统一 `production_jobs` + `job_type` 区分。Phase 5+ 拆表 + 加 `preview_artifacts`(§23 三层 Preview artifact 落表)再决定。
- **中间态 `ASSET_ANALYZING` / `REFERENCE_ANALYZING` / `PLANNING` / `WAITING_APPROVAL` 的真实推进**:MVP `AdvanceByType` 跳过这些中间态;Phase 5+ 接 OM 后由 Agent Gateway 推送。
- **失败重试 / dead-letter 队列**:runner 失败只置 `FAILED` 终态,不自动 retry,无 DLQ。
- **Go 单元测试**:`tasks.yaml` `go_tests: []` 显式声明占位,`internal/jobsvc/states_test.go` 等留 Phase 4 收尾。

## Known limitations / risks

- **Runner in-process,无 durable queue**:`jobsvc.RunJob` 是 `go func`,重启进程 = 丢在跑的 goroutine + job 卡在 `running` 状态。Phase 4+ 必须换成查表 poll + 重启恢复(基于 `updated_at` 超时判定)。当前 GATE 不覆盖进程重启。
- **Runner 失败无 retry**:MVP runner 失败路径不启用(`MVP_JOB_FAILURE_RATE=0` 默认);Phase 5+ 接 OM 时失败可能来自外部 API,需要 max_retries + 退避策略。
- **状态机跳跃式标记 vs 严格 monotonic**:`AdvanceByType` 允许 `CREATED → STORYBOARD_READY` 直跳以满足 GATE,但生产语义上中间应跑过 `PLANNING`。Phase 5+ 接 OM 后改严格模式,中间态由 Agent Gateway 推进。
- **`production_jobs` 表写并发**:`jobsvc/runner.go` runner goroutine + handler 内 status 更新共用一个 SQLite + `MaxOpenConns(1)`,百级并发场景下 `UPDATE` 排队;Phase 4+ 接 durable queue 时一起加事务隔离。