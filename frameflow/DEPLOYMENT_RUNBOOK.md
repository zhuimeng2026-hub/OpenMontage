# FrameFlow 部署与联调 Runbook

## 拓扑

生产双机拓扑固定为：

`浏览器 → https://render.mengxa.com/nginx → BFF :8080 → http://lanes.ymxt.top:8900/mcp`

`render.mengxa.com` 只提供 Web/BFF；`lanes.ymxt.top` 只提供 MCP 上传与 Remotion 渲染。浏览器永远不接触 MCP token。

本机合并部署时，两个域名都可通过 hosts 指向本机，但仍保留同样的域名边界：nginx/BFF 监听本机 8080，MCP 监听 8900。BFF 与 MCP 位于同一台机器时，BFF 必须优先通过 `127.0.0.1` 调用 MCP，避免域名或 LAN 地址回环经过防火墙/NAT 后卡住上传。

## 本机单机联调

1. 准备 `frameflow/bff/.env`：

   ```dotenv
   BFF_PORT=8080
   STATIC_DIR=./web
   MCP_BASE_URL=http://127.0.0.1:8900/mcp
   MCP_PROGRESS_URL=http://127.0.0.1:8900/render-progress
   MCP_API_TOKEN=请填服务端密钥
   FRONTEND_ORIGIN=http://render.mengxa.com
   SESSION_SECURE=false
   AUTH_REQUIRED=false
   RATE_LIMIT_PER_MIN=0
   CUSTOM_COMPOSITION_ENABLED=false
   ```

   本机 hosts 由开发者自行配置，例如将两个域名解析到 `127.0.0.1`；本 runbook 不修改系统 hosts。

2. 启动 MCP（确保 `lanes.ymxt.top:8900` 可达），再启动 BFF：

   ```powershell
   Set-Location C:\Users\Admin\OpenMontage\frameflow\bff
   go run .
   ```

3. 将 `nginx/frameflow-render-local.conf.template` 复制到 nginx 的 `sites-enabled`，检查并重载：

   ```powershell
   nginx.exe -p C:/Users/Admin/OpenMontage/nginx -c C:/Users/Admin/OpenMontage/nginx/nginx.conf -t
   nginx.exe -p C:/Users/Admin/OpenMontage/nginx -c C:/Users/Admin/OpenMontage/nginx/nginx.conf -s reload
   ```

4. 检查：

   ```powershell
   curl.exe -i http://render.mengxa.com/
   curl.exe -i http://render.mengxa.com/api/me
   curl.exe -i http://render.mengxa.com/api/image-scripts
   curl.exe -i http://lanes.ymxt.top:8900/mcp
   ```

   MCP 的 `GET` 返回 4xx 也不代表服务未启动；重点确认 TCP/HTTP 可达，再用 BFF 的真实 `/api/mcp` 工具调用验证 token 和会话。

## 生产双机

在 render 机器部署 BFF、SPA 与 nginx，使用 `nginx/frameflow-render-production.conf.template`。将证书路径换成真实证书，但不要把证书或 `.env` 提交到仓库。

生产 BFF 环境至少应为：

```dotenv
BFF_PORT=8080
STATIC_DIR=./web
MCP_BASE_URL=http://lanes.ymxt.top:8900/mcp
MCP_PROGRESS_URL=http://lanes.ymxt.top:8900/render-progress
MCP_API_TOKEN=生产密钥
FRONTEND_ORIGIN=https://render.mengxa.com
SESSION_SECURE=true
AUTH_REQUIRED=true
RATE_LIMIT_PER_MIN=30
CUSTOM_COMPOSITION_ENABLED=false
```

在 lanes 机器上只暴露 MCP 端口给 render 机器的固定来源地址；不要把 `MCP_API_TOKEN` 放入前端、nginx 配置或日志。

## 生产合并部署（192.168.20.173）

当 Web/BFF/MCP/Remotion 全部部署在 `192.168.20.173` 时，外部入口仍使用 `https://render.mengxa.com`，但 BFF 的上游配置改为：

```dotenv
MCP_BASE_URL=http://127.0.0.1:8900/mcp
MCP_PROGRESS_URL=http://127.0.0.1:8900/render-progress
```

MCP 的 Uvicorn keep-alive 默认配置为30秒，避免与前端/BFF默认5秒状态轮询形成 TCP 关闭竞态。需要覆盖时在 MCP 服务环境设置 `MCP_HTTP_KEEP_ALIVE_SECONDS=30`（允许10–300秒），修改后重启 MCP。

多用户渲染由 MCP 的实际 Remotion CPU 闸门公平调度。12 核生产机建议：

```ini
REMOTION_CONCURRENCY=5
REMOTION_MAX_PARALLEL=2
REMOTION_MAX_PARALLEL_PER_USER=1
```

该配置允许两个不同微信用户各运行一个任务；同一用户的后续任务返回成功并进入队列，不再由 BFF 返回 429。队列按用户轮转、用户内部保持 FIFO，等待中的任务由 MCP 持久化记录在服务重启后恢复。

当前实现的边界是“全局 2 槽、每用户严格 1 槽”：同一用户即使有多个待渲染任务，也只能同时运行其中一个；该限制用于保护 12 核机器和保证多用户首任务的公平性。它不是未来 64 核机器的最终并发策略，不能仅通过把 `REMOTION_MAX_PARALLEL_PER_USER` 改成 2 来替代调度器升级。

### 64 核扩展与分层公平调度规划

增加到 64 核后，目标是先满足每个活跃用户的第一个任务，再把剩余算力轮转给已有用户的第二个任务。调度器应按“首任务优先、第二任务加权轮转”的两层策略工作：

- 第一层保障不同用户的首任务，用户内部仍保持 FIFO；新用户到达后，其首任务应优先于普通的第二任务。
- 第二层只使用首任务没有可调度需求后剩余的槽位，按用户轮转安排第二任务，不能让单个老用户独占全部额外算力。
- 第二任务必须带等待老化（aging）或等价的保底配额/调度比例；当新用户持续进入时，等待足够久的老用户第二任务仍要获得调度机会，避免第二任务饥饿。
- 当活跃用户数量不足时，空闲槽位可借给第二层，以保持 CPU 利用率；当新用户增加时，第二层应收缩但不应被无界期地饿死。

64 核机器的初始压测配置建议为：

```ini
REMOTION_CONCURRENCY=5
REMOTION_MAX_PARALLEL=10
REMOTION_MAX_PARALLEL_PER_USER=2
```

这里的 `10` 是起始压测值，不是承诺的生产并发。按 Remotion 工作线程粗略估算为约 50 个渲染线程，仍需为 Chrome、FFmpeg、MCP、BFF 和系统保留资源。若当前仍只有 32GB 内存，内存峰值、swap 或 Chrome 进程可能先于 CPU 成为瓶颈，应优先扩容内存或降低并发。

验收必须同时覆盖公平性、稳定性和利用率：

1. 在至少两个老用户各有多个任务、并持续加入新用户的混合负载下，新用户首任务应能开始，老用户第二任务也应在老化阈值内开始；记录每个用户首任务和第二任务的排队等待时间。
2. 任一用户不得突破配置的并发上限；用户内部任务保持 FIFO；队列位置、状态、重启恢复和取消行为不能回归。
3. 每一级压测记录成功率、任务耗时（P50/P95）、排队等待（P50/P95）、CPU、RSS/内存峰值、swap、load、磁盘 busy，以及 Chrome/FFmpeg/MCP/BFF 进程组资源。
4. 只有在当前级别所有任务完成且无失败、持续 swap、内存超过 85%、磁盘长期接近 100% busy、明显错误增长或单任务耗时超过单并发基线两倍时，才允许进入下一级；异常立即退回上一个稳定级别。
5. 64 核扩容后按 `1、2、4、6、8、10、12` 个全局并发逐级压测；稳定生产值取最后一个稳定级别的约 80%，并保留突发负载和基础服务余量。若公平性指标或内存指标先失败，以较低者为准，不以 CPU 尚未跑满为理由继续加压。

修改后重启 BFF，并先完成一个最小上传。若 `upload_asset_chunk` 连小文件也无法在数秒内返回，先查 MCP 日志，不要开始并发压测：

```bash
sudo systemctl restart frameflow-bff
sudo systemctl status frameflow-bff openmontage-mcp --no-pager -l
sudo journalctl -u openmontage-mcp -n 200 --no-pager
```

## 性能监控与压测

监控脚本只读取 Linux `/proc`，不需要安装第三方依赖。部署代码后在 `192.168.20.173` 执行：

```bash
cd /opt/OpenMontage
mkdir -p perf
python3 scripts/frameflow_perf_monitor.py \
  --interval 1 \
  --duration 3600 \
  --output "perf/frameflow-$(date +%Y%m%d-%H%M%S).jsonl" \
  | tee perf/frameflow-live.log
```

终端会实时输出 CPU、内存、swap、load、磁盘、网络，以及 Remotion/Chrome/FFmpeg/MCP/BFF/Node 进程组资源；JSONL 用于压测后计算峰值和持续吞吐。跨机器实时读取请部署带令牌鉴权、来源地址限制和日志脱敏的 `scripts/frameflow_observer.py`，完整步骤见 `frameflow/REMOTE_OBSERVABILITY_HANDOFF.md`。不要在生产环境使用无鉴权的 `python -m http.server` 暴露日志目录。

压测按 1、2、4、5、6 个并发逐级增加，每一级均需等待所有任务结束。任一级出现失败、swap 持续增长、内存超过 85%、磁盘持续接近 100% busy，或单任务耗时超过单并发基线两倍，即停止加压。最终生产并发取稳定级别的约 80%，为 nginx、BFF、MCP 和突发负载留余量。

## 微信服务号

- 微信网页授权域名配置为 `render.mengxa.com`。
- `WECHAT_APP_ID`、`WECHAT_APP_SECRET` 仅写在 BFF `.env`。
- 推荐显式设置 `WECHAT_REDIRECT_URI=https://render.mengxa.com/api/wechat/callback`，避免代理头配置错误导致回调协议变成 HTTP。
- 生产必须 `AUTH_REQUIRED=true`、`SESSION_SECURE=true`，并确认微信参数已填；当前代码会对缺失微信配置执行 fail-closed，并拒绝启动。
- 桌面二维码登录依赖 BFF 进程内票据；单实例联调可用，多实例/滚动发布前需迁移到共享存储。

## 验收顺序

1. nginx `-t`、BFF 启动日志无配置告警。
2. `/api/me`、`/api/image-scripts` 可达。
3. 微信登录后 cookie 持久，`/api/me` 返回 authenticated。
4. 5–10 张图片分块上传，确认同一批次 `project_id`。
5. 提交渲染，确认 `/api/render-progress/:jobId` 的 SSE 不被缓冲；断开 SSE 后前端状态轮询仍可完成。
6. 两个独立批次并行提交，确认 lanes 机器资源和 BFF 并发租约允许并行。

本机若未安装 nginx 可执行文件，无法执行 `nginx -t`；此时至少做模板静态审阅，并在目标机器安装 nginx 后执行同一命令再 reload。
