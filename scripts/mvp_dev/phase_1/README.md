# Phase 1 — §17.B + §17.H — 多租户 + 文件权限

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.1 (§17.B + §17.H)
上游 gate:Phase 0 全绿(§17.A 微信身份已落地,JWT 携带 `internal_user_id`)

## TL;DR

Phase 1 把"谁属于哪个租户"和"谁能下载/预览哪个文件"这两条 SaaS 横切关注点落到中间件 + signed URL 层。所有改动在 **独立 binary `cmd/mvp/`**(默认 `:18902`),不碰生产 BFF `main.go`(端口 8900);同 Phase 0 模式。新增 3 张表(`tenants` / `tenant_users` / `file_acl`)、2 个中间件、1 个签名/ACL 包、5 条新路由。

## §17 sections implemented

### §17.B — 多租户

`tenants` 表承载租户实体;`tenant_users` 是 `(user_id, tenant_id, role)` 多对多桥表,`role` 为 `owner` / `member`。`TenantScope` 中间件读 `X-Tenant-Id` header,在 `tenant_users` 里 join 当前 `internal_user_id`(由 `RequireJWT` 写入 gin.Context),命中则把 `tenant_id` + `role` 灌进 ctx,未命中按"缺 header → 401 / 非成员 → 403"区分。设计取舍:`tenant_id` 不进 JWT(用户可能跨多租户),每次调用显式 header 选定;这意味着 JWT 一签发就稳定,加/退租户不需要换 token。

### §17.H — 文件权限

`file_acl` 表把 `file_key` 绑到单一 `tenant_id`(由 Phase 2+ 上传端点写入)。签发链路 `GET /api/files/sign?key=<file_key>`:要求 JWT + `X-Tenant-Id`,校验调用方所属租户与 `file_acl.tenant_id` 一致后,产出 HMAC-SHA256 签名 URL(`exp` + `sig`)。下载链路 `GET /api/files/:key?exp&sig`:无需 JWT,签名本身即授权;`filesvc.Verify` 校验 HMAC + 未过期,然后二次查 `file_acl`(防 row re-bind 飞行中被换绑)。Secret 优先级 `FILESIGN_SECRET > JWT_SECRET > MVP_DEV_SEED`,为将来独立 rotate 留口子。

## Files created / modified

| File | Created / Modified | Purpose |
|---|---|---|
| `frameflow/bff/internal/middleware/auth.go` | created | `RequireJWT(jwtSvc)` — 薄封装 `JWTAuthMiddleware` |
| `frameflow/bff/internal/middleware/tenant.go` | created | `TenantScope(db)` — X-Tenant-Id + tenant_users 校验 |
| `frameflow/bff/internal/filesvc/signed.go` | created | `SignURL` / `Verify` / `SecretBytes` — HMAC-SHA256 签名 |
| `frameflow/bff/internal/filesvc/store.go` | created | `Register` / `LookupTenant` / `CountByTenant` — file_acl 读写 |
| `frameflow/bff/cmd/mvp/handlers_tenant.go` | created | `TenantHandler`: Create / ListMine / AddMember |
| `frameflow/bff/cmd/mvp/handlers_file.go` | created | `FileHandler`: Sign / Serve |
| `frameflow/bff/cmd/mvp/main.go` | modified | 挂载 jwtOnly + scoped + public serve 三组路由 |
| `frameflow/bff/cmd/mvp/db.go` | modified | (Phase 0 已存在;Phase 1 不改 schema bootstrap,迁移在 run.sh 跑) |

## Schema migrations

由 `phase_1/run.sh` step 1 用 `sqlite3` 应用到 `${BFF}/data/frameflow.db`。三条 `CREATE TABLE IF NOT EXISTS`,所有索引 `IF NOT EXISTS`,幂等:

```sql
CREATE TABLE IF NOT EXISTS tenants (
  id          TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'active',
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tenants_created_by ON tenants(created_by);

CREATE TABLE IF NOT EXISTS tenant_users (
  tenant_id   TEXT NOT NULL,
  user_id     TEXT NOT NULL,
  role        TEXT NOT NULL DEFAULT 'member',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (tenant_id, user_id)
);
CREATE INDEX IF NOT EXISTS idx_tenant_users_user ON tenant_users(user_id);

CREATE TABLE IF NOT EXISTS file_acl (
  file_key      TEXT PRIMARY KEY,
  tenant_id     TEXT NOT NULL,
  uploaded_by   TEXT NOT NULL,
  media_type    TEXT NOT NULL DEFAULT 'image',
  created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_file_acl_tenant ON file_acl(tenant_id);
```

迁移 source-of-truth 在 `scripts/mvp_dev/phase_1/tasks.yaml` 的 `sql_migrations` 段;`run.sh` 用 here-doc 复制。`tenants.created_by` 与 `tenant_users.user_id` 都是 `internal_user_id`(hex 串),不走外键约束(SQLite + 跨 Phase 增量迁移,加 FK 风险高,留给 Phase 4 收尾)。

## Routes

`frameflow/bff/cmd/mvp/main.go` 挂载,运行在 `:18902`。

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/tenants` | JWT | 创建租户,创建者自动 owner |
| GET | `/api/tenants` | JWT | 列出当前用户所属的全部租户 |
| POST | `/api/tenants/:id/members` | JWT + `X-Tenant-Id`(owner) | 添加成员(`role` 默认 `member`) |
| GET | `/api/files/sign?key=<file_key>[&ttl_seconds=N]` | JWT + `X-Tenant-Id` | 签发带 TTL(默认 300s,上限 3600s)的下载 URL |
| GET | `/api/files/:key?exp=<unix>&sig=<hex>` | signature only | 凭签名拿文件(MVP 仅返元数据 placeholder) |

`/api/auth/login`、`/api/me/jwt`、`/healthz` 来自 Phase 0,保留。

## Middleware chain

`RequireJWT` 与 `TenantScope` 必须按顺序串 — `TenantScope` 依赖 `RequireJWT` 写入的 `internal_user_id`(`internal/middleware/tenant.go:18` 注释也强调)。`main.go` 把路由分成 3 个 group:

```go
// cmd/mvp/main.go:57-70
jwtOnly := api.Group("")
jwtOnly.Use(middleware.RequireJWT(jwtSvc))
jwtOnly.POST("/tenants", tenants.Create)
jwtOnly.GET("/tenants", tenants.ListMine)

scoped := api.Group("")
scoped.Use(middleware.RequireJWT(jwtSvc))
scoped.Use(middleware.TenantScope(db))
scoped.POST("/tenants/:id/members", tenants.AddMember)
scoped.GET("/files/sign", files.Sign)

api.GET("/files/:key", files.Serve)   // 签名授权,不要 JWT
```

请求 `POST /api/tenants/:id/members` 的实际行为:

1. `RequireJWT` 解 `Authorization: Bearer ...` → 校验 HS256 签名 → 写 `internal_user_id` 到 ctx → 失败 401。
2. `TenantScope` 读 `X-Tenant-Id` header → 缺 401;查 `tenant_users WHERE tenant_id=? AND user_id=?` → 无行 403,命中写 `tenant_id` + `role` 到 ctx。
3. handler `AddMember` 再查一次 `tenant_users` 确认 `role='owner'`(因为 `TenantScope` 已经保证是 member,所以这一步是 owner-check),通过后 `INSERT OR IGNORE`。

`:id` 路径参数(URL 上的 `tn_xxx`)与 header 上的 `X-Tenant-Id` 必须一致,否则越权 — handler 目前不强制校验,留给 Phase 2+ 加 invariant。

## Signed URL format

`GET /api/files/:key?exp=<unix_seconds>&sig=<hex_hmac_sha256>`

```text
# 示例:file_key = "t1-asset-001", ttl = 300s
# exp   = ceil(now + 300s) unix seconds
# sig   = hex(HMAC_SHA256(secret, "t1-asset-001" + ":" + exp))

GET /api/files/t1-asset-001?exp=1756529493&sig=9f3a1c...e8b2
```

`sig` 计算(`internal/filesvc/signed.go:63`):

```text
m = HMAC_SHA256(secret, file_key || ":" || exp_ascii_decimal)
sig = hex.EncodeToString(m)
```

`secret` 来自 `SecretBytes()`(`FILESIGN_SECRET` → `JWT_SECRET` → dev seed)。`Verify` 既校验 HMAC(`hmac.Equal` 常时)又校验 `time.Now().Unix() < exp`,两步都过才放行。下载端点对 signature 不命中仍会查 `file_acl` —— 防 file_key 被 `Register` 重新绑定到别的 tenant 后,旧 URL 仍可拉。

## How to run manually

```bash
cd /opt/OpenMontage_Voicebox/scripts/mvp_dev && bash phase_1/run.sh --fresh
bash phase_1/gate.sh
```

`run.sh --fresh` 全流程:tasks.yaml READY 守门 → schema 迁移 → 写入 6 个新 Go 文件 → 重写 `cmd/mvp/main.go` → `go build -o /tmp/frameflow-bff-mvp-p1 ./cmd/mvp` → 后台启 `:18902`(`WEIXIN_MOCK_AUTH=1 MVP_PORT=18902`) → 等 `/healthz` 通 → 调 `gate.sh` → 杀进程。日志落 `logs/mvp_dev/run-phase_1-<timestamp>.log` + `logs/mvp_dev/phase_1-server.log`。

`gate.sh`(`phase_1/gate.sh`)对照 `tasks.yaml` 的 `gate_min_verification`:

- 跨 tenant 调用任何资源接口 → 403
- 无 JWT 或无 `X-Tenant-Id` header → 401
- signed URL 过期/篡改/跨租户访问 → 403
- 同租户 + 合法签名 → 200

通过后 `run.sh` 退出 0;`orchestrator.sh` 看到 `state/phase_1.json` 24h 内绿就跳过。

## What's NOT done in this phase

明确推迟到 Phase 2+:

- **`products` / `product_assets` / `product_manifest` 表 + 上传 + AI 分类**(Phase 2 — §17.C)
- **`video_projects` / `production_jobs` / `preview_jobs` / `render_jobs` + 13 档状态机**(Phase 3 — §17.D)
- **`quota` 视图 + reserve / consume 语义**(Phase 4 — §17.E)
- **Agent Gateway 8 个业务动词(analyze / storyboard / animatic / sample / render / cancel / status)**(Phase 5 — §17.F)
- **OM MCP 状态码 → 统一 13 档的 mapping table**(Phase 5 — §17.G)
- **真实文件字节**:`Serve` 当前只返元数据 placeholder(`handlers_file.go:97-103`);对象存储接入留在 Phase 2
- **§19 的 24 个具体端点 handler**:路由壳搭好,业务逻辑在 Phase 5 之后单独排期(cron plan §10 明确)
- **`signed_test.go` / `tenant_test.go` Go 单元测试**:`tasks.yaml` 列了文件名,实际测试代码在 Phase 2+ 收尾阶段补
- **`file_key` 字符集白名单校验**:`tasks.yaml` notes 提到 `[A-Za-z0-9_-]+` 长度 8-128,目前只在 gate seed 里硬编码 2 个,handler 没强制
- **删除租户 / 转让 owner / 角色降级**:`tenant_users` 目前只支持 add,owner 退出场景未处理
- **限流 / 配额 / 审计日志**:§17.E 范围
- **WeChat OAuth 跳转路由**:Phase 0 已经存在,Phase 1 不动

## Known limitations / risks

- **签名 URL 跨租户 replay 风险**:`FILESIGN_SECRET` 全局共享(目前没做 per-tenant 签名密钥),secret 一旦泄露,任意持有者都能签任意 `file_key`。`Serve` 端二次查 `file_acl` 阻止了"伪造 sig 拉跨租户文件",但**挡不住"用合法 secret 主动签出再分享"**。Phase 2+ 应引入 per-tenant 派生 secret(例如 `HKDF(master, tenant_id)`)。
- **`X-Tenant-Id` 与 path `:id` 不一致**:handler 不强制校验这两个值相等,意味着越权者可"借"自己有 membership 的 tenant A 的 header,调 `POST /api/tenants/<B>/members`。`AddMember` 只校验调用方是 URL tenant 的 owner,所以实际安全;但 `GET /api/files/sign?key=...` 在未来 phase 可能受同样模式影响 —— 收尾 invariant 时必须明确"header = path"。
- **Go 单元测试缺失**:`internal/filesvc/signed_test.go` / `internal/middleware/tenant_test.go`(`tasks.yaml` `go_tests:` 段)未落地,gate 完全靠 HTTP 端到端 curl,无白盒回归保护。下次改签名逻辑前必须先补测试。