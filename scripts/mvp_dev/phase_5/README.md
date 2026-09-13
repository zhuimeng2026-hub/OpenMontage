# Phase 5 — §17.F + §17.G — Agent Gateway + 状态聚合

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.1(§17.F + §17.G)
上游 gate:Phase 0 + Phase 1 + Phase 2 + Phase 3 + Phase 4 全绿(§17.A 微信身份 / §17.B+H 租户与文件 ACL / §17.C 产品素材 / §17.D Project+Job+状态机 / §17.E Quota 已就绪)

## TL;DR

Phase 5 把"8 个业务动词的 Agent Gateway" + "OM 原始状态 → 13 档统一状态"两件事落地。**不接真实 OpenClaw / Hermes / OM MCP** —— 8 个 verb handler 全部 thin-wrapper 到 Phase 3 的同义 handler(`StartStage` / `Render` / `Cancel` / `Status`);`/api/status/lookup` 是纯函数 raw-string mapper。所有改动在 **独立 binary `cmd/mvp/`**(默认 `:18906`,顺接 Phase 4 `:18905`),不碰生产 BFF `main.go`(端口 8900)。**Phase 5 无新表**(纯逻辑层),`sql_migrations: ""`。

## §17.F + §17.G

§17.F(scope.md L783-800)要求 Gin 不理解 OM 内部细节,提供 8 个统一业务动词 `AnalyzeProductAssets` / `AnalyzeReferenceVideo` / `GenerateStoryboard` / `GenerateAnimatic` / `GenerateSample` / `RenderFinal` / `CancelProduction` / `GetProductionStatus`,内部可以转 OpenClaw/MCP。MVP 阶段不接真实框架,直接把 verb 路由到 Phase 3 的等价 handler —— `GenerateStoryboard` → `POST /api/video-projects/:id/storyboard`,`RenderFinal` → `POST /api/video-projects/:id/render`,`CancelProduction` → `POST /api/video-projects/:id/cancel`,`GetProductionStatus` → `GET /api/video-projects/:id/status`,`AnalyzeProductAssets` / `AnalyzeReferenceVideo` 暂用 `POST /api/video-projects`(预留 plan §21 接 OpenClaw 时再换 delegate target)。

§17.G(scope.md L802-823)要求前端不直接依赖 OM MCP 原始状态,统一映射到 13 档(scope.md 列 14 项;MVP 把 `WAITING_APPROVAL` 视为 UI gate 而非 OM 流程状态,不进 mapping 表 —— Phase 3 已知该态目前由 `approve-*` 端点驱动,见 phase_3 README "Known limitations")。`/api/status/lookup?raw=<om_state>` 是 `GET` 纯函数:raw 字符串 → `{"raw": ..., "unified": <13 档之一>}`,raw 已是 13 档之一则原样返回;**未知 raw 必须 fail-loud 映射到 `FAILED`**,不能 silently fallback 到 `RUNNING` / `PENDING` 之类模糊态(plan §8.2 + cron plan §8 risk 2:`mcp-raw` 的 `error_unknown` 暴露后不能被掩盖)。返回值由 `jobsvc.AllStatuses` 集合校验 —— 不在集合内 handler panic(`status_map.go` 的 `assertValid()`)。

## Files created / modified

| File | Created / Modified | Purpose |
|---|---|---|
| `frameflow/bff/internal/gwsvc/types.go` | created | `GatewayResponse` / `VerbRequest` 共享 JSON 类型(8 verb 共用 envelope) |
| `frameflow/bff/internal/gwsvc/verbs.go` | created | 8 个业务动词名常量 + 路由表(`VerbRoutes` map verb → url segment → handler) |
| `frameflow/bff/internal/gwsvc/status_map.go` | created | OM raw → 13 档映射表 + `Lookup(raw)` 纯函数 + `assertValid()` 校验 |
| `frameflow/bff/cmd/mvp/handlers_gateway.go` | created | 8 个 verb handler(thin-wrapper)+ `/status/lookup` handler |
| `frameflow/bff/cmd/mvp/main.go` | modified | 追加 9 条路由(8 verb + 1 lookup)到 `scoped` 分组(不动 Phase 0/1/2/3/4 既有路由) |

`gwsvc/` 与 `jobsvc/` 同级,沿用 Phase 3 的命名风格。

## Schema

**Phase 5 无 schema 改动**。`tasks.yaml` 显式 `sql_migrations: ""` —— 本阶段纯逻辑层,8 verb handler 直接调 Phase 3 既有的 `jobsvc.StartStage` / `jobsvc.Cancel` / `jobsvc.Get` 等函数,`/status/lookup` 是 stateless 纯函数,不需要持久化。状态聚合读 `jobsvc.AllStatuses` 集合作校验来源,数据表 `video_projects.status` 由 Phase 3 runner 写。

## Routes

`frameflow/bff/cmd/mvp/main.go` 挂载,运行在 `:18906`(Phase 0=18901 / 1=18902 / 2=18903 / 3=18904 / 4=18905 / **5=18906**)。所有路由挂在 Phase 1 的 `RequireJWT + TenantScope` scoped group(`/status/lookup` 也走 scoped —— 状态枚举不是公开 API),handler 直接消费 ctx 里的 `tenant_id` / `internal_user_id`。

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/gateway/analyze-product-assets` | JWT + `X-Tenant-Id` | `AnalyzeProductAssets` verb(当前委托 `POST /api/video-projects`) |
| POST | `/api/gateway/analyze-reference-video` | JWT + `X-Tenant-Id` | `AnalyzeReferenceVideo` verb(预留,当前 placeholder) |
| POST | `/api/gateway/generate-storyboard` | JWT + `X-Tenant-Id` | `GenerateStoryboard` → Phase 3 `POST /api/video-projects/:id/storyboard` |
| POST | `/api/gateway/generate-animatic` | JWT + `X-Tenant-Id` | `GenerateAnimatic` → Phase 3 `POST /api/video-projects/:id/animatic` |
| POST | `/api/gateway/generate-sample` | JWT + `X-Tenant-Id` | `GenerateSample` → Phase 3 `POST /api/video-projects/:id/sample` |
| POST | `/api/gateway/render-final` | JWT + `X-Tenant-Id` | `RenderFinal` → Phase 3 `POST /api/video-projects/:id/render`(Phase 4 reserve 之后) |
| POST | `/api/gateway/cancel-production` | JWT + `X-Tenant-Id` | `CancelProduction` → Phase 3 `POST /api/video-projects/:id/cancel` |
| GET | `/api/gateway/production-status` | JWT + `X-Tenant-Id` | `GetProductionStatus` → Phase 3 `GET /api/video-projects/:id/status?project_id=` |
| GET | `/api/status/lookup` | JWT + `X-Tenant-Id` | `?raw=<string>` → `{"raw":..., "unified":<13 档>}`(stateless pure fn) |

`/api/auth/login`、`/api/me/jwt`、`/api/tenants/*`、`/api/files/sign`、`/api/files/:key`、`/api/products/*` 来自 Phase 0+1+2,保留不动;`/api/video-projects/*`、`/api/jobs/:job_id` 来自 Phase 3,保留不动;`/api/quota/*` 来自 Phase 4,保留不动。Phase 5 只追加 9 条 gateway 路由。

## Verb dispatch

`frameflow/bff/internal/gwsvc/verbs.go` 的 `VerbRoutes` 是单 map,启动时一次性注册到 `scoped` group:

| Verb name | URL segment | Delegated to (Phase 3) | Placeholder note |
|---|---|---|---|
| `AnalyzeProductAssets` | `/api/gateway/analyze-product-assets` | `POST /api/video-projects`(project init + 触发 Phase 2 product manifest 重算) | MVP OK;plan §21 接 OpenClaw 后改走 `prepare_product_remix` |
| `AnalyzeReferenceVideo` | `/api/gateway/analyze-reference-video` | (placeholder) | 当前返回 501 Not Implemented —— MVP 没有 reference analysis handler,Phase 6+ 接 `analyze_reference_video` MCP tool 后再 delegate |
| `GenerateStoryboard` | `/api/gateway/generate-storyboard` | `POST /api/video-projects/:id/storyboard` | Phase 3 `jobsvc.StartStage` |
| `GenerateAnimatic` | `/api/gateway/generate-animatic` | `POST /api/video-projects/:id/animatic` | Phase 3 `jobsvc.StartStage` |
| `GenerateSample` | `/api/gateway/generate-sample` | `POST /api/video-projects/:id/sample` | Phase 3 `jobsvc.StartStage` |
| `RenderFinal` | `/api/gateway/render-final` | `POST /api/video-projects/:id/render` | Phase 4 reserve 之后才允许;Phase 3 handler 已自带 cost gate |
| `CancelProduction` | `/api/gateway/cancel-production` | `POST /api/video-projects/:id/cancel` | Phase 3 `jobsvc.Cancel`,任意态入 `CANCELLED` 终态 |
| `GetProductionStatus` | `/api/gateway/production-status?project_id=<id>` | `GET /api/video-projects/:id/status` | Phase 3 `jobsvc.Get` 读 `video_projects.status` |

verb handler 是 thin wrapper:解析 `VerbRequest` → 构造 Phase 3 handler 所需 path/query/body → 转发 → 把 Phase 3 response 包装成 `GatewayResponse{verb, project_id, status, raw: ...}`。**不复制业务逻辑**。

## Status mapping

`frameflow/bff/internal/gwsvc/status_map.go` 的 `lookupTable` 是 static map,覆盖 OM MCP 常见 raw 字符串 → 13 档统一状态。**`WAITING_APPROVAL` 不在表内**(见上文 §17.F + §17.G 段落解释)—— scope.md §17.G 列 14 项,MVP 映射表 13 项。返回值必须 ∈ `jobsvc.AllStatuses`,否则 `assertValid()` 触发 panic(plan §8.2 fail-loud)。

| OM raw (inbound) | Unified (outbound, 13 档) |
|---|---|
| `created` / `pending` | `CREATED` |
| `analyzing_assets` / `asset_analyzing` | `ASSET_ANALYZING` |
| `analyzing_reference` / `reference_analyzing` | `REFERENCE_ANALYZING` |
| `planning` / `scripting` | `PLANNING` |
| `storyboard_ready` / `storyboard_done` | `STORYBOARD_READY` |
| `animatic_rendering` / `animatic_in_progress` | `ANIMATIC_RENDERING` |
| `animatic_ready` / `animatic_done` | `ANIMATIC_READY` |
| `sample_rendering` / `sample_in_progress` | `SAMPLE_RENDERING` |
| `sample_ready` / `sample_done` | `SAMPLE_READY` |
| `final_rendering` / `render_in_progress` | `FINAL_RENDERING` |
| `completed` / `done` / `success` | `COMPLETED` |
| `failed` / `error` / `mcp-raw:error_unknown` | `FAILED` |
| `cancelled` / `canceled` | `CANCELLED` |
| (任意已在 13 档集合内的字符串) | 原样返回 |
| **(unknown / 不在表内)** | **`FAILED`** (fail-loud,plan §8.2) |

raw 已是 13 档之一(如前端 echo 一个 `STORYBOARD_READY` 进来)→ `Lookup()` 直接返回原值,绕过 table(幂等)。**unknown raw 不返回 unknown / running / pending** —— 这是 cron plan §8 risk 2 明确警告的反模式(`mcp-raw` 的 `error_unknown` 这种隐藏态必须可见)。

## Fail-loud policy

`status_map.go::Lookup(raw)` 行为:
1. 若 `raw ∈ jobsvc.AllStatuses` —— 返回 `{raw, unified: raw}`(原样,13 档之一)。
2. 若 `raw ∈ lookupTable` —— 返回 `{raw, unified: lookupTable[raw]}`。
3. 若都不命中 —— 返回 `{raw, unified: "FAILED"}`(fail-loud)。
4. `assertValid(unified)` 校验返回值 ∈ `jobsvc.AllStatuses` —— 不在集合内 `panic`(防御 lookupTable 写错或 jobsvc.AllStatuses 漂移)。

`FAILED` 而非 `UNKNOWN` / `RUNNING` 是刻意的(cron plan §8 risk 2):silent fallback 到 `RUNNING` 会让前端轮询不到失败,误以为还在跑;`UNKNOWN` 不是 §17.G 的合法档。未知 raw 落到 `FAILED` 让前端能立刻看到错误状态 + 走错误分支,与 Phase 3 状态机的 `FAILED` 终态语义一致。**这条规则禁止改 silent**,即便 Phase 6+ 接 MCP 暴露更多 raw 字符串也保持 fail-loud —— handler 只在新 raw 字符串出现时扩 lookupTable。

## How to run manually

```bash
cd /opt/OpenMontage_Voicebox && bash scripts/mvp_dev/phase_5/run.sh --fresh /tmp/phase_5_diff.txt
# gate.sh 自己启动 :18906 并端到端 curl — 不需要先手起服务:
bash scripts/mvp_dev/phase_5/gate.sh
```

`run.sh --fresh /tmp/phase_5_diff.txt` 全流程:`tasks.yaml` READY 守门(否则 stub-exit 0)→ 无 schema 迁移 → 写 4 个新 Go 文件 + 改 `cmd/mvp/main.go`(只追加 9 条 gateway 路由,不动既有路由)→ `go build -o /tmp/frameflow-bff-mvp-p5 ./cmd/mvp` → 后台启 `:18906`(`WEIXIN_MOCK_AUTH=1 MVP_PORT=18906`) → 等 `/healthz` 通。日志落 `logs/mvp_dev/run-phase_5-<timestamp>.log` + `logs/mvp_dev/phase_5-server.log`。

`gate.sh` 对照 `tasks.yaml` 的 `gate_min_verification` 跑端到端 curl:8 个 verb 都有路由(无 404)→ `/status/lookup?raw=...` 覆盖 13 档无 unknown → 跨 tenant 调 verb → 403 → 状态聚合响应 JSON 含 `status` 字段 ∈ 13 档。

通过后退出 0;`orchestrator.sh` 看到 `state/phase_5.json` 24h 内绿就跳过。

## What's NOT done in this phase

明确推迟到 Phase 6+:

- **真实 OpenClaw / Hermes skill wrapping**(plan §21):MVP verb handler 是 thin-wrapper,`GenerateStoryboard` 直接调 `jobsvc.StartStage`,**不调** OM `prepare_product_remix` MCP tool、不调 `OpenClaw/Hermes` skill runner。Phase 6+ 把 `verbs.go::VerbRoutes` 的 delegate target 改成 OpenClaw adapter 即可,verb URL 契约不变。
- **Async agent pool / job queue**(plan §17.D runner 后续):Phase 3 runner 是 `go func + sleep`,Phase 5 gateway 直接 sync 转发 Phase 3 handler —— 无独立 agent worker,无 durable queue,无 retry。Phase 6+ 接 OpenClaw 后才需要 broker(asynq / river / Postgres-based)。
- **`AnalyzeReferenceVideo` 真实实现**:当前 `verbs.go` 标 placeholder,handler 返回 501 Not Implemented。Phase 6+ 接 OM `analyze_reference_video` MCP tool 后 delegate 到对应 Phase 3 端点(目前 Phase 3 也没有 reference analysis handler,要 Phase 6+ 一并补)。
- **`WAITING_APPROVAL` 真实推进**:scope.md 列 14 项,本 phase 13 项,差 `WAITING_APPROVAL`。Phase 6+ 接 `approve-*` 端点(plan §10)后该态由 approve handler 写 `video_projects.status` 推进。
- **MCP inbound adapter / Webhook 接收**:`/api/gateway/*` 是 outbound verb 入口,**不是** MCP callback 接收端。Phase 6+ 加 `POST /api/mcp/webhook`(或类似)由 OM/OpenClaw 异步推 `mcp-raw` 状态过来,Phase 5 status_map 已经准备好,handler 把 raw 跑一遍 `Lookup()` 写库。
- **Go 单元测试**:`tasks.yaml` `go_tests: []` 显式声明占位,`internal/gwsvc/status_map_test.go` 等留 Phase 6 收尾。重点测:`Lookup(unknown)` → `FAILED`、`Lookup("STORYBOARD_READY")` 原样、`Lookup("mcp-raw:error_unknown")` → `FAILED`。
- **`AnalyzeProductAssets` 真实 delegate**:当前调 `POST /api/video-projects`(project create 路径,Phase 2 product manifest 重算),这是 MVP 占位 —— plan §21 要求最终 delegate 到 OM `prepare_product_remix`,Phase 6+ 换。

## Known limitations / risks

- **Gateway 是 sync thin-wrapper,无 backpressure**:8 verb handler 同步转发 Phase 3 handler,Phase 3 handler 又 sync 启 `go func` 然后立即返回 202。MVP 没有 verb 层的 in-flight 计数、cancel token、rate limit。Phase 4 Quota reserve 之前 `RenderFinal` 不应被高频调用 —— handler 不挡,只靠 Phase 4 cost gate。Phase 6+ 接真实 agent 后必须加 per-tenant concurrency semaphore(参考 `internal/limits/semaphore.go`)。
- **`/status/lookup` 无 cache,每次查都走 map**:13 项 static map,lookup O(1),但仍然每请求分配 JSON。Phase 6+ 接 MCP 高频 push 时建议把 `Lookup()` 结果缓存到 `sync.Map`(raw string → unified),按 raw 命中,miss 才走表。
- **Fail-loud 到 `FAILED` 可能让监控告警风暴**:任何 OM 端新引入 raw 字符串都会落 `FAILED`,前端看到的就是终态失败。Phase 6+ 必须(a)持续扩 `lookupTable` 覆盖新 raw + (b)加 metric 计数 `status_map_unknown_total` 监控 raw 漂移速度。MVP 没有 metric,纯日志。
- **`AnalyzeReferenceVideo` 路由存在但 handler 501**:路由挂上是为契约完整(prod 端测试可走 8 个 verb URL),但 placeholder 行为与 spec 不符 —— 任何 prod 调用 `analyze-reference-video` 都得 501。**Phase 6 第一个 task** 应该是补该 verb delegate(plan §21)。
- **scope.md §17.G 列 14 项 vs Phase 5 tasks.yaml 写 13 档**:scope.md 含 `WAITING_APPROVAL`,本 phase 映射表 13 项(把 `WAITING_APPROVAL` 视为 UI gate 而非 OM 流程态)。如果 Phase 6+ 接 OM 后 `mcp-raw` 真的回 `waiting_approval` 字符串,**lookupTable 必须显式加 `waiting_approval → WAITING_APPROVAL`**,并把 `WAITING_APPROVAL` 加进 `jobsvc.AllStatuses` —— 这是后续 PR 的 scope,**不属于 Phase 5**。
- **Phase 3 handler response 不直接是 `GatewayResponse`**:verb wrapper 自己 wrap 一层(`{verb, project_id, status, raw}`),但 Phase 3 内部 `job_id` / `progress` / `error_message` 这些字段没透出。Phase 6+ 接 OpenClaw 后 verb response 需要统一 envelope(`GatewayResponse{job_id, progress, error_message, ...}`),Phase 5 envelope 故意最小化。
- **跨 tenant 检查只在 handler 入口**:verb handler 委托 Phase 3 后,Phase 3 handler 自己做 `tenant_id` 校验(scope.md §17.B)。Phase 5 verb 层不重复校验,也不重复 403 —— 与 Phase 3 行为一致。**风险**:verb 层若 delegate 到 placeholder(如 `AnalyzeReferenceVideo` 501),tenant 校验可能漏;Phase 6+ 补 delegate 时必须在 verb handler 入口先 `middleware.TenantScope`(已经走 scoped group,等价)。