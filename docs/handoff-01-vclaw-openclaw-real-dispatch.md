# 任务交接 01：VClaw 到 OpenClaw 的正式调度协议与实现

> 面向对象：接手实现的 LLM / 工程师  
> 日期：2026-09-04  
> 目标仓库：`C:\vclaw`  
> 状态：待实现；当前正式入口会安全返回 501，不代表端到端已经接通。

## 1. 任务目标

把 VClaw 当前的“素材包版本已冻结，但 OpenClaw 尚未调用”状态，改造成可恢复、可审计、幂等的真实异步调度链路：

```text
VClaw GUI
  -> VClaw Go API
  -> VClaw job_queue / production_jobs
  -> OpenClaw Runtime
  -> OpenMontage
  -> OpenClaw 回报状态与工件
  -> VClaw 查询统一状态
```

本任务只负责 VClaw 与 OpenClaw 的边界。不得在 Go 控制面里加入视频分析、镜头决策或渲染逻辑。

## 2. 当前代码事实

实施前必须先阅读并以代码为准：

- `C:\vclaw\internal\handler\remix_package.go`
  - `PUT /api/video-projects/:id/remix-package` 已保存不可变版本。
  - `POST /api/studio/video-projects/:id/render` 当前由 `RemixRenderDispatchHandler` 校验项目和 `package_version`，随后返回 `501 OPENCLAW_DISPATCH_NOT_CONFIGURED`。
- `C:\vclaw\cmd\worker\main.go`
  - `startProduction` 已有队列消费框架。
  - `dispatchToOpenClaw` 仍返回 `sim-*` 和 `om-sim-*`，这是必须删除的模拟实现。
- `C:\vclaw\openclaw\run-openclaw.cmd`
  - 已隔离 `HOME`、`OPENCLAW_HOME`、`OPENCLAW_STATE_DIR`、`OPENCLAW_CONFIG_PATH` 和临时目录。
- `C:\vclaw\openclaw\bootstrap-openclaw.ps1`
  - 可从 `C:\u-king\m-claw\offline\dist\payload-package-051b` 校验并解压 Node/OpenClaw 运行时。
- `C:\vclaw\openclaw\clawx-studio\src\services\remixPackage.ts`
  - GUI 已保存素材包版本并调用正式 render API；只有 404/405/501 才允许进入旧路径。

## 3. 必须先冻结的协议

不要直接从 HTTP handler 启动长期进程。正式 render API 应创建 VClaw 自己的生产任务和队列记录，然后快速返回 `202 Accepted`。

### 3.1 VClaw 接收请求

```http
POST /api/studio/video-projects/{project_id}/render
Content-Type: application/json
```

```json
{
  "package_version": 3,
  "request_id": "req_01J...",
  "idempotency_key": "render:tenant_1:project_1:package_3"
}
```

约束：

- `package_version` 必须存在且属于当前租户、当前项目。
- `request_id` 用于链路追踪；`idempotency_key` 用于去重。
- 相同租户和相同幂等键重复提交，必须返回同一个 VClaw job，不得重复启动 OpenClaw、重复渲染或重复扣费。
- 成功响应为 `202`，至少包含 `job_id`、`status`、`project_id`、`package_version`。

### 3.2 VClaw 投递给 OpenClaw 的业务信封

```json
{
  "schema_version": 1,
  "operation": "render_remix_package",
  "request_id": "req_01J...",
  "idempotency_key": "render:tenant_1:project_1:package_3",
  "tenant_id": "tenant_1",
  "vclaw_job_id": "job_01J...",
  "project_id": "project_1",
  "package": {
    "version": 3,
    "content_hash": "sha256-hex",
    "manifest": {}
  },
  "callback": {
    "status_url": "http://127.0.0.1:8080/internal/openclaw/jobs/job_01J/events",
    "token_ref": "runtime-injected-secret"
  }
}
```

必须传不可变版本快照或可验证的版本引用，不能让 OpenClaw在执行时读取“最新版本”。不得传大文件二进制；manifest 只传 `asset_id`、`file_key`、哈希和结构化工件。

### 3.3 OpenClaw 接受方式

实现者必须先通过真实运行时帮助信息或官方接口确认可用入口，再选以下一种：

1. 本机 HTTP API：优先，便于超时、健康检查和结构化响应；
2. CLI 子进程：仅在 OpenClaw 没有稳定 HTTP API 时使用，必须通过 stdin/临时 JSON 文件传参，不能拼接 shell 字符串。

不得猜测不存在的 `/run` endpoint。必须把确认到的 OpenClaw 版本、启动命令、请求格式记录在 `C:\vclaw\openclaw\docs`。

OpenClaw 首次接受后必须返回真实标识：

```json
{
  "openclaw_run_id": "oc_run_...",
  "accepted": true
}
```

若没有拿到真实 `openclaw_run_id`，VClaw 不得把任务标记为已调度。

## 4. VClaw 状态模型

统一使用以下状态，数据库可沿用现有大小写风格，但 API 输出必须稳定：

```text
created -> queued -> dispatching -> running
        -> awaiting_input
        -> completed
        -> failed
        -> cancelled
```

建议持久化字段：

- `request_id`
- `idempotency_key`
- `package_version`
- `package_content_hash`
- `openclaw_run_id`
- `om_job_id`
- `attempt_count`
- `last_heartbeat_at`
- `last_error_code`
- `last_error_message`
- `result_manifest` 或结果工件引用

不得使用 `sim-*`、随机假 ID 或只存在于内存中的映射。

## 5. 推荐实现拆分

### 5.1 OpenClaw 客户端接口

在 Go 内建立可测试接口，建议放在 `internal/openclaw/`：

```go
type Client interface {
    Health(ctx context.Context) error
    Submit(ctx context.Context, req SubmitRequest) (SubmitResult, error)
    GetRun(ctx context.Context, runID string) (RunStatus, error)
    Cancel(ctx context.Context, runID string) error
}
```

提供真实实现和测试 fake；生产代码不得使用返回模拟成功的 nil client。未配置时必须返回明确的 typed error。

### 5.2 API handler

把 `RemixRenderDispatchHandler` 改为：

1. 校验 principal、租户、项目、不可变 package 版本；
2. 校验或生成 `request_id`，要求稳定 `idempotency_key`；
3. 在一个数据库事务中创建/复用 production job，并 enqueue `openclaw_render`；
4. 返回 `202` 和 VClaw job；
5. 不在 HTTP 请求生命周期内等待 OpenClaw 或 OM 完成。

### 5.3 worker

新增明确的 `openclaw_render` job 类型：

1. 重新从数据库读取指定 package 版本并核对 content hash；
2. 把 job 状态设为 `dispatching`；
3. 调用 `Client.Submit`；
4. 保存真实 `openclaw_run_id`；
5. 进入轮询或等待回调；
6. 把 OpenClaw/OM 终态映射到 VClaw；
7. 只有真实完成且结果工件可读取时才能标记 `completed`。

必须删除或彻底隔离 `dispatchToOpenClaw` 的 `sim-*` 实现，保证正式路由永远不会调用它。

### 5.4 内部回调

如果使用回调，建立仅供本机 OpenClaw 调用的内部 endpoint：

```http
POST /internal/openclaw/jobs/{job_id}/events
```

事件必须包含单调递增的 `sequence`，至少支持：

```json
{
  "sequence": 4,
  "event": "om_job_started",
  "openclaw_run_id": "oc_run_...",
  "om_job_id": "om_job_...",
  "progress": 35,
  "artifacts": [],
  "error": null
}
```

校验本机来源不是认证。必须使用单独的内部 token 或 HMAC；拒绝跨租户 job ID、旧 sequence 和未知 run ID。

## 6. 失败、重试和资源规则

- 连接失败、超时和 5xx 可指数退避重试；4xx/协议错误直接失败。
- `Submit` 超时但结果未知时，必须先按幂等键查询，不能盲目再次提交。
- 最大尝试次数和退避参数写入配置。
- 进程重启后，`queued/dispatching/running` 任务必须能从数据库恢复。
- 单机 MVP 并发默认为 1，不能同时启动多个高资源 OM 渲染。
- 扣费必须与幂等键绑定；失败释放预留额度，成功只结算一次。
- 取消操作必须同时更新 VClaw 并尝试取消 OpenClaw；取消失败要留下诊断信息。

## 7. 安全要求

- 不记录 token、完整用户脚本、绝对素材路径或 manifest 全文。
- 子进程调用使用参数数组，不经 `cmd /c` 拼接用户输入。
- OpenClaw 的 home/state/temp 保持在 VClaw 私有 `runtime-data`。
- callback token 通过环境或安全配置注入，不写入 remix package。
- 对 OpenClaw 返回的文件引用执行项目根目录约束，防止路径穿越。

## 8. 测试要求

至少新增：

1. handler：非法 tenant、项目不属于租户、package 版本不存在、重复幂等键。
2. store：同幂等键并发提交只生成一个 production job。
3. worker：真实 client fake 返回 run ID 后正确持久化。
4. worker：submit 超时后的未知结果不会重复提交。
5. callback：签名错误、sequence 回退、run ID 不匹配均拒绝。
6. restart：模拟进程重启后可以继续 running job。
7. failure：OpenClaw 不可用时返回可诊断失败，不产生 `sim-*` ID。
8. 集成探针：运行 `run-openclaw.cmd --version`、健康检查、提交最小 no-op/doctor 任务。

交付前执行：

```text
go test ./...
go vet ./...
npx vue-tsc --noEmit
npm run build:web
```

## 9. 验收标准

- `POST /api/studio/video-projects/:id/render` 返回真实 VClaw job，HTTP 202。
- 相同 `idempotency_key` 提交 3 次仍只有一个 OpenClaw run。
- 数据库中保存真实 `openclaw_run_id`，不存在 `sim-*`。
- 杀掉并重启 VClaw worker 后，任务可继续查询并到达终态。
- OpenClaw 未安装、配置错误、超时、协议错误均有不同错误码。
- OpenClaw 返回 OM job 后，VClaw 能保存 `om_job_id` 并展示进度。
- 只有可验证结果工件存在时才标记 completed。

## 10. 非目标与禁止项

- 不在本任务中实现 OM 混合时间线渲染；见交接文档 02。
- 不把 M-Claw 的用户目录、缓存、凭据或工作流复制到 VClaw。
- 不允许 GUI 直接绕过 VClaw 调用 OpenClaw 或 OM。
- 不允许用 legacy fallback 的成功响应证明正式链路已经完成。
- 不允许保留任何会进入正式路径的模拟 dispatch。

## 11. 交付清单

接手 LLM 完成后必须给出：

- 修改文件列表与关键接口说明；
- 最终 OpenClaw 版本、启动方式和协议证据；
- 数据库迁移说明；
- 测试命令及结果；
- 一次真实 job 的 `request_id -> VClaw job -> OpenClaw run -> OM job` 映射；
- 仍未完成的风险，不得用“基本完成”掩盖。

