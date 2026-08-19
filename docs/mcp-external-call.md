# OpenMontage MCP 外部调用文档

> 本文档说明如何从 **FrameFlow BFF**（`POST /api/mcp`）或**直连 MCP**（`POST /mcp`）调用 OpenMontage 渲染管线。机器 B（`lanes.ymxt.top:8900`）渲染失败的已知问题见末尾「⚠️ Babel/Standalone 已知问题」。

---

## 概述

OpenMontage 渲染管线通过 MCP（Model Context Protocol）暴露工具。外部调用有两种路径：

| 路径 | 端点 | 鉴权 | 适用场景 |
|---|---|---|---|
| **BFF 代理** | `POST http://<bff-host>:8080/api/mcp` | 会话 Cookie（微信登录）或 `DEV_LOGIN` | 浏览器前端（FrameFlow） |
| **直连 MCP** | `POST http://<lanes>:8900/mcp` | `Authorization: Bearer <token>` | 服务器间调用、脚本、Claude Code |

---

## 核心工具清单

| 工具 | 用途 | 会话相关 |
|---|---|---|
| `upload_asset` | 上传单张图片（小文件） | ✅ 需要复用同一 MCP 会话 |
| `upload_asset_chunk` | 上传大图片（分块，支持断点续传） | ✅ 需要复用同一 MCP 会话 |
| `create_remotion_video_share` | 触发 Remotion 视频渲染（异步，非阻塞） | ✅ 需要复用同一 MCP 会话 |
| `get_render_status` | 轮询渲染任务状态 | ✅ 复用同一 MCP 会话 |
| `get_session_assets` | 列出当前会话已上传图片 | ✅ |
| `retry_render_publish` | 重试失败的视频发布 | ✅ |
| `weiyun_upload` | 上传到腾讯微云 |  |
| `weiyun_gen_share_link` | 生成微云分享链接 |  |

---

## 调用方式 A：BFF 代理（浏览器前端路径）

### 认证

**正式模式（`AUTH_REQUIRED=true`）**：微信授权后，浏览器持有一个会话 Cookie（`ff_sid`），BFF 据此关联到用户的 MCP 会话。无需手动传 Token。

**开发模式（`AUTH_REQUIRED=false`）**：匿名访问。

### 请求格式

```
POST /api/mcp
Content-Type: application/json
Cookie: ff_sid=<session-id>   # 仅正式模式需要
```

**请求体结构**：

```json
{
  "tool": "<工具名>",
  "args": {
    // 工具参数（见下方各工具说明）
  }
}
```

### 响应格式

BFF 提取工具返回结构化 JSON（去除外层 MCP 包装）：

```json
{
  "success": true,
  "data": { ... }
}
```

出错时：

```json
{
  "success": false,
  "error": "..."
}
```

---

## 调用方式 B：直连 MCP（服务器间调用）

### 认证

```
Authorization: Bearer <MCP_API_TOKEN>
```

### 请求格式（MCP JSON-RPC 2.0）

```json
POST http://lanes.ymxt.top:8900/mcp
Content-Type: application/json
Authorization: Bearer <token>

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "<工具名>",
    "arguments": {
      // 工具参数
    }
  }
}
```

### 响应格式

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "..."   // JSON 字符串，需 JSON.parse()
      }
    ]
  }
}
```

---

## 工具详解

### 1. upload_asset — 上传单张图片（小文件）

> **注意**：需要复用同一 MCP 会话（`Mcp-Session-Id`），否则图片与后续渲染无法绑定。

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file_path` | string | ✅ | 本地文件路径 |
| `project_id` | string | 否 | 项目 ID（不填则自动生成） |
| `content_type` | string | 否 | MIME 类型，如 `image/jpeg` |

**示例（BFF 路径）**：

```bash
curl -X POST http://localhost:8080/api/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "upload_asset",
    "args": {
      "file_path": "/path/to/image.jpg",
      "content_type": "image/jpeg"
    }
  }'
```

**响应示例**：

```json
{
  "success": true,
  "data": {
    "id": "img-20250819-001",
    "name": "image.jpg",
    "type": "image",
    "path": "/opt/OpenMontage/projects/abc123/assets/images/image.jpg",
    "url": null
  }
}
```

---

### 2. upload_asset_chunk — 分块上传大图片

> 超过约 5MB 的文件建议用此工具，支持断点续传。分块大小建议 2MB。

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file_path` | string | ✅ | 本地文件路径 |
| `chunk_index` | int | ✅ | 当前块序号（从 0 开始） |
| `total_chunks` | int | ✅ | 总块数 |
| `project_id` | string | 否 | 项目 ID |

**示例**：

```bash
# 假设 10MB 文件，分 5 块上传（每块 2MB）
for i in $(seq 0 4); do
  curl -X POST http://localhost:8080/api/mcp \
    -H "Content-Type: application/json" \
    -d "{
      \"tool\": \"upload_asset_chunk\",
      \"args\": {
        \"file_path\": \"/path/to/large.jpg\",
        \"chunk_index\": $i,
        \"total_chunks\": 5
      }
    }"
done
```

---

### 3. create_remotion_video_share — 触发视频渲染（异步）

> **非阻塞**：`create_remotion_video_share` 立即返回 `render_job_id`。轮询 `get_render_status(render_job_id)` 追踪进度。

**参数**：

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|---|---|---|---|---|
| `script_id` | string | ✅（无 code 时） | — | 预设脚本：`photo-ken-burns`、`cinematic-montage`、`ecommerce-product-demo` |
| `duration_per_image` | float | 否 | `3.0` | 每张图片展示秒数（1~30） |
| `aspect_ratio` | string | 否 | `9:16` | 画幅：`9:16`、`16:9`、`1:1` |
| `title` | string | 否 | — | 视频标题（用于微云文件名） |
| `code` | string | ✅（自定义时） | — | 自定义 TSX 源码（需 `CUSTOM_COMPOSITION_ENABLED=true`） |
| `project_id` | string | 否 | — | 项目 ID |
| `queue_owner_id` | string | 否 | — | 队列归属标识 |

**示例 A：预设 Ken Burns 脚本**：

```bash
curl -X POST http://localhost:8080/api/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "create_remotion_video_share",
    "args": {
      "script_id": "photo-ken-burns",
      "duration_per_image": 3.0,
      "aspect_ratio": "9:16",
      "title": "我的视频"
    }
  }'
```

**响应**：

```json
{
  "success": true,
  "data": {
    "render_job_id": "job-abc123",
    "status": "queued",
    "estimated_duration_seconds": 60
  }
}
```

**示例 B：自定义合成脚本**：

```bash
curl -X POST http://localhost:8080/api/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "create_remotion_video_share",
    "args": {
      "code": "import {AbsoluteFill} from \"remotion\";\nexport const MyComp = ({images}) => <AbsoluteFill style={{background:\"#000\"}} />;",
      "duration_per_image": 3.0,
      "aspect_ratio": "9:16",
      "title": "自定义视频"
    }
  }'
# 注意：需 BFF .env 设置 CUSTOM_COMPOSITION_ENABLED=true
```

---

### 4. get_render_status — 轮询渲染状态

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `render_job_id` | string | ✅ | `create_remotion_video_share` 返回的 job ID |

**轮询示例**：

```bash
while true; do
  STATUS=$(curl -s -X POST http://localhost:8080/api/mcp \
    -H "Content-Type: application/json" \
    -d '{
      "tool": "get_render_status",
      "args": { "render_job_id": "job-abc123" }
    }')
  echo "$STATUS"
  # 提取 status 字段判断
  # pending | rendering | uploading | published | failed
  sleep 5
done
```

**典型状态流转**：

```
pending → rendering → uploading → published → done
                                    ↘ failed (可 retry_render_publish)
```

**published 状态响应示例**：

```json
{
  "success": true,
  "data": {
    "status": "published",
    "render_job_id": "job-abc123",
    "share_url": "https://...",   // 微云分享链接
    "video_url": "...",
    "expires_at": "2025-08-20T..."
  }
}
```

---

### 5. get_session_assets — 列出已上传图片

**参数**：无（隐式读取当前 MCP 会话已上传图片）

**示例**：

```bash
curl -X POST http://localhost:8080/api/mcp \
  -H "Content-Type: application/json" \
  -d '{"tool": "get_session_assets", "args": {}}'
```

**响应**：

```json
{
  "success": true,
  "data": {
    "assets": [
      {"id": "img-001", "name": "a.jpg", "type": "image", "path": "/opt/OpenMontage/projects/.../a.jpg"},
      {"id": "img-002", "name": "b.jpg", "type": "image", "path": "/opt/OpenMontage/projects/.../b.jpg"}
    ],
    "total": 2
  }
}
```

---

### 6. retry_render_publish — 重试失败发布

**参数**：

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `render_job_id` | string | ✅ | 失败任务的 job ID |

**示例**：

```bash
curl -X POST http://localhost:8080/api/mcp \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "retry_render_publish",
    "args": { "render_job_id": "job-abc123" }
  }'
```

---

## 完整调用示例：上传图片 → 渲染 → 轮询 → 获取链接

```bash
BFF="http://localhost:8080/api/mcp"

# 1. 上传图片（复用同一会话）
for img in a.jpg b.jpg c.jpg; do
  curl -s -X POST "$BFF" \
    -H "Content-Type: application/json" \
    -d "{\"tool\": \"upload_asset\", \"args\": {\"file_path\": \"/tmp/$img\"}}"
done

# 2. 触发渲染
RESULT=$(curl -s -X POST "$BFF" \
  -H "Content-Type: application/json" \
  -d '{
    "tool": "create_remotion_video_share",
    "args": {
      "script_id": "photo-ken-burns",
      "duration_per_image": 3,
      "aspect_ratio": "9:16",
      "title": "测试视频"
    }
  }')
echo "$RESULT"

JOB_ID=$(echo "$RESULT" | python3 -c "import sys,json; print(json.load(sys.stdin)['data']['render_job_id'])")

# 3. 轮询直到 published 或 failed
while true; do
  STATUS=$(curl -s -X POST "$BFF" \
    -H "Content-Type: application/json" \
    -d "{\"tool\": \"get_render_status\", \"args\": {\"render_job_id\": \"$JOB_ID\"}}")
  STATE=$(echo "$STATUS" | python3 -c "import sys,json; d=json.load(sys.stdin)['data']; print(d.get('status'), d.get('share_url',''))")
  echo "状态: $STATE"
  case $(echo "$STATE" | awk '{print $1}') in
    published) echo "完成: $(echo "$STATE" | awk '{print $2}')"; break ;;
    failed)   echo "失败，需 retry"; break ;;
    *)         sleep 5 ;;
  esac
done
```

---

## ⚠️ Babel/Standalone 已知问题

### 问题描述

当调用 `create_remotion_video_share` 且 `script_id=photo-ken-burns`（`renderer_family=animation-first`）或自定义合成时，Remotion 渲染阶段报错：

```
Module not found: @babel/standalone
```

### 根因

`remotion-composer/package.json` 依赖 `@babel/standalone`，本机 `node_modules` 已正确安装（BFF 所在的机器 A），渲染正常。

机器 B（`lanes.ymxt.top:8900`）的 `node_modules` 缺失此包，导致在该机器执行的渲染任务失败。

### 修复方法

在机器 B 执行：

```bash
cd /path/to/remotion-composer
npm install
```

BFF 重启**不会**修复此问题（BFF 只是转发器，不执行渲染）。

### 当前状态确认

- **机器 A**（BFF / 本机 `/opt/OpenMontage`）：✅ `node_modules/@babel/standalone/` 存在，渲染正常
- **机器 B**（`lanes.ymxt.top:8900`）：❌ 需运维执行 `npm install`
- **BFF 侧代码**：✅ 无问题，转发正常

---

## 相关文件索引

| 文件 | 说明 |
|---|---|
| `mcp_server.py` | MCP 工具实现（`create_remotion_video_share`、`get_render_status` 等） |
| `frameflow/bff/main.go` | BFF Gin 引擎、路由、会话亲和性 |
| `frameflow/bff/internal/mcp/client.go` | MCP Streamable-HTTP 客户端封装 |
| `frameflow/bff/.env.example` | BFF 配置模板（`MCP_BASE_URL`、`MCP_API_TOKEN` 等） |
| `remotion-composer/package.json` | Remotion 依赖（`@babel/standalone` 在此声明） |
| `remotion-composer/src/CustomComposition.tsx` | 自定义 TSX 运行时编译组件 |
| `tools/video/video_compose.py` | 渲染器核心（`_render`、`_remotion_render`） |
| `docs/mcp-remote-tool-list.json` | MCP 工具完整清单（JSON Schema） |
