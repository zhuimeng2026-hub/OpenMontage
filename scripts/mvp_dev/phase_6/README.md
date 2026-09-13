# Phase 6 — 实际预览渲染接入 MCP

`docs/openmontage_product_video_mvp_golang_cron_plan_2026-08-30.md` 第 10 节
明确说 §21 OpenClaw/Hermes + §22 MCP 薄封装「不在 §17 MVP 五阶段范围内,
Phase 5 收尾后单独排期」。本 phase 是那个排期。

## 范围

- `frameflow/bff/internal/mvpclient/` 新建薄包装,封装 `mcp.Client` 到 4 个
  `Prepare*` 方法(storyboard / animatic / sample / render)。
- `frameflow/bff/internal/jobsvc/runner.go` 全文重写,接 mvpclient + Poller,
  失败时 Refund 配额(render 这一档)。
- `frameflow/bff/cmd/mvp/handlers_project.go` `StartStage` 改异步:
  立即返回 `*_RENDERING + job_id`,后台 goroutine 推 MCP + 落 artifacts。
- `frameflow/bff/cmd/mvp/db.go` 加 `ALTER TABLE production_jobs ADD COLUMN artifacts_json`。
- `frameflow/bff/cmd/mvp/main.go` 从 `MCP_BASE_URL` / `MCP_API_TOKEN` 初始化 mvpclient。

## 行为变化

| 端点 | Phase 5 行为 | Phase 6 行为 |
|---|---|---|
| `POST /api/video-projects/:id/storyboard` | 200 `status=STORYBOARD_READY` 同步 | 200 `status=STORYBOARD_READY async=true`(项目已在此状态;job 异步跑 MCP 落 artifacts) |
| `POST /api/video-projects/:id/animatic` | 200 同步 succeeded | 200 `status=ANIMATIC_RENDERING async=true`,轮询到 `ANIMATIC_READY` |
| `POST /api/video-projects/:id/sample` | 同上 stub | 200 `status=SAMPLE_RENDERING async=true`,轮询到 `SAMPLE_READY` |
| `POST /api/video-projects/:id/render` | 200 同步 succeeded + quota consume | 200 `status=FINAL_RENDERING async=true`,失败 `Refund(50)`,成功 `Consume(50)` |
| `GET /api/jobs/:job_id` | 仅基本字段 | 多 `external_run_id` + `artifacts_json` |

## Gate 验证

`gate.sh` 启 stub MCP server(端口 18910)模拟 video_compose 应答,
再启 BFF(:18907 + `MCP_BASE_URL=http://127.0.0.1:18910/mcp`),
跑 7 个测试:setup / 4 个 stage / render-fail-refund / cross-tenant-403 /
fail-loud-503。

stub render **故意失败**,用来验证 `Refund(50)` 路径;其他 3 个 stage
返回成功 artifact(JSON 形状按 scope §23)。

## 运行

```bash
# 一键跑
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/orchestrator.sh --only 6

# 仅 gate
cd /opt/OpenMontage_Voicebox/frameflow/bff
MCP_BASE_URL=http://127.0.0.1:18910/mcp MCP_API_TOKEN=t \
    WEIXIN_MOCK_AUTH=1 /tmp/frameflow-bff-mvp-p6 &
nohup /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_6/mcp_stub_server.py 18910 &
bash /opt/OpenMontage_Voicebox/scripts/mvp_dev/phase_6/gate.sh
```

## 故障排查

| 症状 | 检查 |
|---|---|
| `build FAILED` | `cd frameflow/bff && go build ./cmd/mvp` 看具体错误 |
| `/healthz` 不通 | `tail logs/mvp_dev/phase_6-server.log` |
| stub 不响应 | `tail logs/mvp_dev/phase_6-stub.log` + `curl http://127.0.0.1:18910/mcp` 测一下 |
| `artifacts_json` 空 | 看 BFF server log 里 `[mcp-http] response` 行,确认 stub 返回的 JSON 含 `artifact` 字段 |
| render 失败但 quota 没退 | 看 `quota_ledger` 表里有没有 `refund` 行 |
| 想断 MCP 试 503 | `MCP_BASE_URL=http://127.0.0.1:1/mcp /tmp/frameflow-bff-mvp-p6`;`mvpclient.New` 在启动 handshake 时失败,启动时打 WARN,但进程不挂;启动后 stage 端点返回 503(因为 `MCP == nil` 触发 handler guard)|

## 不在本 phase

- §22 的 `prepare_*` 薄封装 MCP tool(现有 `video_compose` 已覆盖)。
- OpenClaw/Hermes skill 本身的实现(由 MCP 侧决定,BFF 不动)。
- `WAITING_APPROVAL` 状态切换(留待 phase 7+ 加 `approve` 端点)。