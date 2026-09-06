# OpenMontage MCP Server — OpenClaw 对接文档

## 1. 启动 MCP Server

在 OpenMontage 所在机器上：

```bash
cd /opt/video_web/OpenMontage
python mcp_server.py
```

默认监听 `0.0.0.0:8900`（IPv4 + IPv6 双栈），transport 为 `streamable-http`。

**鉴权提示：** 若已配置 `MCP_API_TOKEN`（见下一节），所有客户端请求必须携带 `Authorization: Bearer <token>`，否则返回 401。验证服务：

```bash
# 未配置 token 时
curl -s -X POST http://localhost:8900/mcp \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'

# 已配置 token 时（加上 Authorization 头）
curl -s -X POST http://localhost:8900/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer <MCP_API_TOKEN>" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"test","version":"0.1"}}}'
```

> MCP 服务启动后常驻。修改 `mcp_server.py` 或 `.env` 中的 token 后需重启进程才能生效。

---

## 2. Token 鉴权（外网暴露前必读）

MCP server 通过环境变量 `MCP_API_TOKEN` 开启 Bearer Token 鉴权。**未设置该变量时服务不带鉴权运行**，仅供内网使用，严禁直接暴露公网。

### 2.1 生成 Token

```bash
cd /opt/video_web/OpenMontage
python mcp_server.py gen-token
# 输出类似：kCYnik7zip0QniCECr49ZhlCoXMzlfOY3hfH9QTYm-o
```

### 2.2 配置 Token

写入 `.env`（已 gitignore，不会被提交）或导出环境变量：

```bash
# .env 文件（推荐）
echo 'MCP_API_TOKEN=<上面生成的token>' >> .env

# 或环境变量
export MCP_API_TOKEN=<token>
```

`mcp_server.py` 启动时依次检查环境变量 → `.env` 文件。

### 2.3 生效与校验

重启 MCP 服务后，用带鉴权的请求验证：

```bash
# 无 token / 错 token → 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8900/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# → 401

# 正确 token → 200
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8900/mcp \
  -H "Content-Type: application/json" -H "Accept: application/json" \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}'
# → 200
```

> 鉴权用常量时间比较（`hmac.compare_digest`）防时序侧信道；所有 HTTP 请求（含 `/mcp` 以外路径）都要求鉴权。

### 2.4 安全底线

- 未设置 `MCP_API_TOKEN` 启动时，日志会打印醒目警告。
- Token 泄露后，重新 `gen-token` 并更新所有客户端即可，无需改代码。
- 不要把 token 写进任何会被 git 提交的文件。

---

## 3. OpenClaw 配置

在 OpenClaw 机器上编辑 `~/.openclaw/openclaw.json`（OpenClaw 2026.3+ 使用 `mcp.servers` 结构，文档早期的 `plugins.bundle-mcp` 为旧格式）。

**内网访问（带鉴权）：**

```json
{
  "mcp": {
    "servers": {
      "openmontage": {
        "name": "openmontage",
        "url": "http://192.168.20.173:8900/mcp",
        "headers": {
          "Authorization": "Bearer <MCP_API_TOKEN>"
        }
      }
    }
  }
}
```

**已有域名接入点（外网）：** 本机已配置域名 `lanes.ymxt.top:8900`，但该域名目前仅解析到 IPv6、实际连通性待确认，公网可达前仍建议用内网地址：

```json
{
  "mcp": {
    "servers": {
      "openmontage": {
        "name": "openmontage",
        "url": "http://lanes.ymxt.top:8900/mcp",
        "headers": {
          "Authorization": "Bearer <MCP_API_TOKEN>"
        }
      }
    }
  }
}
```

> OpenClaw 的 `mcp.servers.<name>.headers` 是**记录（record）格式**（键为 header 名，值为 header 值）——据此携带 `Authorization: Bearer`。（注意：ACP 协议 schema 里该字段是数组，但 OpenClaw 实际校验要求 record。）

如果有 IPv6 地址，也可以用 IPv6：

```json
"url": "http://[2001:db8::1]:8900/mcp"
```

配置后重启 OpenClaw Gateway：

```bash
openclaw gateway --restart
```

---

## 4. MCP 工具一览

共 **32 个 MCP 工具**（FastMCP `@mcp.tool()` 装饰器），分为 9 组；外加
2 个非 MCP 的 HTTP 路由（`/render-progress/{job_id}` SSE、`/voicebox/mcp/{path:path}`
反向代理），见 §4.9。

### 4.1 工具发现（4 个）

#### `list_tools` — 列出工具

列出所有注册工具，可按 capability / status / provider / tier 过滤。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| capability | string? | 否 | 按能力过滤：`tts`, `image_generation`, `video_generation`, `video_post`, `analysis`, `audio_processing`, `enhancement`, `graphics`, `subtitle` 等 |
| status | string? | 否 | `available` / `unavailable` / `degraded` |
| provider | string? | 否 | 按供应商过滤：`elevenlabs`, `ffmpeg`, `fal`, `piper`, `openai` 等 |
| tier | string? | 否 | `core` / `voice` / `enhance` / `generate` / `source` / `analyze` / `publish` |

**返回：** ToolSummary 数组，每项包含 name, capability, provider, status, tier, runtime, stability。

**示例调用：**
```
openclaw agent --message "调用 openmontage 的 list_tools，过滤 status=available"
```

#### `get_tool_info` — 获取工具详情

获取工具的完整合约：输入/输出 JSON Schema、依赖、成本信息。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| tool_name | string | 是 | 工具名称 |

**返回：** 完整工具合约 dict，关键字段：
- `input_schema` — 输入 JSON Schema（调用 execute_tool 前必读）
- `output_schema` — 输出 JSON Schema
- `dependencies` — 依赖列表（cmd:xxx, env:xxx, python:xxx）
- `best_for` / `not_good_for` — 适用/不适用场景
- `cost` 信息

#### `get_capabilities` — 按能力分组

返回所有工具按 capability 分组的完整目录。

**参数：** 无

**返回：** `{capability_name: [tool_info, ...]}`

#### `get_provider_menu` — 供应商菜单

返回人类可读的供应商菜单，包含哪些已配置（有 API Key）、哪些未配置。

**参数：** 无

**返回：** 包含 `composition_runtimes`, `capabilities`, `setup_offers`, `runtime_warnings`

---

### 4.2 工具执行（2 个）

#### `execute_tool` — 执行工具

执行任意 OpenMontage 工具。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| tool_name | string | 是 | 工具名称（从 list_tools 获取） |
| inputs | object | 是 | 工具输入参数（格式见 get_tool_info 的 input_schema） |

**返回：** ExecuteResult

| 字段 | 类型 | 说明 |
|------|------|------|
| success | bool | 是否成功 |
| data | object | 工具输出数据 |
| artifacts | string[] | 生成的文件路径列表 |
| error | string? | 错误信息 |
| cost_usd | float | 实际花费（美元） |
| duration_seconds | float | 执行时长（秒） |
| seed | int? | 使用的随机种子 |
| model | string? | 使用的模型标识 |

**重要：** 调用前先 `get_tool_info` 确认输入格式，对付费工具先 `dry_run_tool` 查看成本。

#### `dry_run_tool` — 预检

不实际执行，只返回估算成本和运行时间。

**参数：** 同 `execute_tool`

**返回：** DryRunResult — tool, estimated_cost_usd, estimated_runtime_seconds, status, would_execute

---

### 4.3 素材管理与视频发布（5 个）

#### `upload_asset` — 上传项目素材

通过 Streamable HTTP 使用时，服务端会使用 MCP 标准的 `Mcp-Session-Id` 区分不同
WorkBuddy 会话。上传文件会保存到该会话对应的隔离目录；分片上传的
`upload_id` 也只能由创建它的会话继续使用。客户端不需要把会话 ID 作为工具参数
传入，只需按 MCP 协议在后续 HTTP 请求中原样携带服务端返回的 `Mcp-Session-Id`。

图片上传完成后，响应会额外包含 `status=collecting_assets`、`asset_count`、
`message`、`next_action=continue_upload_or_generate` 和 `batch_id`。图片会按
当前 Streamable HTTP 会话隔离；没有 `Mcp-Session-Id` 的请求会明确失败，不会
进入共享的 legacy 批次。批次成功发布后，同一会话的下一张图片会自动开启新批次。

将客户端的图片、视频或音频以 Base64 上传到远程 OpenMontage 项目的
`projects/<project_id>/assets/` 目录。返回的 `asset_manifest` 可直接传给
`execute_tool(tool_name="video_compose", ...)`，或传给图生视频工具。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | string | 是 | 项目 ID，只允许字母、数字、`.`、`_`、`-` |
| filename | string | 是 | 文件名，不允许路径分隔符；支持常见图片/视频/音频格式 |
| content_base64 | string | 是 | 原始 Base64 或 `data:...;base64,...` |
| mime_type | string? | 否 | MIME 类型，不填则按扩展名推断 |
| sha256 | string? | 否 | 64 位 SHA-256，用于完整性校验 |
| overwrite | boolean | 否 | 默认 false；同名不同内容时拒绝覆盖 |

**安全限制：** 单文件默认最大 100 MB，可通过 `OPENMONTAGE_MAX_UPLOAD_MB`
调整；文件始终限制在对应项目的 `assets` 子目录内。

对于 1080p 图片、视频或批量素材，优先使用 `upload_asset_chunk`，避免
Base64 请求经过 Nginx/网关时超限。每个分块建议不超过 1 MiB。

#### `upload_asset_chunk` — 分片上传（用于 1080p / 批量素材）

针对 1080p 图片、视频或大批量素材的可恢复分片上传通道。每个分片作为
一次 MCP 工具调用（参数为 Base64 字符串），单次请求体较小，可安全通过
Nginx / 网关。

**操作流程（三阶段）：**

```text
start  (operation=start,  project_id, filename, total_bytes, mime_type, sha256)
       → 返回 upload_id
append (operation=append, upload_id, offset, chunk_base64)
       → 单片 ≤ 1 MiB，可重复调用直到 offset+len == total_bytes
complete(operation=complete, upload_id)
       → 校验 SHA-256，落盘到 projects/<project_id>/assets/
```

**参数（按 operation 区分）：**

| 参数 | 类型 | 必填 | 适用 | 说明 |
|------|------|------|------|------|
| operation | enum | 是 | 全部 | `start` / `append` / `complete` |
| project_id | string | 是 | start | 项目 ID，规则同 `upload_asset` |
| filename | string | 是 | start | 文件名，禁止路径分隔符 |
| total_bytes | int | 是 | start | 完整文件字节数（用于服务端预算与校验） |
| mime_type | string? | 否 | start | MIME 类型，按扩展名推断 |
| sha256 | string? | 否 | start | 64 位 SHA-256，`complete` 时校验 |
| upload_id | string | 是 | append/complete | `start` 返回的上传句柄 |
| offset | int | 是 | append | 当前分片在文件内的字节偏移 |
| chunk_base64 | string | 是 | append | 本片 Base64 字符串（≤ 1 MiB） |

**返回值关键字段：**

- `start` 返回 `{ upload_id, received_bytes: 0 }`
- `append` 返回 `{ upload_id, received_bytes }`
- `complete` 返回标准 `success` + `artifacts`（写入路径）

> 服务端只信任 `Mcp-Session-Id` 标头确定归属，不接受请求体里另外声明
> `upload_id` 的所属会话；`upload_id` 只能由创建它的会话继续使用。

#### `rsync_upload_artifact` — 上传生成产物到公网服务器

通过 SSH/rsync 将 OpenMontage 服务器上的生成视频或其他产物上传到已配置的
公网服务器。连接参数全部从项目 `.env` 读取，不在 MCP 请求中传递密钥。

配置示例见 `.env.example` 中的 `RSYNC_*` 项。`source_path` 可在调用时传入，
不传时使用 `RSYNC_SOURCE_PATH`。成功后返回远程路径；配置
`RSYNC_PUBLIC_BASE_URL` 时同时返回 HTTPS `download_url`。

```json
{
  "tool_name": "rsync_upload_artifact",
  "inputs": {
    "source_path": "C:/OpenMontage/projects/bag-demo/renders/final.mp4",
    "remote_name": "bag-demo-final.mp4"
  }
}
```

生产环境应为该工具配置专用 SSH 用户、密钥和仅可写入的远程目录，并在公网
服务器上用 Nginx/Caddy 提供 HTTPS 下载。首次连接前请将服务器指纹写入
OpenMontage 运行用户的 `known_hosts`；工具使用 `BatchMode=yes` 和
`StrictHostKeyChecking=yes`，不会交互式接受未知主机。

推荐的视频网站素材规格：图片按 1920×1080（16:9）准备；视频使用
1920×1080、H.264、25/30 FPS；单个素材控制在 100 MB 以内。上传接口不
强制重编码，保持源文件质量，并在完成时校验 SHA-256。

**返回示例：**

```json
{
  "success": true,
  "asset": {
    "id": "bag-demo-a1b2c3d4e5f6",
    "path": ".../projects/bag-demo/assets/bag.png",
    "mime_type": "image/png",
    "sha256": "..."
  },
  "asset_manifest": {
    "assets": [
      {"id": "bag-demo-a1b2c3d4e5f6", "path": ".../projects/bag-demo/assets/bag.png"}
    ]
  }
}
```

#### `s3_upload` — S3 兼容存储上传

把已渲染的视频上传到任意 AWS S3 兼容对象存储（AWS S3、MinIO、Cloudflare
R2、阿里云 OSS、腾讯云 COS、七牛云等）。签名使用纯 `requests` 实现的
AWS SigV4，无需安装 `boto3`/`minio` 等依赖，只需配置 `.env` 中的四个
必填变量：

- `S3_ENDPOINT_URL` — 端点（含 scheme，如 `https://s3.cn-hangzhou.aliyuncs.com`）
- `S3_ACCESS_KEY` / `S3_SECRET_KEY` — 密钥对
- `S3_BUCKET` — 目标桶名（桶需提前建好）

**三种交付模式（通过 `visibility` + `make_download_page` 控制）：**

| 模式 | 适用场景 | 返回字段 |
|------|----------|---------|
| `visibility=public` | 公开链接，直接可访问 | `url`（永久直链） |
| `visibility=private` | 临时链接，过期自动失效 | `url`（预签名 GET，默认 7 天） |
| `make_download_page=true` | 多文件打包交付 | `download_page_url` + `uploaded_files` 列表 |

> `visibility=public` 且需要 CDN 域名时，额外配置 `S3_PUBLIC_BASE_URL`，否则
> 返回的是端点原始 URL（外部客户端可能 403）。

**关键参数：**

| 参数 | 类型 | 必填 | 默认 | 说明 |
|------|------|------|------|------|
| `video_path` | string | 是 | — | 已渲染视频路径（来自 `render_report.outputs[].path`） |
| `visibility` | enum | 否 | `public` | `public` 或 `private` |
| `expire_seconds` | int | 否 | 604800 | private 模式下的链接有效期（60~604800 秒） |
| `project_id` | string | 否 | 从路径推断 | 命名空间，决定 object key 前缀 |
| `object_key` | string | 否 | `videos/<project_id>/<filename>` | 显式指定对象键 |
| `make_download_page` | bool | 否 | `false` | 是否生成 HTML 下载页 |
| `additional_files` | string[] | 否 | — | 要一起上传并展示在下载页中的额外文件 |
| `page_title` | string | 否 | — | 下载页标题 |
| `platform_label` | string | 否 | `s3` | `publish_log` 中的平台标识 |

**调用示例（公开链接）：**

```json
{
  "tool_name": "s3_upload",
  "inputs": {
    "video_path": "/opt/OpenMontage/projects/my-video/renders/final.mp4",
    "project_id": "my-video",
    "visibility": "public"
  }
}
```

**返回示例（公开链接）：**

```json
{
  "success": true,
  "url": "https://cdn.example.com/videos/my-video/final.mp4",
  "object_key": "videos/my-video/final.mp4",
  "bucket": "my-bucket",
  "visibility": "public",
  "uploaded_files": [...],
  "publish_log": { "platform": "s3", "status": "published", ... }
}
```

**返回示例（私钥预签名 + 下载页）：**

```json
{
  "success": true,
  "url": "https://bucket.s3.amazonaws.com/videos/...?X-Amz-Signature=...",
  "visibility": "private",
  "expires_at": "2026-08-15T12:00:00Z",
  "download_page_url": "https://cdn.example.com/videos/my-video/download.html",
  "uploaded_files": [
    {"key": "videos/my-video/final.mp4", "url": "...", "size_bytes": 12345678},
    {"key": "videos/my-video/subtitle.srt", "url": "...", "size_bytes": 1234}
  ],
  "publish_log": { "platform": "s3", "status": "published", ... }
}
```

**注意事项：**
- 该工具执行的是服务器本地文件的 `PUT` 上传，不涉及客户端 Base64 编码，适合
  大文件（无 100 MB 限制，单文件 PUT 上限取决于 S3 服务端，通常 5 GB）。
- 上传失败会返回 `success=false` 和 `error` 字段，不含密钥明文。
- 重复调用同一 `object_key` 会覆盖已有对象；如需保留历史版本，传不同的
  `object_key` 或在 `project_id` 后追加时间戳。
- 该工具在 `tier=publish`、`capability=publish`，可通过
  `list_tools(tier="publish")` 或 `list_tools(capability="publish")` 发现。
- 外部智能体调用前建议先用 `dry_run_tool` 检查 `missing_env` 和文件是否存在。

#### `export_bundle` — 打包渲染产物

将已渲染的视频及关联素材打包为自包含的导出目录，并生成合法的
`publish_log`（`status: "exported"`）。无需网络上传，仅做本地整理，
适用于交付给创作者或手动上传到视频平台的场景。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `video_path` | string | 是 | 已渲染视频路径（来自 `render_report.outputs[].path`） |
| `project_name` | string? | 否 | 项目名，默认从路径推断 |
| `chapters` | dict[]? | 否 | 章节信息列表，用于生成时间轴元数据 |
| `metadata` | dict? | 否 | 附加 SEO 元数据（标题、描述、标签等） |

**返回：** 包含 `export_dir`（打包目录路径）、`publish_log`（合法的发布日志）

---

### 4.4 Pipeline 管理（3 个）

#### `list_pipelines` — 列出生产线

**参数：** 无

**返回：** pipeline 名称列表

```
["animated-explainer", "animation", "avatar-spokesperson", "cinematic",
 "clip-factory", "documentary-montage", "hybrid", "localization-dub",
 "podcast-repurpose", "screen-demo", "talking-head", "framework-smoke"]
```

#### `get_pipeline` — 获取 Pipeline 详情

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | Pipeline 名称 |

**返回：** 完整 YAML manifest dict，包含 stages, tools, review criteria, approval gates。

#### `get_pipeline_stages` — 获取阶段顺序

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| pipeline_name | string | 是 | Pipeline 名称 |

**返回：** 阶段名称列表，如 `["research", "proposal", "script", "scene_plan", "assets", "edit", "compose"]`

---

### 4.5 Checkpoint 管理（4 个）

#### `read_checkpoint` — 读取检查点

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | string | 是 | 项目 ID |
| stage | string | 是 | 阶段名称 |

**返回：** CheckpointData 或 error dict

#### `get_latest_checkpoint` — 获取最新检查点

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | string | 是 | 项目 ID |

**返回：** CheckpointData 或 error dict

#### `get_pipeline_status` — 获取 Pipeline 进度

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | string | 是 | 项目 ID |
| pipeline_type | string? | 否 | Pipeline 类型 |

**返回：** PipelineStatus — completed_stages, next_stage, latest_checkpoint

#### `write_checkpoint` — 写入检查点

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| project_id | string | 是 | 项目 ID |
| stage | string | 是 | 当前阶段 |
| status | string | 是 | `completed` / `in_progress` / `awaiting_human` / `failed` |
| artifacts | object | 是 | 阶段产物 |
| pipeline_type | string? | 否 | Pipeline 类型 |
| style_playbook | string? | 否 | 视觉风格 |
| human_approval_required | bool? | 否 | 是否需要人工审批 |
| human_approved | bool? | 否 | 是否已审批 |
| review | object? | 否 | 审查结果 |
| cost_snapshot | object? | 否 | 成本快照 |
| error | string? | 否 | 错误信息 |

**返回：** `{"success": true, "path": "..."}`

### 4.6 声音克隆与 Voicebox 桥接（5 个）

通过本地 Voicebox REST API（默认 `http://127.0.0.1:17493`，可通过环境变量
`VOICEBOX_REST_URL` 覆盖）做声音克隆与合成；语音数据完全在本地处理，
不消耗云端 API 额度。

#### `clone_voice` — 创建克隆声纹（Voicebox，简化接口）

调用 Voicebox `/clone_voice` 接口创建一个克隆声纹，返回 `profile_id`
供 `voicebox_tts` 后续合成使用。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | 声纹名称（Voicebox 内可见） |
| audio_paths | string[] | 是 | 参考音频文件绝对路径列表；建议总时长 ≥ 30 秒 |
| description | string? | 否 | 声纹备注 |
| engine | string? | 否 | 克隆引擎，默认 `qwen`；可选 `qwen`/`luxtts`/`chatterbox`/`chatterbox_turbo`/`tada`。预设声纹（`kokoro` 等）不支持克隆 |
| reference_texts | string[]? | 否 | 每段音频对应文本（与 audio_paths 同序）；Voicebox 要求每段参考音频有匹配转写 |
| reference_text | string? | 否 | 同一段文本应用到所有参考音频（粗粒度回退） |

**返回：** ExecuteResult，`data.profile_id` 为新声纹 ID。

#### `list_cloned_voices` — 列出本地声纹

仅返回 `voice_type=cloned`（即通过 `clone_voice` / `voicebox_clone_voice` 创建的）声纹。
`include_presets=True` 时同时返回预设和 designed 声纹。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| include_presets | bool | 否 | 默认 false |

**返回：** ExecuteResult，`data.voices[]` 每项含 `id`、`name`、`voice_type`、`is_cloned`。

#### `voicebox_clone_voice` — 同 `clone_voice`，显式命名空间别名

与 `clone_voice` 功能相同；保留这个具名别名是因为它在 `.mcp.json` 的
`voicebox` server 上下文里调用更清晰，且接受 Voicebox 原生参数字段
（`default_engine` 而非 `engine`）。行为以 `clone_voice` 为准。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 是 | 声纹名称 |
| audio_paths | string[] | 是 | 参考音频路径列表 |
| description | string? | 否 | 声纹描述 |
| default_engine | string? | 否 | 默认 `qwen` |
| reference_texts | string[]? | 否 | 每段参考音频对应转写 |
| reference_text | string? | 否 | 公共参考文本（粗粒度） |

#### `voicebox_tts` — 用 Voicebox 声纹合成语音

通过本地 Voicebox REST API 调用文本转语音，使用已克隆（或预设）的声纹。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| text | string | 是 | 要合成的文本 |
| profile_id | string | 是 | `voicebox_clone_voice` 返回或预设的声纹 ID |
| language | string? | 否 | 默认 `en` |
| engine | string? | 否 | 覆盖该声纹的默认引擎 |
| model_size | string? | 否 | 模型尺寸（视引擎而定） |
| instruct | string? | 否 | 风格/情感指令 |
| personality | bool? | 否 | 是否启用 personality 向量 |
| seed | int? | 否 | 随机种子 |
| output_path | string? | 否 | 不指定时落到当前项目 `assets/audio/` |
| timeout_seconds | int? | 否 | 单次请求超时 |

**返回：** ExecuteResult，`artifacts=[output_path]`。

#### `voicebox_list_cloned_voices` — 同 `list_cloned_voices`

与 `list_cloned_voices` 等价；保留这个具名别名用于 Voicebox 命名空间下
调用的清晰度。返回字段（含 `voice_type` 和 `is_cloned`）刻意对齐
ElevenLabs 的 schema，方便上层 selector 跨供应商做统一过滤。

### 4.7 字幕烧录与发布辅助（4 个）

围绕「渲染 → 发布」流水线提供的、已经被高层 `create_remotion_video_share`
组合好的底层工具，单独暴露以便重试、修复或手工拼接。

#### `burn_subtitles` — 把字幕烧进视频

`video_compose` `operation=burn_subtitles` 的薄封装，使用 FFmpeg
`subtitles=` 滤镜；codec 默认 `libx264` 保证广泛兼容，音轨原样拷贝
（不重编码）。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| input_path | string | 是 | 源视频路径 |
| subtitle_path | string | 是 | `.srt` / `.ass` / `.vtt` 文件路径 |
| output_path | string? | 否 | 不传则覆盖原文件（FFmpeg 同名输出） |
| subtitle_style | object? | 否 | `fontname` / `fontsize` / `primary_color` 等覆盖 |
| codec | string | 否 | 默认 `libx264` |
| crf | int | 否 | 默认 23，数值越低质量越高 |

**返回：** ExecuteResult，`artifacts=[output_path]`。

#### `retry_render_publish` — 重试失败的微云发布

仅重试已渲染成功但发布到 Weiyun 失败的渲染任务。**不会再次调用渲染器**，
可以反复调用：已经 `published` 的任务直接返回现有 `share_url` 而不会
重复上传；失败的 `video_path` 在持久化层保留，便于客户端取回半成品。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| render_job_id | string | 是 | 由 `create_remotion_video_share` 返回的渲染任务 ID |

**返回：**

```json
{
  "success": true,
  "render_job_id": "...",
  "status": "published",
  "stage": null,
  "share_url": "https://share.weiyun.com/...",
  "error": null
}
```

并发安全：对同一 `render_job_id` 的重试由每 job 独立的 thread lock 串行化，
并发调用第二个会立即返回 `success=false, stage="in_progress"` 而不是
排队，避免双发重复上传。

> 当 `status="failed"` 且 `stage` 为 `render` / `validation` /
> `background_crash` 时（渲染未完成），此工具**无法恢复**；需重新调用
> `create_remotion_video_share`。

#### `weiyun_upload` — 上传视频到腾讯微云（Token 流）

不需要 QR 码登录 / cookie，依赖 `.env` 里的 `WEIYUN_MCP_TOKEN`。
返回 `file_id` 与上传后的文件名，作为 `weiyun_gen_share_link` 的输入。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| video_path | string | 是 | 已渲染视频本地路径 |
| target_dir | string | 否 | 远端目标目录，空字符串表示根 |
| overwrite | bool | 否 | 默认 false |

**返回：** `{ success, data: { file_id, name, ... }, error }`

#### `weiyun_gen_share_link` — 生成微云分享外链

把 `weiyun_upload` 上传的文件（或目录）打包成可对外分发的短链。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| file_list | string[] | 否 | 文件名/路径列表 |
| dir_list | string[] | 否 | 目录列表 |
| share_name | string | 否 | 分享标题 |
| passwd | string | 否 | 提取码，留空表示无密码 |

**返回：** `{ success, data: { share_url, ... }, error }`

### 4.8 WorkBuddy 会话资产（2 个）

为前端 BFF 提供「当前 MCP 会话已上传素材」的查询与回读能力，按
`Mcp-Session-Id` 隔离会话状态，不需要把会话 ID 作为工具参数显式传入。

#### `read_session_asset` — 回读已上传素材的字节流

按 repo 相对路径读取当前会话上传的资产并以 Base64 返回。存在的目的：
让远端 BFF（与本 MCP server 不共享文件系统）也能渲染 `<img>` 缩略图
或代理下载。**不做白名单检查**——BFF 必须先调用 `get_session_assets`
确认资产归属后再调用本工具。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| relative_path | string | 是 | repo 内相对路径，必须落在 `<repo>/projects/` 下 |

**返回：**

```json
{
  "success": true,
  "bytes": 123456,
  "data_base64": "...",
  "mime_type": "image/png",
  "filename": "bag.png",
  "relative_path": "projects/demo/assets/images/bag.png"
}
```

> 路径安全由工具强制（限定在 `projects/` 下），但归属检查交由 BFF。

#### `get_session_assets` — 列出当前会话已上传的素材

返回当前 `Mcp-Session-Id` 对应批次内已上传的资产清单（按 sha256 去重）。
前端用来在断点续传后让用户看到「服务端已有什么」，避免重传。

**返回：**

```json
{
  "success": true,
  "assets": [
    {
      "relative_path": "projects/demo/assets/images/bag.png",
      "original_filename": "bag.png",
      "sha256": "...",
      "bytes": 123456,
      "type": "image/png"
    }
  ]
}
```

会话内无任何上传时返回 `{ success: true, assets: [] }`。

### 4.9 非 MCP HTTP 路由（2 个）

这两个不是 `@mcp.tool()` 工具，而是挂在 `:8900` 同源下的额外 HTTP 端点。
同样受 Bearer 鉴权保护（与 MCP 主路径同一中间件）。

#### `GET /render-progress/{job_id}` — 渲染进度 SSE 流

针对 `create_remotion_video_share` 派发的 `render_job_id`，以 Server-Sent
Events 实时推送渲染阶段变更。客户端无需轮询 `get_render_status`，
订阅此流即可获得首次快照 + 后续增量事件，直到终态（`published` /
`failed`）后自动结束。

**事件流示例：**

```
data: {"phase":"snapshot","status":"rendering","render_phase":"remotion_install", ...}

data: {"phase":"update","status":"rendering","render_phase":"remotion_render","progress":0.42, ...}

data: {"phase":"update","status":"uploading","stage":"weiyun_upload", ...}

data: {"phase":"update","status":"published","share_url":"https://share.weiyun.com/..."}

data: {"phase":"done","status":"published"}
```

**SSE 保持：**

- 每 1 秒无事件时推送 `: keep-alive` 注释帧，防止 nginx / 代理断开
  空闲连接。
- 响应头显式 `X-Accel-Buffering: no`，关闭 nginx 的 SSE 缓冲。
- 订阅者断开时自动 `unsubscribe`，无资源泄漏。

**前端调用：**

```javascript
const evtSource = new EventSource(
  `/render-progress/${renderJobId}`,
  // 若 MCP_API_TOKEN 已配置，浏览器需通过 fetch + EventSource polyfill
  // 或由反向代理去掉鉴权头；原生 EventSource 不支持自定义 header
);
```

#### `* /voicebox/mcp/{path:path}` — Voicebox FastMCP 反向代理

将本地 Voicebox FastMCP server（loopback `:17493`）反向代理到 OpenMontage
的 `:8900` 同源 `/voicebox/mcp/*`。客户端只需配一个 `:8900` 入口
（带 Bearer 头）就能同时调用 OpenMontage 工具和 Voicebox 工具，
不必为 Voicebox 单独再开端口和鉴权。

**实现要点：**

- 入口鉴权由 `BearerTokenAuthMiddleware` 统一处理（`X-Voicebox-Client-Id`
  不在入口出现）。
- 转发前会剥离客户端的 `Authorization` 头，因为 Voicebox 使用的是
  `X-Voicebox-Client-Id` 头；Voicebox 信任 loopback 跳（因为 `:8900`
  已经把住了入口）。
- SSE 透传：上游的 SSE 响应原样流式转发到客户端，不缓冲。

**等价直连：**

```bash
# 不走代理时直连 Voicebox
curl -X POST http://127.0.0.1:17493/mcp \
  -H "Content-Type: application/json" \
  -H "X-Voicebox-Client-Id: openmontage-agent" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

# 通过 :8900 代理后等价
curl -X POST http://127.0.0.1:8900/voicebox/mcp \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer $MCP_API_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## 5. 当前可用工具清单

### 免费 / 本地工具（无需 API Key）

| 工具 | 能力 | 说明 |
|------|------|------|
| edge_tts | TTS | 微软 Edge TTS，免费高质量多语言，中文支持优秀 |
| piper_tts | TTS | 离线文字转语音 |
| subtitle_gen | 字幕 | SRT/VTT 字幕生成 |
| video_compose | 视频合成 | FFmpeg 合成编码 |
| video_stitch | 视频拼接 | 多片段拼接、交叉淡入 |
| video_trimmer | 视频裁剪 | 精确剪切 |
| audio_mixer | 音频混合 | 多轨混音、ducking |
| audio_enhance | 音频增强 | 降噪、标准化 |
| color_grade | 色彩校正 | LUT 色彩调整 |
| frame_sampler | 帧采样 | 智能抽帧 |
| scene_detect | 场景检测 | 自动场景边界识别 |
| video_analyzer | 视频分析 | 综合视频分析 |
| diagram_gen | 图表生成 | Mermaid 图表 |
| code_snippet | 代码截图 | 代码高亮渲染 |
| image_selector | 图片选择 | 自动选择最优图片工具 |
| video_selector | 视频选择 | 自动选择最优视频工具 |
| tts_selector | TTS 选择 | 自动选择最优 TTS 工具 |
| direct_clip_search | 素材搜索 | 直接素材检索 |
| clip_search | 素材检索 | CLIP 语义检索 |
| upload_asset | 素材上传 | 将客户端 Base64 图片/视频/音频写入项目 assets 目录 |
| s3_upload | 视频发布 | S3 兼容存储上传，返回公开链接/预签名 URL/下载页 |

### 付费 / 需要 API Key 的工具

| 工具 | 需要的 Key | 用途 |
|------|-----------|------|
| elevenlabs_tts | ELEVENLABS_API_KEY | 高质量 TTS |
| openai_tts | OPENAI_API_KEY | OpenAI TTS |
| flux_image | FAL_KEY | FLUX 图片生成 |
| openai_image | OPENAI_API_KEY | DALL-E 图片 |
| pexels_image / pexels_video | PEXELS_API_KEY | 免费素材 |
| pixabay_image / pixabay_video | PIXABAY_API_KEY | 免费素材 |
| kling_video | FAL_KEY | Kling 视频生成 |
| veo_video | FAL_KEY | Google Veo 视频 |
| runway_video | RUNWAY_API_KEY | Runway 视频 |
| heygen_video | HEYGEN_API_KEY | HeyGen 视频 |
| grok_image / grok_video | XAI_API_KEY | Grok 图片/视频 |
| suno_music | SUNO_API_KEY | AI 音乐生成 |
| music_gen | ELEVENLABS_API_KEY | ElevenLabs 音乐 |
| s3_upload | S3_ACCESS_KEY / S3_SECRET_KEY | S3 兼容存储上传（见 §4.3 s3_upload） |

---

## 6. 典型工作流

### 流程 A：生成一个科普视频（免费路径）

```
1. list_tools(status="available")
   → 确认可用工具

2. get_tool_info("edge_tts")
   → 查看 TTS 输入格式

3. execute_tool("edge_tts", {"text": "大家好...", "voice": "zh-CN-XiaoxiaoNeural", "output_path": "narration.mp3"})
   → 生成旁白音频（免费，中文质量高）。显式指定 `voice` 为 `XiaoxiaoNeural`
   是当前默认；若希望用 `YunxiNeural` 可显式传入但要做好回退准备（见 §8.1 注）

4. execute_tool("subtitle_gen", {"segments": [...], "format": "srt"})
   → 生成字幕

5. execute_tool("video_compose", {"operation": "render", ...})
   → 合成最终视频
```

### 流程 B：通过 Pipeline 驱动（完整流程）

```
1. list_pipelines()
   → 选择 pipeline（如 "animated-explainer"）

2. get_pipeline("animated-explainer")
   → 了解阶段和工具要求

3. get_pipeline_stages("animated-explainer")
   → ["research", "proposal", "script", "scene_plan", "assets", "edit", "compose"]

4. write_checkpoint(project_id="my-video", stage="research",
                    status="completed", artifacts={...})
   → 保存研究阶段成果

5. get_pipeline_status(project_id="my-video", pipeline_type="animated-explainer")
   → 查看已完成阶段和下一阶段

6. get_next_stage → "proposal"
   → 继续下一阶段...
```

### 流程 C：预检付费工具

```
1. dry_run_tool("flux_image", {"prompt": "...", "aspect_ratio": "16:9"})
   → 查看预估成本

2. execute_tool("flux_image", {"prompt": "...", "aspect_ratio": "16:9"})
   → 确认后执行
```

---

## 7. 网络配置

| 项目 | 值 |
|------|------|
| 协议 | HTTP (MCP streamable-http) |
| 端点 | `http://<host>:8900/mcp` |
| 内网 IPv4 | `192.168.20.173:8900` |
| 域名接入点 | `lanes.ymxt.top:8900`（仅解析到 IPv6，公网连通性待确认） |
| 鉴权 | 可选：`Authorization: Bearer <MCP_API_TOKEN>`（配置后必填，否则 401） |
| 方法 | POST |
| Content-Type | application/json |
| 请求格式 | JSON-RPC 2.0 |

**防火墙：** 确保远端机器可以访问 8900 端口。

```bash
# 检查端口是否开放
sudo ufw allow 8900/tcp
# 或
sudo iptables -A INPUT -p tcp --dport 8900 -j ACCEPT
```

**公网暴露前必做：** 配置 `MCP_API_TOKEN`（见第 2 节）。`execute_tool` 可直接调用付费工具并消耗 API 额度，**无鉴权时严禁暴露到公网**。若走路由器端口映射，确认公网 IP 与转发规则；域名 `lanes.ymxt.top` 需先确认 IPv6/域名解析可用。

---

## 8. edge_tts 中文 TTS 使用指南

`edge_tts` 使用微软 Edge 浏览器的免费 TTS 接口，无需 API Key，中文语音质量高，**是中文旁白的首选工具**。

### 8.1 参数说明

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| text | string | 是 | — | 要合成的文本（支持中英文混合） |
| voice | string | 否 | `zh-CN-XiaoxiaoNeural` | 声音名称，见下方声音列表 |
| rate | string | 否 | `+0%` | 语速调节，如 `+20%` 加速、`-10%` 减速 |
| volume | string | 否 | `+0%` | 音量调节，如 `+50%` |
| pitch | string | 否 | `+0Hz` | 音调调节，如 `+5Hz` 升调、`-3Hz` 降调 |
| output_path | string | 否 | `tts_output.mp3` | 输出文件路径（MP3 格式） |

> **关于默认声音 `XiaoxiaoNeural`（vs 上游 `YunxiNeural`）的工程说明：**
> 上游 `edge_tts` 工具的默认声音是 `zh-CN-YunxiNeural`，但微软 edge-tts
> 服务在许多出口 IP 上会拒绝该声音（返回 `NoAudioReceived`）。MCP 包装层
> 把默认值改为 `zh-CN-XiaoxiaoNeural` 以保证开箱即用。如果业务明确需要
> `YunxiNeural`，显式传 `voice="zh-CN-YunxiNeural"`，遇到 NoAudioReceived
> 时回退到 `XiaoxiaoNeural` 即可。

### 8.2 推荐中文声音

| 声音名 | 性别 | 风格 | 适合场景 |
|--------|------|------|---------|
| `zh-CN-YunxiNeural` | 男 | 阳光活泼 | 科普解说、年轻风格 |
| `zh-CN-XiaoxiaoNeural` | 女 | 温暖自然 | 旁白、新闻播报 |
| `zh-CN-YunjianNeural` | 男 | 激情有力 | 体育、热血场景 |
| `zh-CN-YunyangNeural` | 男 | 专业沉稳 | 新闻、正式场合 |
| `zh-CN-XiaoyiNeural` | 女 | 活泼可爱 | 卡通、轻松风格 |
| `zh-CN-YunxiaNeural` | 男 | 童声可爱 | 儿童、趣味场景 |
| `zh-CN-liaoning-XiaobeiNeural` | 女 | 东北方言 | 方言趣味 |
| `zh-CN-shaanxi-XiaoniNeural` | 女 | 陕西话 | 方言趣味 |

常用英文声音：`en-US-AndrewNeural`（男）、`en-US-AvaNeural`（女）、`en-US-GuyNeural`（男，新闻）。

### 8.3 调用示例

#### 基础中文旁白

```json
{
  "tool_name": "edge_tts",
  "inputs": {
    "text": "中国科技，正在加速。5G基站全球占比超60%。",
    "voice": "zh-CN-YunxiNeural",
    "output_path": "narration.mp3"
  }
}
```

#### 调整语速和音调

```json
{
  "tool_name": "edge_tts",
  "inputs": {
    "text": "大家好，欢迎收看本期节目。",
    "voice": "zh-CN-XiaoxiaoNeural",
    "rate": "-10%",
    "pitch": "+2Hz",
    "output_path": "intro.mp3"
  }
}
```

#### 中英文混合

```json
{
  "tool_name": "edge_tts",
  "inputs": {
    "text": "OpenMontage是一个AI视频生产系统，支持多语言旁白。",
    "voice": "zh-CN-XiaoxiaoNeural",
    "output_path": "mixed.mp3"
  }
}
```

### 8.4 完整 MCP JSON-RPC 调用

```
POST /mcp HTTP/1.1
Content-Type: application/json
Authorization: Bearer <MCP_API_TOKEN>   # 若已开启鉴权则必填，否则 401

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "execute_tool",
    "arguments": {
      "tool_name": "edge_tts",
      "inputs": {
        "text": "从追赶到引领，中国创新从未停步。",
        "voice": "zh-CN-YunxiNeural",
        "output_path": "projects/my-video/assets/narration.mp3"
      }
    }
  }
}
```

返回示例：

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [{"type": "text", "text": "{...}"}],
    "structuredContent": {
      "success": true,
      "data": {
        "provider": "edge_tts",
        "voice": "zh-CN-YunxiNeural",
        "text_length": 15,
        "output": "projects/my-video/assets/narration.mp3",
        "format": "mp3"
      },
      "artifacts": ["projects/my-video/assets/narration.mp3"],
      "cost_usd": 0.0,
      "duration_seconds": 1.2,
      "model": "edge-tts/zh-CN-YunxiNeural"
    }
  }
}
```

### 8.5 注意事项

- **免费但需网络**：edge_tts 需要访问微软服务器，服务器本机需能访问 `speech.platform.bing.com`
- **输出格式**：固定为 MP3，如需 WAV 可通过 `audio_mixer` 或 ffmpeg 转换
- **文本长度**：单次建议不超过 5000 字符，长文本建议分段合成后用 `audio_mixer` 拼接
- **对比其他 TTS**：edge_tts 免费、中文质量高；piper_tts 离线但中文声音有限；google_tts / openai_tts 需付费 API

---

## 9. 故障排除

| 问题 | 检查 |
|------|------|
| 连接被拒 | `curl http://192.168.20.173:8900/mcp` 确认服务可达 |
| **401 Unauthorized** | 已配置 `MCP_API_TOKEN`，但请求未带 `Authorization: Bearer <token>`；检查客户端 headers 配置 |
| 工具 unavailable | `get_tool_info(tool_name)` 查看 dependencies，确认 API Key / 二进制是否就绪 |
| 执行超时 | 部分工具（视频生成、渲染）耗时较长，增大 OpenClaw 的工具超时设置 |
| IPv6 不通 | 确认 `ip -6 addr` 有全局地址，防火墙放行 IPv6 的 8900 端口 |
| edge_tts 网络错误 | 服务器需能访问 `speech.platform.bing.com`，检查代理设置 |
| edge_tts 声音不存在 | 用 `edge-tts --list-voices` 查看可用声音列表 |

#### `create_remotion_video_share` — 生成并分享照片视频（异步 / 非阻塞）

先通过 `upload_asset` 或 `upload_asset_chunk` 上传一张或多张图片，再调用此工具。
工具不接收 `image_paths` 或 `session_id`，会从当前 Streamable HTTP 会话读取打开的
批次，构造最小 `edit_decisions` 与 `asset_manifest`，并明确锁定 Remotion 渲染。
可选参数：`project_id`、`duration_per_image`（默认 3 秒）、`aspect_ratio`（默认
`9:16`）和 `title`。

**此工具是非阻塞的。** 它仅做输入校验、领取渲染任务（`render_job_id`），并把
`render → weiyun_upload → weiyun_share_link` 整条流水线派发到后台线程，**立即返回**
`status=queued` 与 `render_job_id`，不会等到视频发布完成。客户端随后用
`get_render_status(render_job_id)` 轮询，直到 `status` 变为终态（`published` 或
`failed`）：成功时取 `share_url`，失败时取 `error` 与 `stage`（失败阶段）。

后台流水线使用已跟踪的 `weiyun_upload` 上传 MP4（返回 `file_id`），再调用
token 化的 `weiyun_share_link` 生成面向客户的 `share_url`。渲染、上传、分享失败会在
`get_render_status` 中体现为 `stage=validation|render|weiyun_upload|weiyun_share|
background_crash`；`weiyun_upload` / `weiyun_share` 阶段失败时仍保留 `video_path`，
客户端可取回半成品视频。

业务日志写入 `logs/session_video.log`，采用轮转 JSON 记录；只记录短 session hash，
不会记录完整会话 ID、Base64 媒体、token 或 cookie。

#### `get_render_status` — 轮询渲染进度（配合上面的异步工具）

按 `render_job_id` 轮询 `create_remotion_video_share` 派发的渲染任务进度与结果。

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| render_job_id | string | 是 | 由 `create_remotion_video_share` 返回的渲染任务 ID |

**返回：**

```json
{
  "success": true,
  "render_job_id": "<hex>",
  "status": "rendering",
  "stage": "render",
  "error": "...",
  "batch_id": "...",
  "project_id": "...",
  "video_path": "projects/.../renders/...mp4",
  "share_url": "https://share.weiyun.com/...",
  "updated_at": "2026-..."
}
```

`status` 取值：`queued`、`rendering`、`rendered`、`uploading`、`published`（成功终态）、
`failed`（失败终态）。成功时 `share_url` 为微云分享链接；失败（或对应阶段）时 `stage`
给出失败阶段、`error` 给出人类可读原因，`video_path` 在上传/分享阶段失败时仍保留。

若传入的 `render_job_id` 找不到对应任务，返回
`{"success": false, "error": "No render job found for render_job_id '...'"}`。
