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

共 14 个工具，分为 5 组。

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

### 4.3 素材管理（1 个）

#### `upload_asset` — 上传项目素材

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

---

## 6. 典型工作流

### 流程 A：生成一个科普视频（免费路径）

```
1. list_tools(status="available")
   → 确认可用工具

2. get_tool_info("edge_tts")
   → 查看 TTS 输入格式

3. execute_tool("edge_tts", {"text": "大家好...", "voice": "zh-CN-YunxiNeural", "output_path": "narration.mp3"})
   → 生成旁白音频（免费，中文质量高）

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
| voice | string | 否 | `zh-CN-YunxiNeural` | 声音名称，见下方声音列表 |
| rate | string | 否 | `+0%` | 语速调节，如 `+20%` 加速、`-10%` 减速 |
| volume | string | 否 | `+0%` | 音量调节，如 `+50%` |
| pitch | string | 否 | `+0Hz` | 音调调节，如 `+5Hz` 升调、`-3Hz` 降调 |
| output_path | string | 否 | `tts_output.mp3` | 输出文件路径（MP3 格式） |

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
    "voice": "zh-CN-YunxiNeural",
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
