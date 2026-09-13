# Phase 0 — §17.A — 微信身份

配套计划:`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` §2

## 范围

实现 §17.A 列出的微信身份能力:

- 微信登录(code → openid → JWT);
- openid / unionid 持久化;
- session/token 签发与校验;
- 内部 `internal_user_id`(供 §17.B 多租户 / §17.D Project/Job 用)。

## 与现有 `/api/wechat/*` 的关系

现有 `frameflow/bff` 已有的路由(GET 跳转流)—— `wechat/login`、`wechat/callback`、`wechat/qrlogin`、`wechat/qrlogin/status` —— 全部保留,**Phase 0 不动**。

Phase 0 新增的是文档 §19 推荐的 **`POST /api/auth/login`**(静默登录)— 供 mini-program 与后端调用。两套并存,OAuth 网页跳转用户继续走老路由。

## Gate 最小验证

`POST /api/auth/login` 用合法 code 拿 JWT;`GET /api/me` 带 JWT 返回 `user_id` + `internal_user_id`。

## 开工步骤

1. 编辑 `tasks.yaml`:
   - 把 `status: STUB` 改成 `status: READY`
   - 复核 `files_to_create` / `files_to_modify` / `sql_migrations` / `go_tests` 是否齐全
2. 编辑 `run.sh` — 把 TODO 段替换成实际 schema / handler 改动:
   - `sqlite3 data/state.db < store/migrations/0001_auth.sql`(或对应 psql)
   - 写 `internal/auth/jwt.go` + `internal/auth/wechat.go` + `internal/middleware/auth.go`
   - 改 `handlers/auth.go` 加 `Login` / 扩 `Me`
   - 改 `main.go` 挂载 `api.POST("/auth/login", h.Login)`
   - 跑 `go test ./internal/auth/... -count=1`
3. 编辑 `gate.sh` — 把 TODO 段替换成实际 curl 校验
4. 干跑:
   ```bash
   bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_0/run.sh --fresh
   bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_0/gate.sh
   ```
5. 通过后:
   ```bash
   bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh --only 0
   ```

## 实现注意

- `JWT_SECRET` 从 env 读,缺省随机生成(重启即失效)仅用于 dev。
- JWT payload 字段:**只放 `internal_user_id`**;`openid` / `unionid` 通过 `UserIdentity` 表按需查 — 这样 openid 改绑不影响 token。
- `internal_user_id` 必须全局唯一(给 §17.B 多租户做 join 键)。
- 不要在 token 里放 PII(昵称、手机号)— 这些走 `GET /api/me` 即时查表。

## 上游 / 下游

- **上游**:无(Phase 0 是入口)。
- **下游**:Phase 1(B 多租户)需要 `internal_user_id` 作为 `tenant_users.user_id` 外键。
