# Phase 7 — WAITING_APPROVAL + approve 端点

把 §17.D 状态机的最后一段补全:**用户明确 approve 之后才能进 render**。
Phase 0-6 跳过了这个步骤 — sample 完就直接进 render。

## 范围

- `internal/jobsvc/states.go` 加 `approve` 触发器,`SAMPLE_READY → WAITING_APPROVAL`(幂等:再 approve 仍是 WAITING_APPROVAL)。
- `cmd/mvp/db.go` 加 `ALTER TABLE video_projects ADD COLUMN approved_by / approved_at`。
- `cmd/mvp/handlers_project.go` 加 `Approve(c)` handler,改 `projectRow` + `loadProject` 多读两个字段。
- `cmd/mvp/main.go` 挂 `POST /api/video-projects/:id/approve`。
- `scripts/mvp_dev/phase_7/` 配套脚本,stub MCP 加 `--succeed-render` flag。

## API

```
POST /api/video-projects/:id/approve
Authorization: Bearer <jwt>
X-Tenant-Id: <tenant>

→ 200 {"project_id":"...","status":"WAITING_APPROVAL","approved_by":"iu_...","approved_at":"2026-..."}
→ 409 {"error":"illegal transition from CREATED via approve","allowed_from":["SAMPLE_READY","WAITING_APPROVAL"],"current_status":"CREATED"}
→ 403 cross-tenant
→ 404 project not found
→ 401 missing identity
```

`approved_by` = `internal_user_id`(不是 openid),`approved_at` = `datetime('now')` UTC。

## 状态机

```
sample_done:    SAMPLE_RENDERING   →  SAMPLE_READY          (Phase 3 已存在)
approve:        SAMPLE_READY       →  WAITING_APPROVAL      (Phase 7 新增)
                WAITING_APPROVAL   →  WAITING_APPROVAL      (幂等)
render:         WAITING_APPROVAL   →  FINAL_RENDERING       (Phase 3 已允许)
                SAMPLE_READY       →  FINAL_RENDERING       (跳过 approve 仍允许,便于自动化)
```

## Gate 覆盖

- approve-from-CREATED → 409
- 完整链路 storyboard → animatic → sample → SAMPLE_READY → approve → WAITING_APPROVAL → render → COMPLETED
- approve 幂等(从 WAITING_APPROVAL 再 approve 仍 200)
- DB 持久化 `approved_by` / `approved_at`
- quota Reserve + Consume(无 Refund)
- 跨 tenant approve → 403

## 运行

```bash
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh --only 7
```

## 不在本 phase

- 多步 approve(reject / re-approve 不同 sample 版本)— 当前 approve 是单次幂等
- approve 后回滚到 SAMPLE_READY 的 cancel-approve — 留待 OpenClaw 编排层做
- plan §22 的 `prepare_*` MCP 薄封装 — 显式不做,用 `video_compose` 直覆盖
- OpenClaw skill 编排本身 — MCP 侧的责任

## 故障排查

| 症状 | 检查 |
|---|---|
| build FAILED | `cd frameflow/bff && go build ./cmd/mvp` 看错误 |
| approve 返回 409 | 当前状态不对;只接受 SAMPLE_READY 或 WAITING_APPROVAL |
| approved_by 为空 | 看 `sqlite3 frameflow.db "SELECT approved_by FROM video_projects"` — schema 迁移是否成功 |
| render 不走通 | stub `--succeed-render` flag;`phase_7-stub.log` 看请求 |