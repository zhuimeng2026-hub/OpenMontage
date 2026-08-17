# FrameFlow 9910 只读观测接口使用指南

本文档供运维人员和其他大模型排查 FrameFlow 上传、渲染、发布及性能问题。观测服务默认位于：

```text
http://192.168.20.173:9910
```

它只提供经过鉴权的性能指标和脱敏日志，不提供 shell、文件浏览、任务修改、服务重启或其他写操作。业务端口为 BFF `8080`、MCP `8900`；`9910` 仅用于观测，停止它不会停止业务。

## 1. 安全边界

- 除 `/health` 外，所有接口都必须携带独立的 `FRAMEFLOW_OBSERVER_TOKEN`。
- 观测令牌不得写入 Git、本文档、普通日志、URL 查询参数或聊天记录。
- 令牌只用于 9910，禁止复用 `MCP_API_TOKEN`、微信密钥或微云凭据。
- 生产环境必须同时使用来源 CIDR 防火墙限制；当前建议只允许管理机 `192.168.20.246/32`。
- 返回日志会脱敏常见 token、key、secret，但调用方仍不得主动收集或回显凭据。

服务端令牌文件默认位置：

```text
/etc/frameflow-observer/observer.env
```

Linux 客户端应通过环境变量提供令牌：

```bash
export FRAMEFLOW_OBSERVER_TOKEN='<通过安全渠道取得的令牌>'
export FRAMEFLOW_OBSERVER_URL='http://192.168.20.173:9910'
```

使用结束后：

```bash
unset FRAMEFLOW_OBSERVER_TOKEN FRAMEFLOW_OBSERVER_URL
```

## 2. 服务启动与检查

9910 由两个独立服务组成：

- `frameflow-perf-monitor.service`：每秒采集 CPU、内存、负载、磁盘、网络和进程组资源。
- `frameflow-observer.service`：以只读 HTTP API 暴露指标和脱敏日志。

启用并立即启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now \
  frameflow-perf-monitor.service \
  frameflow-observer.service
```

手工重启：

```bash
sudo systemctl restart \
  frameflow-perf-monitor.service \
  frameflow-observer.service
```

检查：

```bash
sudo systemctl status \
  frameflow-perf-monitor.service \
  frameflow-observer.service \
  --no-pager -l

curl -fsS http://127.0.0.1:9910/health
sudo ss -lntp '( sport = :9910 )'
```

停用观测服务不会影响 BFF/MCP/Remotion：

```bash
sudo systemctl disable --now \
  frameflow-observer.service \
  frameflow-perf-monitor.service
```

## 3. API 总览

请求条数通过 `limit` 或 `lines` 指定，范围会被限制为 `1–1000`。

| 接口 | 鉴权 | 返回内容 |
| --- | --- | --- |
| `GET /health` | 否 | observer 是否存活及自身 uptime |
| `GET /v1/metrics/latest` | 是 | 最新一个真实性能样本 |
| `GET /v1/metrics/tail?limit=300` | 是 | 最近 N 个性能样本 |
| `GET /v1/logs?source=bff&lines=200` | 是 | `frameflow-bff.service` journald 日志 |
| `GET /v1/logs?source=mcp&lines=200` | 是 | `openmontage-mcp.service` journald 日志 |
| `GET /v1/logs?source=nginx-access&lines=200` | 是 | nginx access log |
| `GET /v1/logs?source=nginx-error&lines=200` | 是 | nginx error log |
| `GET /v1/logs?source=monitor&lines=200` | 是 | 性能采集器自身日志 |

鉴权支持两种请求头，推荐 Bearer：

```text
Authorization: Bearer <FRAMEFLOW_OBSERVER_TOKEN>
```

兼容形式：

```text
X-Observer-Token: <FRAMEFLOW_OBSERVER_TOKEN>
```

## 4. curl 使用示例

健康检查：

```bash
curl -fsS "$FRAMEFLOW_OBSERVER_URL/health" | jq .
```

最新性能：

```bash
curl -fsS \
  -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" \
  "$FRAMEFLOW_OBSERVER_URL/v1/metrics/latest" | jq .
```

最近五分钟指标（监控间隔为一秒时约 300 条）：

```bash
curl -fsS \
  -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" \
  "$FRAMEFLOW_OBSERVER_URL/v1/metrics/tail?limit=300" | jq .
```

读取并筛选 MCP 上传日志：

```bash
curl -fsS \
  -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" \
  "$FRAMEFLOW_OBSERVER_URL/v1/logs?source=mcp&lines=1000" \
  | jq -r '.lines[]' \
  | grep -Ei 'upload_asset|failed|error|exception|traceback'
```

读取 BFF 上游错误：

```bash
curl -fsS \
  -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" \
  "$FRAMEFLOW_OBSERVER_URL/v1/logs?source=bff&lines=1000" \
  | jq -r '.lines[]' \
  | grep -Ei 'upload|render|upstream_failed|tool_error| 502 | 503 | 504 '
```

查看 nginx 请求状态：

```bash
curl -fsS \
  -H "Authorization: Bearer $FRAMEFLOW_OBSERVER_TOKEN" \
  "$FRAMEFLOW_OBSERVER_URL/v1/logs?source=nginx-access&lines=1000" \
  | jq -r '.lines[]' \
  | grep -Ei 'api/mcp|image-batches|render| 4[0-9]{2} | 5[0-9]{2} '
```

## 5. 返回结构

### `/health`

```json
{
  "ok": true,
  "uptime_seconds": 1234.5
}
```

这只表示 observer 进程存活，不代表 BFF、MCP 或渲染正常。

### `/v1/logs`

```json
{
  "source": "mcp",
  "count": 2,
  "lines": [
    "2026-08-17T16:00:00+0800 ...",
    "2026-08-17T16:00:01+0800 ..."
  ]
}
```

### `/v1/metrics/latest`

核心字段：

```json
{
  "timestamp": "2026-08-17T16:00:00+08:00",
  "machine": {"cpu_count": 12},
  "cpu_percent": 10.6,
  "memory": {
    "total_gb": 30.73,
    "available_gb": 19.54,
    "used_percent": 36.41,
    "swap_used_gb": 0.03
  },
  "load": {"1m": 1.75, "5m": 1.51, "15m": 1.11},
  "disk": {"read_mbps": 0.0, "write_mbps": 0.0, "busy_percent": 0.9},
  "network": {"rx_mbps": 0.04, "tx_mbps": 0.01},
  "processes": {
    "remotion": {"count": 0, "rss_mb": 0.0, "cpu_percent": 0.0},
    "chrome": {"count": 0, "rss_mb": 0.0, "cpu_percent": 0.0},
    "ffmpeg": {"count": 0, "rss_mb": 0.0, "cpu_percent": 0.0},
    "mcp": {"count": 1, "rss_mb": 300.0, "cpu_percent": 0.0},
    "bff": {"count": 1, "rss_mb": 30.0, "cpu_percent": 0.0},
    "node": {"count": 0, "rss_mb": 0.0, "cpu_percent": 0.0}
  }
}
```

进程组 `count` 是诊断信号，不等于 systemd 实例数。若 MCP `count > 1` 且没有渲染任务，应继续用 SSH 核对 systemd MainPID、端口 PID 和 cgroup。

## 6. 标准排障流程

其他大模型收到“上传/渲染失败”时，应按以下顺序只读检查：

1. 调用 `/health`，确认 observer 可达。
2. 调用 `/v1/metrics/latest`，记录时间戳及 MCP/BFF/Remotion/Chrome/FFmpeg 进程数。
3. 读取同一时间窗口的 `bff` 和 `mcp` 日志；用 `session_hash`、`project_id`、`batch_id`、`render_job_id`、`request_id` 对齐调用链。
4. 若 BFF 没有请求，查看 `nginx-access`；若出现 4xx/5xx，再查看 `nginx-error`。
5. 区分失败阶段：
   - nginx/BFF 前：域名、TLS、请求体限制、认证或会话问题。
   - `upload_asset_chunk start`：文件名、扩展名、大小、配额校验。
   - `append`：offset、base64、连接超时或会话轮换。
   - `complete`：总大小/SHA、落盘、素材注册。
   - `render`：Remotion/FFmpeg/脚本/素材路径。
   - `weiyun_upload`、`weiyun_share`：微云发布链路。
6. 只有指标达到资源瓶颈证据时，才能归因 CPU/内存/磁盘；不能看到失败就推断“机器性能不足”。
7. 9910 没有修改接口。需要停止服务、读取任务状态文件或检查 PID/cgroup 时，明确说明需要 SSH，并给出最小只读命令。

### 常见信号

| 日志/指标 | 含义 |
| --- | --- |
| `Address already in use` | 同一端口被另一个进程或重复 systemd 服务占用 |
| `Session not found` | MCP transport session 已过期；新版 BFF 应重新初始化并重试一次 |
| `filename must be a safe basename` | 旧版分块上传或其他仍执行严格校验的入口收到不安全文件名，尚未发送文件内容 |
| `asset exceeds ... MB limit` | 超过 `OPENMONTAGE_MAX_UPLOAD_MB` |
| `unsupported asset extension` | 文件后缀不在允许列表 |
| `Still image ... Use operation='render'` | 旧进程错误地把图片任务送入 FFmpeg compose 路径 |
| `failure_stage=orphaned` | 渲染/发布过程中服务重启，后台工作线程丢失 |
| `remotion=0, chrome=0, ffmpeg=0` | 当前没有实际渲染子进程；不表示历史任务成功 |

## 7. 图片文件名规范化规则

素材在服务器上的最终保存名仍必须满足：

```regex
^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$
```

保存名要求：

- 总长度 1–255 个字符。
- 首字符必须是 ASCII 字母或数字。
- 后续只能使用 ASCII 字母、数字、点、下划线和短横线。
- 不能包含 `/`、`\\`、空格、中文或其他 Unicode 字符。
- 图片后缀允许 `.png`、`.jpg`、`.jpeg`、`.webp`、`.gif`、`.bmp`、`.avif`。

安全示例：

```text
product-01.jpg
A_20260817.png
scene.03.webp
```

需要自动改名的输入示例：

```text
商品主图.jpg
_product.jpg
../product.jpg
product photo.jpg
```

分块上传会保留用户原始文件名作为 `original_filename` 元数据，并将不安全输入自动转换为类似 `asset_<12位哈希>.jpg` 的安全保存名。`start` 响应返回 `filename`、`safe_filename`、`original_filename` 和 `renamed`，调用方可以明确展示是否发生改名。已经安全的文件名保持不变。

网页应把原始文件名交给 MCP，不应预先转换成 `_...`。缺失文件名时，网页使用内容 SHA-256 生成稳定的 ASCII fallback。后端仍执行严格的保存路径校验，因此自动改名不会放宽目录穿越防护。

### 2026-08-17 三图上传案例

通过 9910 对齐 MCP 日志后确认：

- 一张图片完成全部 `start → append → complete`，每个分块均 `success=True`。
- 约 `4,654,885` 字节的图片在 `start` 阶段约 3 ms 内失败。
- 约 `389,629` 字节的图片在 `start` 阶段约 3 ms 内失败。
- 两个失败均为 `filename must be a safe basename`。
- 因失败发生在 `start`，这两张图片的内容没有进入分块传输；与网络带宽、文件大小和并发无关。

该案例触发了 `upload_asset_chunk` 1.1.0 的自动改名修复。部署修复后，同类中文或特殊字符文件名应成功上传，并在返回元数据中记录原始名与实际保存名。

## 8. 9910 的限制

- 每次最多返回 1000 行日志或 1000 个指标样本。
- 当前不支持按起止时间、关键词或任务 ID 在服务端过滤；应由客户端用 `jq`/`grep` 过滤。
- journald 接口只读取配置的两个单元：`frameflow-bff.service` 和 `openmontage-mcp.service`。同一程序若被另一个 service 启动，其日志不会自动归入 `source=mcp`。
- 不读取 `projects/.mcp_sessions`、BFF SQLite 或用户文件，所以不能单独依靠 9910 列出所有历史任务。
- 不返回 PID 详情、cgroup、systemd MainPID 或端口监听者；这些需要 SSH 的 `systemctl`、`ss`、`ps` 和 `/proc`。
- `/health` 对允许来源限制和 token 鉴权是例外，只证明 observer 自身可达。

## 9. 给其他大模型的执行约束

可将以下内容与本文档一并交给排障模型：

```text
你只能使用 FrameFlow 9910 的只读接口诊断，不得猜测或请求输出真实 token。
令牌由执行环境变量 FRAMEFLOW_OBSERVER_TOKEN 提供；不要把它放进 URL、日志或回答。
先读取 health 和 metrics/latest，再对齐 bff、mcp、nginx-access、nginx-error 日志。
所有结论必须引用时间、阶段和明确错误；资源不足必须有指标证据。
9910 无法完成的 PID、cgroup、任务状态文件检查，应标注“需要 SSH”，不要声称已经检查。
禁止通过 9910 执行重启、删除、重试、上传或其他写操作，因为该接口没有这些能力。
```

完整部署和防火墙配置见 [`REMOTE_OBSERVABILITY_HANDOFF.md`](REMOTE_OBSERVABILITY_HANDOFF.md)。
