# Phase 2 — §17.C — Product / Asset 管理

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2
范围文档:`docs/openmontage_product_video_mvp_golang_scope.md` §17.C
上游 gate:Phase 0 + Phase 1 全绿(§17.A JWT 携带 `internal_user_id`,§17.B `TenantScope` 写入 ctx,§17.H `file_acl` 已就绪)

## TL;DR

Phase 2 把"一个商品 + 它的素材 + AI 分类结果 + 人工修正"这块 SaaS 数据模型落到 3 张表 + 6 条路由。所有改动在 **独立 binary `cmd/mvp/`**(默认 `:18903`,与 Phase 1 `:18902` 同形),不碰生产 BFF `main.go`(端口 8900)。AI 分类不接 M3 / 视觉模型,走 MVP 文件名启发式 + 手工修正。

## §17.C — Product/Asset

`products` 是租户内商品实体;`product_assets` 一对多挂载,每行带 `role`(`unclassified` / `hero_front` / `detail` / `lifestyle` / ...)+ `quality_score`(REAL,默认 0.5);`product_manifests` 每次 assets 变化追加新行(`version` 单调递增),保留版本历史,前端只读最新一行(读路径 `?version=` 可选,默认 `MAX(version)`)。上传走 `multipart/form-data`(`file` 字段),bytes 落 `${MVP_UPLOAD_DIR:-/tmp/mvp_uploads}/<file_key>` 占位 + 同时 register 进 Phase 1 的 `file_acl`(等同"上传即绑租户")。所有路由挂在 scoped group —— `RequireJWT` + `TenantScope` 已经把 `tenant_id` / `internal_user_id` 写进 ctx,handler 直接消费。

## Files created / modified

| File | Created / Modified | Purpose |
|---|---|---|
| `frameflow/bff/internal/productsvc/store.go` | created | `Product` / `Asset` / `Manifest` DB CRUD + `file_acl.Register` 联动 |
| `frameflow/bff/internal/productsvc/manifest.go` | created | `BuildManifest(productID)` — 聚合当前 assets + 算 `missing_roles` |
| `frameflow/bff/internal/productsvc/classify.go` | created | MVP 启发式分类(文件名 → role,`quality_score=0.5` 默认) |
| `frameflow/bff/cmd/mvp/handlers_product.go` | created | 6 条路由的 HTTP handler(Create / GetProduct / UploadAsset / ListAssets / GetManifest / PutAssetClassification) |
| `frameflow/bff/cmd/mvp/main.go` | modified | 挂载 6 条产品路由到 `scoped` 分组(不动 Phase 0/1 路由) |

## Schema migrations

由 `phase_2/run.sh` step 1 用 `sqlite3` 应用到 `${BFF}/data/frameflow.db`(同 Phase 0/1 数据库)。三条 `CREATE TABLE IF NOT EXISTS`,所有索引 `IF NOT EXISTS`,幂等:

```sql
CREATE TABLE IF NOT EXISTS products (
  id          TEXT PRIMARY KEY,
  tenant_id   TEXT NOT NULL,
  name        TEXT NOT NULL,
  category    TEXT NOT NULL DEFAULT 'general',
  sku         TEXT NOT NULL DEFAULT '',
  created_by  TEXT NOT NULL,
  created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_products_tenant ON products(tenant_id);
CREATE INDEX IF NOT EXISTS idx_products_tenant_name ON products(tenant_id, name);

CREATE TABLE IF NOT EXISTS product_assets (
  id               TEXT PRIMARY KEY,
  tenant_id        TEXT NOT NULL,
  product_id       TEXT NOT NULL,
  file_key         TEXT NOT NULL,
  media_type       TEXT NOT NULL DEFAULT 'image',
  role             TEXT NOT NULL DEFAULT 'unclassified',
  quality_score    REAL NOT NULL DEFAULT 0.5,
  ai_metadata_json TEXT NOT NULL DEFAULT '{}',
  uploaded_by      TEXT NOT NULL,
  created_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_assets_product ON product_assets(product_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_product_assets_file_key ON product_assets(file_key);

CREATE TABLE IF NOT EXISTS product_manifests (
  id                 TEXT PRIMARY KEY,
  product_id         TEXT NOT NULL,
  version            INTEGER NOT NULL DEFAULT 1,
  assets_json        TEXT NOT NULL DEFAULT '[]',
  missing_roles_json TEXT NOT NULL DEFAULT '[]',
  ai_model           TEXT NOT NULL DEFAULT 'mvp_heuristic_v1',
  created_at         TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_product_manifests_product ON product_manifests(product_id, version);
```

迁移 source-of-truth 在 `scripts/mvp_dev/phase_2/tasks.yaml` 的 `sql_migrations` 段;`run.sh` 用 here-doc 复制。所有 `tenant_id` / `product_id` / `file_key` / `internal_user_id` 仍不走 FK(同 Phase 1 notes:SQLite + 跨 Phase 增量迁移,Phase 4 收尾)。

## Routes

`frameflow/bff/cmd/mvp/main.go` 挂载,运行在 `:18903`。

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/api/products` | JWT + `X-Tenant-Id` | 创建商品;返回 `product_id` |
| GET | `/api/products/:id` | JWT + `X-Tenant-Id` | 读商品元数据(403 if 非同 tenant) |
| POST | `/api/products/:id/assets` | JWT + `X-Tenant-Id` | 上传素材(multipart `file` 字段);落 `/tmp/mvp_uploads/` + register `file_acl` + 跑分类 + 重建 manifest |
| GET | `/api/products/:id/assets` | JWT + `X-Tenant-Id` | 列出当前 product 全部 assets |
| GET | `/api/products/:id/manifest` | JWT + `X-Tenant-Id` | 读最新 manifest(`role` + `quality_score` 必须可见) |
| PUT | `/api/products/:id/manifest/:asset_id` | JWT + `X-Tenant-Id` | 手工修正某 asset 的 `role` + `quality_score`;再触发 `BuildManifest` |

`/api/auth/login`、`/api/me/jwt`、`/api/tenants/*`、`/api/files/sign`、`/healthz` 来自 Phase 0 + Phase 1,保留不动。

## MVP classification heuristic

`productsvc/classify.go` 用纯文件名匹配,**不调 M3 / 任何视觉模型**(Phase 5+ 才接 Agent Gateway 时一起替换)。优先级按表从上到下,首次命中即返回;命中失败走默认行。请求体里的 `role` + `quality_score` 字段显式传值可覆盖(等同"用户标注优先级更高"):

| 文件名子串(lower-case 匹配) | 产出 `role` | 产出 `quality_score` |
|---|---|---|
| `hero` | `hero_front` | 0.85 |
| `detail` | `detail` | 0.80 |
| `lifestyle` | `lifestyle` | 0.75 |
| 其他 / 无匹配 | `unclassified` | 0.50 |

`file_key` 生成规则 `pa_<16hex>`(`productsvc/store.go` 内),与 Phase 1 `tn_<...>` / `t1-asset-...` 命名空间不冲突。

## Manifest version semantics

每次 `product_assets` 集合变化(insert / classify / `PUT manifest/:asset_id`)触发 `BuildManifest`(`productsvc/manifest.go`):

1. 读当前 product 全部 assets → 序列化到 `assets_json`(每行含 `asset_id` / `file_key` / `role` / `quality_score`)。
2. 算 `missing_roles_json` = `固定 11 role 集合` \ `已有 role 集合`。
3. 取当前 `MAX(version)` → +1,插一行新 `product_manifests`(`ai_model='mvp_heuristic_v1'` 默认)。

`product_manifests` 是 append-only 历史表,前端用 `GET manifest`(默认 `MAX(version)`)。`PUT manifest/:asset_id` 不删旧 manifest 行 —— 历史可追;Phase 3+ 接 OM 时再让 OM 按 version 读某张快照。

`fixed_roles` 集合(11 项):`hero_front` / `hero_45` / `side` / `back` / `open_view` / `inside` / `wheel_detail` / `handle_detail` / `zipper_detail` / `logo` / `lifestyle`(取自 `openmontage_product_video_mvp_golang_scope.md` §4.1)。

## How to run manually

```bash
cd /opt/OpenMontage_Voicebox && bash scripts/mvp_dev/phase_2/run.sh --fresh /tmp/phase_2_diff.txt
# gate.sh 会自己启动 :18903 并端到端 curl — 不需要先手起服务:
bash scripts/mvp_dev/phase_2/gate.sh
```

`run.sh --fresh /tmp/phase_2_diff.txt` 全流程:tasks.yaml READY 守门 → 三张表 schema 迁移 → 写 4 个新 Go 文件 + 改 `cmd/mvp/main.go`(只追加 product 路由,不动既有路由) → `go build -o /tmp/frameflow-bff-mvp-p2 ./cmd/mvp` → 后台启 `:18903`(`WEIXIN_MOCK_AUTH=1 MVP_PORT=18903`) → 等 `/healthz` 通。日志落 `logs/mvp_dev/run-phase_2-<timestamp>.log` + `logs/mvp_dev/phase_2-server.log`。

`gate.sh` 对照 `tasks.yaml` 的 `gate_min_verification` 跑端到端 curl(占位版本只校验 `status: READY`,真实 HTTP gate 在 implementation 落盘后补):创建 product → 上传一张图 → 读 manifest → 跨 tenant 调 → 走 PUT 修正后 manifest version+1。

通过后退出 0;`orchestrator.sh` 看到 `state/phase_2.json` 24h 内绿就跳过。

## What's NOT done in this phase

明确推迟到 Phase 3+:

- **`video_projects` / `production_jobs` / `preview_jobs` / `render_jobs` + 13 档状态机**(Phase 3 — §17.D)
- **`quota` 视图 + reserve / consume 语义**(Phase 4 — §17.E)
- **Agent Gateway 8 个业务动词(analyze / storyboard / animatic / sample / render / cancel / status)**(Phase 5 — §17.F)
- **OM MCP 状态码 → 统一 13 档 mapping table**(Phase 5 — §17.G)
- **真实视觉模型调用(MiniMax M3)**:当前 `classify.go` 是文件名启发式。计划文档 §29 明确这是 Phase 5+ 接 Agent Gateway 时一起替换的事,Phase 2 gate 不依赖。
- **真实 OpenMontage MCP 集成**:manifest 仍然是 Gin 内部 JSON,不调 OM,不创建 `om_project_id`。
- **Creative Brief / 参考视频 / 3 层 Preview 入库字段**:`products` 表当前只有 `category` / `sku`,`creative_brief_json` / `reference_file_key` 等都在 Phase 3 的 `video_projects` 表里。
- **`POST /api/products/:id/analyze-assets` 重新分析触发端点**:`tasks.yaml` 的 `gate_endpoints` 没列(scope §17.C 只是 CRUD + 修正),前端当前每次上传自动重算,显式 re-analyze 留 Phase 5+。
- **删除 product / 删除 asset / asset 替换**:`tasks.yaml` 没列 DELETE 路由,只做 PUT 修正。`idx_product_assets_file_key` UNIQUE 阻碍同 file_key 跨 product 复用,Phase 3+ 决定要不要松。
- **Go 单元测试**:`tasks.yaml` `go_tests: []` 显式声明占位,`internal/productsvc/classify_test.go` 等留 Phase 4 收尾。
- **限流 / 配额 / 审计日志**:§17.E 范围。

## Known limitations / risks

- **MVP 启发式非 AI**:文件名叫 `hero.jpg` 就归类成 `hero_front`,实际上可能是模特图。`unclassified` + `quality_score=0.5` 是大量产物的常态,误判要靠 PUT 手工修正兜底。Phase 5+ 接 M3 后这层只是 cache 兜底。
- **上传 bytes 落 `/tmp` 而非对象存储**:`MVP_UPLOAD_DIR` 默认 `/tmp/mvp_uploads/`(`/opt/OpenMontage_Voicebox/frameflow/bff/cmd/mvp/main.go` env 读),容器重启 / 磁盘满 / 多副本扩展都会丢文件或不一致。`file_acl` 已经把 `file_key` 绑 tenant 了,所以接 S3/OSS 时只换 `Store` 内部实现 + 不动 ACL 语义。
- **无 asset dedup**:`idx_product_assets_file_key` 是 UNIQUE —— 同 file_key 二次上传会被静默拒绝(SQLite `UNIQUE constraint failed`),但没有内容 hash 比对,用户把同一张图改文件名再传就会建两条 asset 行,manifest version 也会 +1。Phase 3+ 加 perceptual hash dedup。
- **Manifest 重建在 handler 内同步跑**:`BuildManifest` 是纯 SQLite 读 + JSON 序列化,百行 assets 级别无问题;万行 product + 并发 PUT 时会争 `MAX(version)`(没有事务隔离,可能产生相同 version 的两行)。Phase 3+ 接 OM 后必须加 `BEGIN IMMEDIATE` / 唯一约束 `(product_id, version)`。
