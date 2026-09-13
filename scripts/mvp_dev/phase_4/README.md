# Phase 4 — §17.E — Quota / Billing

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.1(§17.E,scope:771-780)
上游 gate:Phase 0 + 1 + 2 + 3 全绿(§17.D `video_projects` / `production_jobs` + 14 档状态机已就绪)

## TL;DR

Phase 4 把 §17.E 的 `available / reserved / consumed` 三档 credits 记账落到 2 张表 + 5 条路由,并在 Phase 3 的 `POST /render` 前挂 auto-reserve hook。核心是**单条 atomic `UPDATE ... WHERE`**:条件不满足 → 0 rows affected → 402,不用锁、不用读-改-写。不变量 `available + reserved + consumed == tier_grant` 由"每次只在同一行的两列之间搬运"保证。所有改动仍在独立 binary `cmd/mvp/`(默认 `:18905`,顺接 Phase 3 `:18904`),不碰生产 BFF `main.go`(端口 8900)。**不接真计费系统** —— MVP 只在 DB 里记账 + 幂等检查。

## §17.E — Quota

scope §17.E 只给了三个字段名(`available_credits` / `reserved_credits` / `consumed_credits`)和一句"高成本 Final Render 前建议先 reserve"。Phase 4 把它具体化成:`quota_credits` 每个 tenant 一行(PK = `tenant_id`,默认 free tier = 100 credits),`quota_ledger` 每次 reserve / consume / refund 写一行审计。三个动词的语义是**双向搬运**而非增减:reserve 把 credits 从 `available` 搬到 `reserved`,consume 从 `reserved` 搬到 `consumed`,refund 从 `reserved` 搬回 `available` —— 全程总量守恒。`Reserve()` 返回 `reservation_id`(= `quota_ledger.id`),`Consume()` / `Refund()` 必须带上它,靠 ledger 行做幂等(同一 reservation 二次结算 → 409)。所有路由挂在 Phase 1 的 `RequireJWT + TenantScope` scoped group —— handler 直接消费 ctx 里的 `tenant_id` / `internal_user_id`,跨 tenant 一律 403。

## Files created / modified

| File | Created / Modified | Purpose |
|---|---|---|
| `frameflow/bff/internal/quotasvc/types.go` | created | `Quota` / `LedgerEntry` struct + `Operation` 枚举(`reserve` / `consume` / `refund`)+ 默认 tier 常量(`free` = 100 credits) |
| `frameflow/bff/internal/quotasvc/store.go` | created | `quota_credits` CRUD + `Reserve` / `Consume` / `Refund` 单事务实现;`EnsureRow(tenant_id)` upsert free tier |
| `frameflow/bff/internal/quotasvc/ledger.go` | created | `quota_ledger` 写入 + `LookupReservation(reservation_id)` 幂等检查 + `balance_after` JSON 快照 |
| `frameflow/bff/internal/quotasvc/cost.go` | created | `EstimateCost(job_type) float64` 固定表(见"Cost estimation") |
| `frameflow/bff/cmd/mvp/handlers_quota.go` | created | 4 条 quota 路由 HTTP handler |
| `frameflow/bff/cmd/mvp/main.go` | modified | 追加 quota 路由到 `scoped` 分组(不动 Phase 0/1/2/3 路由;当前挂载点见 `main.go:62-75`) |
| `frameflow/bff/cmd/mvp/handlers_project.go` | modified | Phase 3 的 `POST /render` 在 `RunJobAsync` 之前调 `quotasvc.Reserve` |

`quotasvc/` 与 `productsvc/` / `jobsvc/` 同级,沿用 Phase 2 的 store 风格(plain `*sql.DB` + `context.Context` 首参,无 ORM;参考 `internal/productsvc/store.go:64`)。

> **注意**:`tasks.yaml:12-16` 的 `files_to_create` 只列了 4 个文件,但 `notes` 第 6 条要求 cost 表写在 `quotasvc/cost.go`(`tasks.yaml:76`)—— 实际是 5 个新文件。以本表为准。

## Schema migrations

由 `phase_4/run.sh` step 1 用 `sqlite3` 应用到 `${BFF}/data/frameflow.db`(同 Phase 0/1/2/3 数据库)。两条 `CREATE TABLE IF NOT EXISTS` + 索引 `IF NOT EXISTS`,幂等:

```sql
-- 每个 tenant 一行额度。MVP 默认 free tier = 100 credits。
CREATE TABLE IF NOT EXISTS quota_credits (
  tenant_id         TEXT PRIMARY KEY,
  available_credits REAL NOT NULL DEFAULT 100,
  reserved_credits  REAL NOT NULL DEFAULT 0,
  consumed_credits  REAL NOT NULL DEFAULT 0,
  tier              TEXT NOT NULL DEFAULT 'free',
  updated_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

-- 审计 ledger:每次 reserve/consume/refund 一行。id 同时是 reservation_id。
CREATE TABLE IF NOT EXISTS quota_ledger (
  id              TEXT PRIMARY KEY,
  tenant_id       TEXT NOT NULL,
  operation       TEXT NOT NULL,               -- reserve | consume | refund
  amount          REAL NOT NULL,
  job_id          TEXT NOT NULL DEFAULT '',
  balance_after   TEXT NOT NULL DEFAULT '{}',  -- {"available": N, "reserved": M, "consumed": K}
  created_by      TEXT NOT NULL,
  created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_quota_ledger_tenant ON quota_ledger(tenant_id, created_at);
CREATE INDEX IF NOT EXISTS idx_quota_ledger_job ON quota_ledger(job_id);
```

迁移 source-of-truth 在 `scripts/mvp_dev/phase_4/tasks.yaml:22-45` 的 `sql_migrations` 段;`run.sh` 用 here-doc 复制。`tenant_id` / `job_id` 仍不走 FK(同 Phase 1/2/3 notes:SQLite + 跨 Phase 增量迁移)。`quota_credits` 无 `CHECK` 约束 —— 不变量由 Go 侧 SQL 保证,不由 DB 强制(见"Known limitations")。

## Routes

`frameflow/bff/cmd/mvp/main.go` 挂载,运行在 `:18905`(Phase 0=18901 / 1=18902 / 2=18903 / 3=18904 / **4=18905**)。

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/api/quota` | JWT + `X-Tenant-Id` | 读 tenant 额度(`available` / `reserved` / `consumed` / `tier`);行不存在则 upsert free tier 后返回 |
| POST | `/api/quota/reserve` | JWT + `X-Tenant-Id` | `{amount, job_id}` → `reserved += amount`,`available -= amount`;返回 `reservation_id`。余额不足 → 402 |
| POST | `/api/quota/consume` | JWT + `X-Tenant-Id` | `{reservation_id}` → `consumed += amount`,`reserved -= amount`。二次结算 → 409 |
| POST | `/api/quota/refund` | JWT + `X-Tenant-Id` | `{reservation_id}` → `available += amount`,`reserved -= amount`。二次结算 → 409 |
| POST | `/api/video-projects/:id/render` | JWT + `X-Tenant-Id` | (Phase 3 hook)启 final render 前自动 reserve 50 credits;失败自动 refund |

`/api/auth/login`、`/api/me/jwt`、`/api/tenants/*`、`/api/files/*`、`/healthz`(Phase 0 + 1)、`/api/products/*`(Phase 2)、`/api/video-projects/*` + `/api/jobs/:job_id`(Phase 3)保留不动。

状态码约定:402 余额不足 / 403 跨 tenant / 404 `reservation_id` 不存在 / 409 该 reservation 已结算。

## Reserve / consume / refund 语义

三个动词各是**一条** `UPDATE`,条件写在 `WHERE` 里 —— 不做"先 SELECT 再判断再 UPDATE"(那有 TOCTOU 窗口)。`RowsAffected() == 0` 就是拒绝信号:

```sql
-- reserve:available → reserved。0 rows affected ⇒ 余额不足 ⇒ 402
UPDATE quota_credits
   SET available_credits = available_credits - :amount,
       reserved_credits  = reserved_credits  + :amount,
       updated_at        = datetime('now')
 WHERE tenant_id = :tenant_id
   AND available_credits >= :amount;

-- consume:reserved → consumed。0 rows affected ⇒ reservation 已被结算/额度漂移 ⇒ 409
UPDATE quota_credits
   SET reserved_credits = reserved_credits - :amount,
       consumed_credits = consumed_credits + :amount,
       updated_at       = datetime('now')
 WHERE tenant_id = :tenant_id
   AND reserved_credits >= :amount;

-- refund:reserved → available(原数返还)
UPDATE quota_credits
   SET reserved_credits  = reserved_credits  - :amount,
       available_credits = available_credits + :amount,
       updated_at        = datetime('now')
 WHERE tenant_id = :tenant_id
   AND reserved_credits >= :amount;
```

**不变量:`available + reserved + consumed == tier_grant`**(free tier = 100)。三条 SQL 每条都是同一行内两列的等量搬运(`- :amount` / `+ :amount` 成对出现),三列之和恒定,所以不变量在任何一条语句前后都成立,与并发无关。`tier_grant` 由 Go 侧按 `tier` 列查常量表得到,不存在行里 —— 校验用一条 reconciliation query 跑:`SELECT tenant_id FROM quota_credits WHERE available_credits + reserved_credits + consumed_credits != 100 AND tier = 'free'`。

单次调用的完整顺序(`quotasvc/store.go`):

1. `EnsureRow(tenant_id)` —— `INSERT OR IGNORE` 兜底建行。
2. (consume / refund 才有)`LookupReservation(reservation_id)` —— ledger 里必须存在 `operation='reserve'` 且 `tenant_id` 匹配的行,且**没有**后续引用它的 consume / refund 行,否则 409。`amount` 从这行读,不信任 client 传的数。
3. 执行上面对应的 `UPDATE`,`RowsAffected() == 0` → 按状态码返回,不写 ledger。
4. 成功后 `SELECT` 回三列,序列化成 `balance_after` JSON,`INSERT INTO quota_ledger`。步骤 3 + 4 包在同一个 `sql.Tx` 里 —— ledger 与余额同生共死。

`Reserve()` 返回的 `reservation_id` 就是这一步生成的 `quota_ledger.id`,是后续 consume / refund 的唯一句柄。

## Cost estimation

MVP 不做真实成本模型,`quotasvc/cost.go` 就一张固定表(`tasks.yaml:76-77`),按 Phase 3 的 `job_type` 一档一价:

| job_type | credits | 触发路由 |
|---|---|---|
| `storyboard` | 1 | `POST /api/video-projects/:id/storyboard` |
| `animatic` | 5 | `POST /api/video-projects/:id/animatic` |
| `sample` | 10 | `POST /api/video-projects/:id/sample` |
| `render` | 50 | `POST /api/video-projects/:id/render` |

未知 `job_type` → 返回 error,handler 转 400。free tier 100 credits ⇒ 一个 tenant 最多 2 次 final render(50 × 2),gate 用例足够。**只有 `render` 在 Phase 4 接了自动 hook**;storyboard / animatic / sample 的价格已定义但路由暂不扣费(见"What's NOT done")。

## Phase 3 render hook — auto-reserve 50 credits

`handlers_project.go` 的 `POST /api/video-projects/:id/render`,在 Phase 3 原有的"校验 status → InsertJob → `go RunJobAsync`"三步之间插入 quota:

1. 校验 `video_projects.status` 允许推进到 `FINAL_RENDERING`(Phase 3 逻辑不变)。
2. `cost := quotasvc.EstimateCost("render")` → 50。
3. `resID, err := quotasvc.Reserve(ctx, db, tenantID, cost, newJobID, userID)`。**失败(402)直接返回,不插 job 行、不起 goroutine** —— 余额不足不能留下半个 job。
4. `InsertJob(...)` 时把 `cost_reserved = 50` 写进 `production_jobs`(Phase 3 已留字段,phase_3/README.md:146 记为"MVP 不写值",Phase 4 补上),`resID` 一并存起来给 runner。
5. `go jobsvc.RunJobAsync(jobID)`,返回 202 + `{job_id, reservation_id}`。
6. runner 走到 `COMPLETED` → `quotasvc.Consume(resID)`,并把 `cost_actual = 50` 回写 `production_jobs`。
7. runner 走到 `FAILED` / `POST /cancel` → `quotasvc.Refund(resID)`,`cost_actual = 0`。

净效果:成功一次 render = `available -50` / `consumed +50`;失败一次 render = 余额回到原数,ledger 里留下 reserve + refund 两行审计。

## How to run manually

```bash
cd /opt/OpenMontage_Voicebox && bash scripts/mvp_dev/phase_4/run.sh --fresh /tmp/phase_4_diff.txt
# gate.sh 自己启动 :18905 并端到端 curl — 不需要先手起服务:
bash scripts/mvp_dev/phase_4/gate.sh
```

`run.sh --fresh /tmp/phase_4_diff.txt` 全流程:`tasks.yaml` READY 守门(`run.sh:23-31`)→ 两张表 schema 迁移 → 写 5 个新 Go 文件 + 改 `cmd/mvp/main.go` 与 `handlers_project.go`(只追加 quota 路由 + render hook,不动既有路由)→ `go build -o /tmp/frameflow-bff-mvp-p4 ./cmd/mvp` → 后台启 `:18905`(`WEIXIN_MOCK_AUTH=1 MVP_PORT=18905`)→ 等 `/healthz` 通。日志落 `logs/mvp_dev/run-phase_4-<timestamp>.log` + `logs/mvp_dev/phase_4-server.log`。

`gate.sh` 对照 `tasks.yaml:56-62` 的 `gate_min_verification` 跑端到端 curl:`GET /api/quota` 看 upsert 出 free tier 100 → `reserve` 后 available 减少 / reserved 增加 → `consume` 后 reserved 减少 / consumed 增加 → 再 reserve + `refund` 看 available 回原数 → `POST /render` 看自动 reserve 50 → 跨 tenant 调 403 → 余额打空后再 reserve 402。每步之间 assert 不变量三列之和 == 100。

通过后退出 0;`orchestrator.sh` 看到 `state/phase_4.json` 24h 内绿就跳过。

> **当前状态**:`run.sh` 与 `gate.sh` 还是 `install_scaffolding.sh` 生成的占位版(`run.sh:39-47`、`gate.sh:16-20` 都是 TODO,无条件 `exit 0`)。`tasks.yaml:5` 已 `status: READY`,所以 orchestrator 会跑到它们 —— 实现 Phase 4 时必须同时把这两个脚本填掉,否则 gate 是假绿。

## What's NOT done in this phase

明确不在 Phase 4 范围内:

- **真实计费系统集成**:没有任何外部 billing / 发票 / 对账链路。credits 纯粹是 SQLite 里的三个 `REAL` 列,充值只能手工 `UPDATE`。
- **Stripe / 微信支付 / 任何支付通道**:不接。`docs/CLAUDE.md` 那套 WeChat Pay V3 网关与本 phase 无关,不共享表、不共享代码。
- **per-user quota**:额度粒度只到 `tenant_id`(`quota_credits` PK)。同一 tenant 下多个 `internal_user_id` 共用一个池子,谁先花谁花掉;ledger 的 `created_by` 只是审计字段,不参与任何判定。
- **tier upgrade flow**:`tier` 列写死 `free`,没有 upgrade / downgrade 端点,没有 tier → grant 的动态映射,没有周期性重置(月初回满)或过期。改 tier 只能手工改库。
- **storyboard / animatic / sample 的自动扣费**:`cost.go` 里价格已定义,但只有 `/render` 挂了 hook(`tasks.yaml:74` 只要求 render 路径)。另外三条 stage 路由 Phase 5+ 补。
- **SQL 视图**:cron plan §2 的估算写的是"1 张表 + 视图",MVP 落成 2 张表 + 0 视图 —— `GET /api/quota` 直接查行,ledger 聚合报表留 Phase 5+。
- **与 `internal/limits` 的桥接**:cron plan §235 点名的重叠项。`limits.Usage`(`frameflow/bff/internal/limits/limits.go:102`)是**进程内内存**的 per-user 并发 + 日配额计数器,与 `quota_credits` 完全独立,两套限流互不知情。Phase 4 不动它,不替换也不桥接。
- **Go 单元测试**:`tasks.yaml:47` `go_tests: []` 显式空。`quotasvc/store_test.go`(并发 reserve 竞态)、`cost_test.go` 留后续。

## Known limitations / risks

- **不变量无 DB 级强制**:`quota_credits` 没有 `CHECK (available_credits >= 0 AND reserved_credits >= 0)`,也没有和为常数的约束。任何绕过 `quotasvc` 的直接 `UPDATE`(运维手改、未来别的 handler)都能让三列之和漂移,只有 reconciliation query 事后能发现。加 `CHECK` 需要 rebuild 表(SQLite 不支持 `ALTER TABLE ADD CONSTRAINT`),留后续。
- **reservation 泄漏,无 TTL 清理**:Phase 3 的 runner 是 in-process `go func`(phase_3/README.md:158),进程重启 = goroutine 丢失 = job 永远卡 `running` = 那 50 credits 永远挂在 `reserved`,既不 consume 也不 refund。没有 sweeper 按 `created_at` 超时自动 refund。这是 Phase 4 最现实的额度流失路径,Phase 5 接 durable queue 时必须一起解决。
- **`REAL` 存 credits**:浮点。当前四档成本都是整数,加减无误差;一旦引入按秒 / 按帧计费的小数单价,累加会有舍入漂移,不变量的 `!=` 比较会假报警。生产应改整型 credits(最小单位分档)。
- **SQLite 单写者**:`MaxOpenConns(1)`(同 `docs/CLAUDE.md` 记录的约束),quota 的 `UPDATE` 与 Phase 3 runner 的 job status `UPDATE` 抢同一条连接,百级并发下排队。好处是 atomic `UPDATE` 的竞态在这个配置下天然不可能触发 —— 也意味着**并发正确性没有被真正验证过**,换 Postgres / 放开连接数时需要重跑竞态测试。
- **ledger 只增不改,但可被绕过**:幂等靠"查 ledger 有没有后续 consume/refund 行"。这个查询 + `UPDATE` 在同一个 `sql.Tx` 里,单写者下安全;多写者下需要 `SELECT ... FOR UPDATE` 等价物,SQLite 没有,得靠 `BEGIN IMMEDIATE`。
- **`tier` 是标签不是额度**:`tier_grant` 常量在 Go 里,行里的 `tier` 只是字符串。两边不同步(比如手工把 `tier` 改成 `pro` 但 Go 没这个档)时,reconciliation 会全表假报警。
- **无 FK**:`quota_credits.tenant_id` / `quota_ledger.job_id` 都不约束到 `tenants` / `production_jobs`,删 tenant 会留孤儿额度行(同 Phase 1/2/3 的既有取舍)。
