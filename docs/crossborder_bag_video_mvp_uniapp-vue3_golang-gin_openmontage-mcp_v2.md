# 跨境箱包参考视频重构 MVP — uni-app Vue3 + Go/Gin + OpenMontage MCP 架构版 (v2)

> 目标读者:Claude Code / Codex / Gemini CLI / OpenClaw / 其他代码大模型
>
> 本文件 **v2** 替代上一版 `Vue3 + FastAPI + OpenMontage Adapter + Remotion` 方案 **以及** `v1 (uni-app + Gin + stdio MCP)` 方案。
>
> v1 → v2 主要变化(基于 `mcp_server.py` 与 `MCP_SERVER.md` 实际源码复核):
>
> 1. **MCP transport**:`stdio` → **`streamable-http`**(默认 `0.0.0.0:8900/mcp`)
> 2. **鉴权**:增加可选 Bearer Token(`MCP_API_TOKEN`),未配置时仅内网模式
> 3. **MCP 工具命名**:`create_project / run_tool / render_video / get_job_status / list_jobs` → **真实存在的** `list_tools / execute_tool / dry_run_tool / create_remotion_video_share / get_render_status`
> 4. **Job 模型归属**:`MCP Server` 不持久化 job,Gin 必须在 SQLite 自维护 job 表
> 5. **Render 入口**:不走自建 Remotion,而走 **target_blueprint → edit_decisions Adapter → `execute_tool(video_compose)`** 或 **`create_remotion_video_share(custom-composition)`**
> 6. **Schema 复用**:`reference_blueprint.json` 通过 Adapter 归一化 `video_analysis_brief.json`(OpenMontage 已有);`target_blueprint.json` 仍是新建,但渲染前必须转换为 `edit_decisions.json`
> 7. **MCP Server 生命周期**:独立常驻进程,**不再** 由 Gin 启动/拉起
>
> MVP 原则:**先跑通单条视频生产闭环,再扩展到智能体批量生产。**

---

## v2 修订摘要

| 章节 | v1 假设 | v2 实际/调整 | 严重度 |
|---|---|---|---|
| §4.4 MCP transport | stdio | **streamable-http (HTTP JSON-RPC 2.0, port 8900)** | 🔴 必须改 |
| §4.4 鉴权 | 无 | 可选 Bearer Token,生产部署必配 | 🔴 必须改 |
| §12 Client 接口 | `CreateProject / RunTool / GetJobStatus / ListJobs` | 全部删除或重命名:对应到 `execute_tool / get_render_status` | 🔴 必须改 |
| §25 Render 路径 | 自建 Remotion 渲染器 | 走 OpenMontage `video_compose`(推荐)或 `custom-composition` | 🔴 必须改 |
| §33 MCP 流程 | `list_capabilities → create_project → run_tool → get_job_status` | `list_tools → execute_tool(video_analyzer) → (sync result)`;render 用 `create_remotion_video_share` + 轮询 `get_render_status` | 🔴 必须改 |
| §34 MCP 生命周期 | Gin 启动时拉起 | **独立常驻进程**,Gin 只连不断 | 🟡 强烈建议 |
| §10 Schema | 新建 reference_blueprint | 复用 `video_analysis_brief`,Adapter 归一化 | 🟡 强烈建议 |
| §11 Schema | 新建 target_blueprint | 新建但必须含 `→ edit_decisions` 转换 Adapter | 🟡 强烈建议 |
| §37 Mock | 加载 fixture 走 MCP | Mock 客户端完全不连 MCP,纯本地 fixture | 🟢 可选 |

---

# 1. 产品目标

用户提供:

1. 一个参考视频 URL
2. 自己的箱包产品基础资料
3. 自己的产品图片 / 视频素材

系统完成:

```text
参考视频 URL
→ OpenMontage MCP 分析
→ reference_blueprint.json (从 video_analysis_brief 归一化)
→ 商品映射
→ target_blueprint.json
→ Web Scene Review
→ Preview
→ Render Adapter (target_blueprint → edit_decisions)
→ final.mp4
```

一句话定义:

> **粘贴一个优秀的箱包获客视频,上传自己的商品资料和素材,自动生成自己的营销视频,并允许逐场景调整。**

---

# 2. 未来演进目标

MVP 完成后,保持业务 API 不变,新增:

```text
Excel
→ OpenClaw
→ 多 Agent
→ Gin API / MCP
→ 批量 Blueprint
→ 批量 Render
```

未来核心生产单位:

```text
SKU × Scenario × Variant
```

例如:

```text
20 SKU
× 5 场景
× 3 Hook 版本
= 300 条视频
```

因此本版所有接口必须考虑:

- 可程序化调用
- 不依赖 Web UI
- 单 Scene 可独立修改
- 单任务可重试
- Render 可独立执行
- 项目状态归 Gin 持有
- Agent 不拥有项目状态

---

# 3. MVP 范围

## 3.1 必须实现

### 用户输入

```text
Reference Video URL
Product Name
Product Description
Features
Price
Offer
CTA
Target Market
Product Images
Product Videos
```

### 视频分析

通过:

```text
OpenMontage MCP (streamable-http)
→ execute_tool(tool_name="video_analyzer", inputs={source: refURL, analysis_depth: "standard"})
→ video_analysis_brief.json (本地落盘)
→ BlueprintNormalizer
→ reference_blueprint.json
```

实现:

- 创建 OpenMontage project (Gin 侧维护,MCP 不感知)
- 视频下载(yt-dlp,本地完成)
- transcript 获取(YouTube 字幕 / faster-whisper)
- scene / keyframe / pacing 分析
- hook 分析(LLM 二阶段)
- 产品展示结构分析
- CTA 分析

### 标准化

必须生成:

```text
reference_blueprint.json (MVP 自有 schema)
= BlueprintNormalizer(video_analysis_brief)
```

### 商品映射

生成:

```text
target_blueprint.json
```

### Web Scene Review

必须支持:

- Scene Cards
- 顺序调整(↑↓ 按钮优先,H5 可追加拖拽)
- 替换素材
- 编辑 headline
- 编辑 voiceover
- 编辑 duration
- 删除 Scene
- 单 Scene AI rewrite
- 保存

### Preview

MVP 使用前端轻量 Preview,不要求和最终 MP4 100% 一致。

### Render

通过:

```text
target_blueprint.json
→ RenderAdapter (Blueprint → EditDecisions)
→ execute_tool(tool_name="video_compose", inputs={operation:"render", edit_decisions:..., asset_manifest:..., output_path:...})
→ final.mp4
```

或备选:

```text
target_blueprint.json
→ RenderAdapter (Blueprint → TSX Code)
→ create_remotion_video_share(code=<tsx>, aspect_ratio="9:16", ...)
→ final.mp4
```

输出固定:

```text
1080 × 1920
9:16
30fps
H.264 MP4
```

---

## 3.2 明确不做

禁止扩展为完整视频平台。

MVP 不做:

```text
完整时间轴 NLE
多轨编辑
AI 换脸
AI 换物
逐帧 inpainting
口型同步
复杂音频工程
SaaS 多租户
支付
自动发布
广告投放
长视频
多人协作
复杂权限
Agent 多机调度
Excel 批量
OpenClaw Multi-Agent
```

后两项属于 Phase 2,不属于当前 MVP。

---

# 4. 技术栈

## 4.1 Frontend

```text
uni-app
Vue 3
Pinia
```

MVP 首要验收端:

```text
H5
```

不要同时把:

```text
H5
微信小程序
App
```

全部作为第一版验收目标。未来兼容小程序即可。

## 4.2 Backend

```text
Go 1.23+
Gin
```

Gin 负责:

```text
Project
Product
Asset
Scene
Blueprint
Job
Render
MCP 调度
```

## 4.3 OpenMontage

本项目不 import OpenMontage Python package。

只通过:

```text
OpenMontage MCP Server (streamable-http)
```

调用。

必须创建:

```text
OpenMontageMCPClient
```

作为唯一集成入口。

## 4.4 MCP(v2 修正)

### Transport

```text
streamable-http
HTTP POST
JSON-RPC 2.0
Content-Type: application/json
默认端口 8900
```

### 端点

```text
http://<host>:8900/mcp
```

### 鉴权

```text
可选 Bearer Token
Header: Authorization: Bearer <MCP_API_TOKEN>
```

`MCP_API_TOKEN` 未配置时,服务端日志告警,客户端可省略 Header(仅限内网)。

### 客户端类型

业务代码不直接操作 HTTP / JSON-RPC 协议细节。所有 MCP 调用走:

```text
internal/mcp/openmontage/client.go (Go interface)
  ↳ internal/mcp/transport/http_jsonrpc.go (JSON-RPC 2.0 编码)
    ↳ net/http (实际请求)
```

禁止业务 Handler 出现 `tool_name="video_analyzer"`、`render_job_id` 等 MCP 协议字段。

### 已知 MCP 顶层工具清单

| 类别 | 工具 |
|---|---|
| 工具发现 | `list_tools` / `get_tool_info` / `get_capabilities` / `get_provider_menu` |
| 工具执行 | `execute_tool` / `dry_run_tool` |
| 素材 | `upload_asset` / `upload_asset_chunk` / `read_session_asset` / `get_session_assets` |
| 渲染 | `create_remotion_video_share` (异步) + `get_render_status` |
| 旁白/字幕 | `edge_tts` / `burn_subtitles` / `clone_voice` / `list_cloned_voices` |
| 发布 | `s3_upload` / `rsync_upload_artifact` / `export_bundle` |
| Pipeline | `list_pipelines` / `get_pipeline` / `get_pipeline_stages` |
| Checkpoint | `read_checkpoint` / `get_latest_checkpoint` / `get_pipeline_status` / `write_checkpoint` |

### 通过 `execute_tool` 可间接调用的内部工具

| 工具 | 用途 |
|---|---|
| `video_analyzer` | URL/本地视频综合分析 → `video_analysis_brief.json` |
| `video_downloader` | yt-dlp 下载 |
| `transcript_fetcher` | YouTube/TikTok 字幕抓取 |
| `transcriber` | faster-whisper 本地转写 |
| `scene_detect` | 场景边界识别 |
| `frame_sampler` | 智能抽帧 |
| `audio_energy` | 音频能量曲线 |
| `video_compose` | **Render 主路径**(operation=render) |

### 可替换性

```text
OpenMontageClient interface
  → OpenMontageHTTPMCPClient (默认)
  → OpenMontageZhMCPClient (未来)
  → MockOpenMontageClient (Phase 1-3 验证用,完全不连 MCP)
```

## 4.5 Rendering

```text
OpenMontage video_compose (Render 主路径)
   或
OpenMontage create_remotion_video_share (custom-composition 模式)
```

原则:

```text
target_blueprint.json
→ RenderAdapter
→ video_compose(operation=render, edit_decisions=...)
→ MP4
```

Remotion 不承担 AI 判断。

## 4.6 Storage

MVP:

```text
本地文件系统
+
SQLite (Gin 持有,WAL 模式,MaxOpenConns=1)
```

SQLite 保存:

```text
projects
assets
jobs
```

Blueprint 仍保存 JSON 文件。

目录:

```text
data/
  projects/
    {project_id}/
      project.json
      reference_blueprint.json
      target_blueprint.json
      edit_decisions.json  (Render Adapter 落盘,渲染中间产物)
      assets/
      previews/
      renders/
```

---

# 5. 总体架构

```text
                       ┌────────────────────┐
                       │    uni-app H5      │
                       │                    │
                       │ Create Project     │
                       │ Scene Review       │
                       │ Asset Picker       │
                       │ Preview            │
                       └─────────┬──────────┘
                                 │
                                 │ REST
                                 ↓
                       ┌────────────────────┐
                       │      Gin API       │
                       │                    │
                       │ Project Service    │
                       │ Scene Service      │
                       │ Asset Service      │
                       │ Job Service        │
                       │ Render Service     │
                       └────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────────┐
              ↓                 ↓                     ↓
   OpenMontage MCP      LLM HTTP API          Render Adapter
   (streamable-http)         │                     │
              │               ↓                     ↓
              ↓         Product Mapper       target_blueprint
   reference analysis   (LLM JSON only)       → edit_decisions
                                                   ↓
                                          execute_tool(video_compose,
                                            operation=render, ...)
                                                   ↓
                                              final.mp4
```

---

# 6. Phase 2 架构预留

未来:

```text
                         Excel
                           ↓
                       OpenClaw
                           ↓
                    Multi-Agent Layer
                           ↓
                         Gin API
                           ↓
       ┌───────────────────┼───────────────────┐
       ↓                   ↓                   ↓
OpenMontage MCP      Scene Service        Render Service
```

重要:

> Web 和 OpenClaw 都只是 Gin API 的 Client。

禁止形成:

```text
Web 一套业务逻辑
OpenClaw 另一套业务逻辑
```

---

# 7. 核心状态原则

## 7.1 Gin 拥有项目状态

项目状态必须保存在:

```text
Gin
SQLite
JSON files
```

不保存在:

```text
OpenMontage MCP Server 进程内存
OpenClaw
LLM Context
```

**关键**:MCP Server 的 `render_job_id` 只在 MCP Server 进程内有效。**Gin 必须把 render_job_id 落 SQLite**,且假设 MCP Server 不重启(重启后旧 job_id 会失效,需重新发起)。

## 7.2 OpenMontage 是能力提供者

OpenMontage 负责:

```text
Reference Video
→ Analysis (video_analyzer)
→ Scene detection / keyframe / transcript
```

不负责:

```text
业务 SKU 状态
Scene 最终顺序
用户修改状态
价格真实性
Offer
CTA
```

## 7.3 MCP 是 transport,不是业务模型

MCP 工具调用结果必须经过:

```text
OpenMontage MCP
→ Adapter
→ Normalizer
→ ReferenceBlueprint
```

业务层不直接使用 MCP 原始返回值。

## 7.4 Render 流程中的职责切割

| 模块 | 负责 | 不负责 |
|---|---|---|
| Gin Render Service | 调用 Render Adapter、落 job 表、轮询 `get_render_status` | 渲染本身 |
| Render Adapter | `target_blueprint` → `edit_decisions` 转换(或 → TSX) | 实际渲染 |
| OpenMontage MCP | 实际渲染 | 业务语义 |

---

# 8. 核心数据模型

## 8.1 Project

```json
{
  "id": "proj_001",
  "status": "draft",
  "reference_url": "https://youtube.com/...",
  "scenario": "airport_travel",
  "variant": "default",
  "mcp_session_id": "abc123...",
  "product": {
    "sku": "BAG-001",
    "name": "40L Travel Backpack",
    "description": "Carry-on friendly travel backpack",
    "features": [
      "40L capacity",
      "Water resistant",
      "Separate shoe compartment",
      "17-inch laptop compartment"
    ],
    "price": "$39.99",
    "offer": "Free Shipping",
    "cta": "Shop Now",
    "target_market": "US women 25-45"
  }
}
```

必须预留:

```text
scenario
variant
sku
mcp_session_id  (MCP session 隔离标识,upload_asset 需要)
```

## 8.2 Asset

```json
{
  "id": "asset_001",
  "project_id": "proj_001",
  "type": "image",
  "path": "/data/projects/proj_001/assets/bag-front.jpg",
  "label": "front",
  "description": "Black travel backpack front view",
  "mcp_uploaded": true
}
```

支持:

```text
image
video
```

---

# 9. Scene Type

第一版只允许:

```text
hook
pain_point
product_reveal
feature_demo
lifestyle
social_proof
offer
cta
```

fallback:

```text
feature_demo
```

禁止第一版扩展无限 Scene Type。

---

# 10. reference_blueprint.json

```json
{
  "source": {
    "url": "https://youtube.com/...",
    "duration": 38.4,
    "platform": "youtube"
  },
  "strategy": {
    "video_type": "problem_solution_product_demo",
    "target_customer": "female traveler",
    "core_hook": "travel frustration",
    "core_sales_logic": "pain -> reveal -> proof -> offer -> CTA"
  },
  "scenes": [
    {
      "id": "ref_scene_01",
      "order": 1,
      "start": 0,
      "end": 2.8,
      "duration": 2.8,
      "type": "hook",
      "sales_role": "pain hook",
      "visual_description": "traveler struggling with multiple bags",
      "original_text": "Stop traveling like this",
      "pacing": "fast"
    }
  ]
}
```

**生成方式(v2 修正)**:

```text
OpenMontage MCP
  → execute_tool(tool_name="video_analyzer",
                  inputs={source: refURL, analysis_depth: "standard"})
  → video_analysis_brief.json (本地落盘,OpenMontage 已有 schema)
  → BlueprintNormalizer (Gin 侧)
  → reference_blueprint.json (MVP 业务 schema)
```

`video_analysis_brief` 字段(节选)对应关系:

| `video_analysis_brief` | `reference_blueprint` |
|---|---|
| `source.url` | `source.url` |
| `source.duration_seconds` | `source.duration` |
| `content_analysis.summary` | `strategy.core_sales_logic` |
| `structure_analysis.scenes[].start_seconds` | `scenes[].start` |
| `structure_analysis.scenes[].end_seconds` | `scenes[].end` |
| `structure_analysis.scenes[].duration_seconds` | `scenes[].duration` |
| `structure_analysis.pacing_profile` | `scenes[].pacing` |

**Scene Type 归一化**:`video_analysis_brief` 不直接产出 `hook/pain_point/...`,需要 `BlueprintNormalizer` + LLM 二阶段分类(MVP 可以用简单规则 + GPT-4o-mini 一次性总结)。

---

# 11. target_blueprint.json

这是系统最重要的数据结构。

```json
{
  "project_id": "proj_001",
  "sku": "BAG-001",
  "scenario": "airport_travel",
  "variant": "default",
  "format": {
    "width": 1080,
    "height": 1920,
    "fps": 30
  },
  "scenes": [
    {
      "id": "scene_01",
      "order": 1,
      "type": "hook",
      "duration": 2.5,
      "headline": "Still packing like this?",
      "voiceover": "Still carrying too much when you travel?",
      "asset_id": "asset_003",
      "transition": "cut"
    }
  ]
}
```

transition 只支持:

```text
cut
fade
```

默认:

```text
cut
```

**渲染前必须经过 Render Adapter 转换为 `edit_decisions.json`**(详见 §25):

```text
target_blueprint.json
  → RenderAdapter.ToEditDecisions()
  → edit_decisions.json (OpenMontage 已有 schema)
  → execute_tool(tool_name="video_compose", inputs={operation:"render", edit_decisions:...})
```

---

# 12. OpenMontage MCP Client(v2 重写)

创建:

```text
backend/internal/mcp/openmontage/client.go
```

接口(v2):

```go
type OpenMontageClient interface {
    // 工具发现
    ListCapabilities(ctx context.Context) ([]Capability, error)

    // 健康检查
    Ping(ctx context.Context) error

    // 参考视频分析
    // 内部:execute_tool(tool_name="video_analyzer", inputs={source, analysis_depth})
    //      → 同步返回(MCP 顶层 execute_tool 是同步的,只有 create_remotion_video_share 异步)
    AnalyzeReference(
        ctx context.Context,
        req AnalyzeReferenceRequest,
    ) (*video_analysis_brief.VideoAnalysisBrief, error)

    // 素材上传(走 MCP session,需要在 HTTP Header 携带 Mcp-Session-Id)
    UploadAsset(
        ctx context.Context,
        projectID string,
        filePath string,
        opts UploadAssetOptions,
    ) (*AssetManifestEntry, error)

    // 渲染(异步)
    // 内部:create_remotion_video_share(... 或 ...)
    //      或 execute_tool(tool_name="video_compose", inputs={operation:"render", ...})
    // 返回:Gin 侧 job_id,MCP 侧 render_job_id 由实现内部维护
    Render(
        ctx context.Context,
        req RenderRequest,
    ) (*RenderJobHandle, error)

    // 渲染状态轮询
    GetRenderStatus(
        ctx context.Context,
        renderJobID string,
    ) (*RenderStatus, error)

    // 通用工具执行(预留,日常不用,供 Adapter 调试)
    ExecuteTool(
        ctx context.Context,
        toolName string,
        inputs map[string]any,
    ) (*ExecuteResult, error)
}

type AnalyzeReferenceRequest struct {
    Source        string // URL 或本地路径
    AnalysisDepth string // "transcript_only" | "standard" | "deep"
    OutputDir     string // 可选,默认由 MCP 决定
}

type RenderJobHandle struct {
    JobID         string // Gin 侧 job_id(主键)
    MCPJobID      string // MCP 侧 render_job_id(可能为空,取决于渲染路径)
    Status        string
}

type RenderStatus struct {
    Status     string  // queued | running | completed | failed
    Stage      string  // validation | render | upload | share
    VideoPath  string
    Error      string
    UpdatedAt  time.Time
}
```

**业务 Service 严禁出现**:

```text
tool_name
render_job_id
Mcp-Session-Id
JSON-RPC
```

等 MCP 协议细节。

---

# 13. MCP Transport(v2 重写)

创建:

```text
backend/internal/mcp/transport/
  http_jsonrpc.go   (streamable-http + JSON-RPC 2.0)
  session.go        (Mcp-Session-Id 管理)
  types.go          (JSON-RPC 请求/响应 struct)
```

### 13.1 HTTP JSON-RPC 客户端

```go
type Client struct {
    baseURL    string
    apiToken   string
    httpClient *http.Client
    sessionID  atomic.Value // string,首次 tools/list 后填充
}

func (c *Client) Call(ctx context.Context, method string, params any) (*JSONRPCResponse, error) {
    req := JSONRPCRequest{
        JSONRPC: "2.0",
        ID:      nextID(),
        Method:  method,
        Params:  params,
    }
    body, _ := json.Marshal(req)

    httpReq, _ := http.NewRequestWithContext(ctx, "POST", c.baseURL+"/mcp", bytes.NewReader(body))
    httpReq.Header.Set("Content-Type", "application/json")
    httpReq.Header.Set("Accept", "application/json")
    if c.apiToken != "" {
        httpReq.Header.Set("Authorization", "Bearer "+c.apiToken)
    }
    if sid := c.sessionID.Load(); sid != nil {
        httpReq.Header.Set("Mcp-Session-Id", sid.(string))
    }

    resp, err := c.httpClient.Do(httpReq)
    // ... 解析响应,提取 Mcp-Session-Id 缓存
}
```

### 13.2 Session 管理

- 首次请求(`tools/list`)完成后,从响应 Header `Mcp-Session-Id` 读取并缓存
- 后续 `upload_asset` 必须携带同一个 `Mcp-Session-Id`,否则会进入不同隔离目录
- MCP Server 重启后 `Mcp-Session-Id` 会失效,客户端应捕获 404/410 并重新初始化

### 13.3 错误处理

```go
type MCPError struct {
    Code    int    // JSON-RPC error code, -32600 ~ -32603
    Message string
    Data    any
}
```

常见错误:

```text
-32600 Invalid Request
-32601 Method not found
-32602 Invalid params
-32603 Internal error
-32001 ~ -32007 MCP 扩展错误
401     Unauthorized (token 缺失/错误)
404/410 Session expired (需重新初始化)
```

---

# 14. OpenMontage Adapter(v2 重写)

创建:

```text
backend/internal/services/reference_analyzer.go
```

接口:

```go
type ReferenceAnalyzer interface {
    Analyze(
        ctx context.Context,
        project *Project,
    ) (*ReferenceBlueprint, error)
}

type OpenMontageReferenceAnalyzer struct {
    mcpClient OpenMontageClient
    outputDir string
    normalizer BlueprintNormalizer
}

func (a *OpenMontageReferenceAnalyzer) Analyze(ctx, project) (*ReferenceBlueprint, error) {
    // 1. 调 MCP 分析
    brief, err := a.mcpClient.AnalyzeReference(ctx, AnalyzeReferenceRequest{
        Source:        project.ReferenceURL,
        AnalysisDepth: "standard",
        OutputDir:     filepath.Join(a.outputDir, project.ID, "analysis"),
    })
    if err != nil {
        return nil, fmt.Errorf("MCP analyze failed: %w", err)
    }

    // 2. 落盘 video_analysis_brief.json
    briefPath := filepath.Join(project.Dir, "video_analysis_brief.json")
    writeJSON(briefPath, brief)

    // 3. 归一化
    bp, err := a.normalizer.Normalize(brief, project)
    if err != nil {
        return nil, fmt.Errorf("normalize failed: %w", err)
    }

    // 4. 落盘 reference_blueprint.json
    bpPath := filepath.Join(project.Dir, "reference_blueprint.json")
    writeJSON(bpPath, bp)

    return bp, nil
}
```

执行:

```text
Gin Project
↓
OpenMontageMCPClient.AnalyzeReference
↓
execute_tool("video_analyzer", {source, depth})
↓
video_analysis_brief.json
↓
BlueprintNormalizer
↓
reference_blueprint.json
```

---

# 15. Blueprint Normalizer

创建:

```text
backend/internal/services/blueprint_normalizer.go
```

职责:

```text
video_analysis_brief (OpenMontage 原生 schema)
↓
字段映射 + Scene Type 归一化(LLM 或规则)
↓
ReferenceBlueprint (MVP 业务 schema)
```

字段映射对照表见 §10。

**Scene Type 归一化策略(MVP)**:

```text
规则优先 + LLM 兜底
1. 如果 brief.structure_analysis.scenes[].description 含 "question" / "stop" / "?" → hook
2. 如果含 "introduce" / "reveal" / "meet" → product_reveal
3. 如果含 "feature" / "spec" / "%" → feature_demo
4. 否则调 LLM 一次性给所有未分类 scene 打 tag
5. 兜底:feature_demo
```

不得把 `video_analysis_brief` 原始 JSON 直接暴露给前端。

---

# 16. Product Mapper

创建:

```text
backend/internal/services/product_mapper.go
```

通过 HTTP 调用 LLM。

接口:

```go
type ProductMapper interface {
    Map(
        ctx context.Context,
        reference *ReferenceBlueprint,
        product *Product,
        assets []Asset,
    ) (*TargetBlueprint, error)
}
```

LLM 输出必须:

```text
JSON only
```

LLM Prompt 核心约束(同 v1):

> 保持参考视频的营销结构、节奏和各 Scene 的销售作用,但不得照抄原文。将内容改写为给定箱包 SKU 的营销视频。优先使用真实产品卖点和用户提供的素材。

---

# 17. 商品真实性约束

所有商品描述必须来自:

```text
Product
Assets
```

禁止 AI 编造:

```text
Feature
Price
Offer
Material
Waterproof Level
Capacity
Certification
Discount
```

建议在 Product Mapper 输出后增加一道 **结构化校验**:

```go
func ValidateTargetBlueprint(bp *TargetBlueprint, product *Product) error {
    allowedFeatures := setFrom(product.Features)
    for _, scene := range bp.Scenes {
        // 检查 voiceover / headline 不出现 product.Features 之外的功能描述
        for _, claim := range extractClaims(scene.Headline, scene.Voiceover) {
            if !allowedFeatures.Contains(claim) && isMaterialClaim(claim) {
                return fmt.Errorf("scene %s invents undeclared feature: %s", scene.ID, claim)
            }
        }
    }
    return nil
}
```

---

# 18. Gin API

Base:

```text
/api/v1
```

## 18.1 创建项目

```http
POST /api/v1/projects
```

## 18.2 获取项目

```http
GET /api/v1/projects/:id
```

## 18.3 上传素材

```http
POST /api/v1/projects/:id/assets
```

multipart。

## 18.4 获取素材

```http
GET /api/v1/projects/:id/assets
```

## 18.5 分析参考视频

```http
POST /api/v1/projects/:id/analyze
```

返回:

```json
{
  "job_id": "job_001",
  "status": "queued"
}
```

注意:

> 视频分析属于长任务(MVP 通过 MCP `video_analyzer`,内部可能 30s-3min)。Gin 不应长期阻塞 HTTP request → 用 Gin goroutine + SQLite job state。

---

# 19. Job Model

```json
{
  "id": "job_001",
  "project_id": "proj_001",
  "type": "reference_analysis | render",
  "status": "queued | running | completed | failed",
  "mcp_render_job_id": "render_job_abc123",
  "external_ref": "video_analysis_brief.json | final.mp4",
  "error": null,
  "created_at": "...",
  "updated_at": "..."
}
```

**重要(v2 新增)**:

- `mcp_render_job_id`:MCP Server 返回的 `render_job_id`,**只在 MCP Server 进程生命周期内有效**。如果 MCP Server 重启,该字段失效,job 状态需重新查询或重新发起。
- 业务层使用 `id`(Gin 内部 job_id)作为对外 ID,不暴露 `mcp_render_job_id`。

---

# 20. Job 执行策略

MVP:

```text
Gin goroutine
+
SQLite job state
```

即可。

不要引入:

```text
Kafka
RabbitMQ
Redis Queue
Celery
```

未来批量生产再升级。

---

# 21. 获取 Job 状态

```http
GET /api/v1/jobs/:id
```

uni-app 轮询。

第一版无需 WebSocket。

---

# 22. 生成 Target Blueprint

```http
POST /api/v1/projects/:id/generate-blueprint
```

执行:

```text
reference_blueprint
+
product
+
assets
↓
ProductMapper (LLM)
↓
ValidateTargetBlueprint (反幻觉)
↓
target_blueprint.json
```

---

# 23. Scene API

### List

```http
GET /api/v1/projects/:id/scenes
```

### Update

```http
PATCH /api/v1/projects/:id/scenes/:sceneId
```

### Reorder

```http
POST /api/v1/projects/:id/scenes/reorder
```

Body:

```json
{
  "scene_ids": ["scene_03", "scene_01", "scene_02"]
}
```

### Delete

```http
DELETE /api/v1/projects/:id/scenes/:sceneId
```

### Rewrite

```http
POST /api/v1/projects/:id/scenes/:sceneId/rewrite
```

---

# 24. Render API(v2 重写)

```http
POST /api/v1/projects/:id/render
```

返回:

```json
{
  "job_id": "job_002"
}
```

后台执行流程:

```text
1. Gin 校验 target_blueprint.json 存在且 ≥1 scene
2. Gin 创建 job (status=queued, type=render)
3. Gin goroutine 启动:
   a. RenderAdapter.ToEditDecisions(target_blueprint) → edit_decisions.json
   b. RenderAdapter.ToAssetManifest(target_blueprint) → asset_manifest.json
   c. RenderAdapter.ToScenePlan(target_blueprint) → scene_plan.json
   d. OpenMontageMCPClient.Render(ctx, RenderRequest{
        Operation: "render",
        EditDecisions: ...,
        AssetManifest: ...,
        ScenePlan: ...,
        OutputPath: projects/proj_001/renders/final.mp4,
      })
   e. Render 返回 MCPJobID,落到 job.mcp_render_job_id
   f. 轮询 OpenMontageMCPClient.GetRenderStatus(mcp_job_id) 每 5 秒
   g. status=completed → job.status=completed, 写 final.mp4 路径
      status=failed   → job.status=failed, 写 error
```

---

# 25. Render Adapter(v2 重写)

创建:

```text
backend/internal/services/render_service.go
```

包含两种实现路径,**MVP 优先路径 A**:

### 路径 A(推荐):target_blueprint → edit_decisions → video_compose

```go
type Renderer interface {
    Render(
        ctx context.Context,
        project *Project,
        blueprintPath string,
        outputPath string,
    ) (*RenderJobHandle, error)
}

type OpenMontageRenderer struct {
    mcpClient OpenMontageClient
}

func (r *OpenMontageRenderer) Render(ctx, project, blueprintPath, outputPath) (*RenderJobHandle, error) {
    // 1. 读取 target_blueprint
    bp := loadTargetBlueprint(blueprintPath)

    // 2. 转换为 edit_decisions
    editDecisions := BlueprintToEditDecisions(bp)
    editDecisionsPath := filepath.Join(filepath.Dir(blueprintPath), "edit_decisions.json")
    saveJSON(editDecisionsPath, editDecisions)

    // 3. 构造 asset_manifest + scene_plan
    assetManifest := BlueprintToAssetManifest(bp, project.Assets)
    scenePlan := BlueprintToScenePlan(bp)

    // 4. 调 MCP 渲染(走内部 video_compose 工具)
    return r.mcpClient.Render(ctx, RenderRequest{
        Operation:     "render",
        EditDecisions: editDecisions,
        AssetManifest: assetManifest,
        ScenePlan:     scenePlan,
        OutputPath:    outputPath,
    })
}

func BlueprintToEditDecisions(bp *TargetBlueprint) *edit_decisions.EditDecisions {
    cuts := []edit_decisions.Cut{}
    for i, scene := range bp.Scenes {
        asset := lookupAsset(scene.AssetID)
        startSec := sumDurations(bp.Scenes[:i])
        endSec := startSec + scene.Duration
        cuts = append(cuts, edit_decisions.Cut{
            ID:         scene.ID,
            Source:     asset.Path,
            InSeconds:  startSec,
            OutSeconds: endSec,
            Layer:      "primary",
            Transform: edit_decisions.Transform{
                Animation: sceneTypeToAnimation(scene.Type),
            },
            TransitionIn:      scene.Transition,
            TransitionOut:     "cut",
            TransitionDuration: 0.25,
        })
    }
    return &edit_decisions.EditDecisions{
        Version:       "1.0",
        Cuts:          cuts,
        RenderRuntime: "remotion",
        Overlays:      blueprintToOverlays(bp), // headline / voiceover 字幕
    }
}
```

**优点**:

- 完全复用 OpenMontage 已有的 `video_compose` 渲染管线
- 不需要自建 Remotion 项目
- 不需要 `CUSTOM_COMPOSITION_ENABLED=true` 配置
- 字幕、音频、转场全部由 video_compose 处理

### 路径 B(备选):target_blueprint → TSX → create_remotion_video_share

```go
func (r *OpenMontageRenderer) RenderViaCustomComposition(ctx, project, bp) (*RenderJobHandle, error) {
    // 1. 把 target_blueprint 编译成 TSX 源码
    tsxCode := BlueprintToTSX(bp, project.Assets)

    // 2. 调 MCP custom-composition 模式
    return r.mcpClient.Render(ctx, RenderRequest{
        Mode:            "custom_composition",
        CustomCode:      tsxCode,
        AspectRatio:     "9:16",
        DurationPerImage: bp.Scenes[0].Duration, // 简化
    })
}
```

**前置条件**:

```text
.env: CUSTOM_COMPOSITION_ENABLED=true
```

**缺点**:

- 需要维护 TSX 模板生成器
- 中文 TTS、字幕叠加、复杂转场都得在 TSX 里手动实现
- 仅在路径 A 渲染质量不达标时启用

### MVP 选择

**默认路径 A**。如果 `video_compose` 渲染效果达不到营销级别,在 Phase 2 切到路径 B。

---

# 26. uni-app 页面

第一版只做:

```text
pages/index/index.vue
pages/project/review.vue
```

---

# 27. Page 1 — Create Project

```text
参考视频 URL
[________________________]

SKU
[________________________]

Product Name
[________________________]

Description
[________________________]

Features
[ + ]

Price
[________]

Offer
[________]

CTA
[________]

Target Market
[________________________]

Scenario
[ airport_travel ]

Assets
[ Upload ]

[ Analyze & Generate ]
```

执行:

```text
create project
→ upload assets (每个文件触发一次 upload_asset,带 Mcp-Session-Id)
→ analyze (POST /analyze, 返回 job_id)
→ poll job (GET /jobs/:id)
→ generate blueprint (POST /generate-blueprint)
→ review page
```

---

# 28. Page 2 — Scene Review

MVP 不做专业 Timeline。

用:

```text
Storyboard + Scene Cards
```

结构:

```text
┌────────────────────────────────┐
│ Project                        │
├────────────────────────────────┤
│ Scene Cards                    │
│                                │
│ [1] [2] [3] [4] [5]           │
├────────────────────────────────┤
│ Selected Scene                 │
│ Asset                          │
│ Headline                       │
│ Voiceover                      │
│ Duration                       │
├────────────────────────────────┤
│ Preview                        │
│                                │
│ [Generate Final Video]         │
└────────────────────────────────┘
```

---

# 29. Scene 排序

由于 uni-app 后续需要兼容小程序,第一版不要依赖只支持 H5 DOM 的复杂 drag library。

MVP 优先提供:

```text
↑
↓
```

调整顺序。

H5 可追加拖拽。

必须保证:

```text
Scene reorder
```

本质上通过 API 完成。

---

# 30. Asset Picker

使用 uni-app popup。

显示:

```text
图片 thumbnail
视频 thumbnail
label
description
```

点击:

```text
PATCH scene.asset_id
```

---

# 31. Preview

第一版前端 Preview:

```text
Image
→ timer
→ next scene

Video
→ play duration
→ next scene
```

Overlay:

```text
headline
```

即可。

不要求和最终 MP4 100% 一致。

---

# 32. Remotion / 渲染目录(v2 调整)

### 路径 A:不维护独立 renderer/

直接走 OpenMontage `video_compose` 的内部 Remotion 工程,无需 `bag-video-mvp-uniapp-gin-mcp/renderer/` 目录。

### 路径 B(如启用):保留 renderer/

```text
renderer/  (仅路径 B 使用)
  package.json
  src/
    Root.tsx
    ProductAd.tsx
    SceneRenderer.tsx
    scenes/
      HookScene.tsx
      ProductRevealScene.tsx
      FeatureScene.tsx
      LifestyleScene.tsx
      OfferScene.tsx
      CTAScene.tsx
```

---

# 33. OpenMontage MCP 调用流程(v2 重写)

### 33.1 参考分析流程

```text
1. Gin 启动时 OpenMontageHTTPMCPClient.Ping() 验证连通性
2. 加载/缓存 Mcp-Session-Id(可选,reference_analysis 不强制需要)
3. execute_tool(tool_name="video_analyzer",
                inputs={source: refURL, analysis_depth: "standard"})
   → 同步返回 video_analysis_brief(MCP 顶层 execute_tool 是同步的)
4. 落盘 video_analysis_brief.json
5. BlueprintNormalizer.Normalize() → reference_blueprint.json
```

### 33.2 渲染流程

```text
1. target_blueprint → edit_decisions (本地)
2. execute_tool(tool_name="video_compose",
                inputs={
                  operation: "render",
                  edit_decisions: ...,
                  asset_manifest: ...,
                  scene_plan: ...,
                  output_path: ...,
                })
   → MCP video_compose 内部启动后台渲染,返回 render_job_id
3. 轮询 get_render_status(render_job_id)
   → status=completed → final.mp4 落盘
```

### 33.3 注意事项

- MCP `execute_tool(video_analyzer)` 是同步的(几十秒到几分钟)
- MCP `execute_tool(video_compose, operation=render)` 是异步的,通过 `get_render_status` 轮询
- MCP `create_remotion_video_share` 也是异步,提供 `render_job_id` + `get_render_status` 轮询

---

# 34. MCP Server 生命周期(v2 重写)

**MVP 部署模式**:

```text
MCP Server 独立常驻进程
systemd 或 tmux 或 nohup
```

Gin **不** 启动/拉起 MCP Server。Gin 只连接,如果 MCP Server 不可达,Gin 应:

1. 启动期:日志告警 + 启动降级到 `MockOpenMontageClient`
2. 运行期:首次 MCP 调用失败 → 标记 MCP 不可用 → 后续请求走 Mock

```go
func NewClient(cfg Config) OpenMontageClient {
    real := NewHTTPMCPClient(cfg)
    if err := real.Ping(ctx); err != nil {
        log.Warn("MCP unreachable, falling back to Mock", "err", err)
        return NewMockClient(cfg.FixtureDir)
    }
    return real
}
```

---

# 35. MCP 配置(v2 重写)

`.env.example`:

```text
# OpenMontage MCP Server
OPENMONTAGE_MCP_URL=http://127.0.0.1:8900/mcp
OPENMONTAGE_MCP_TOKEN=                # 可选,生产部署必填
OPENMONTAGE_MCP_TIMEOUT_SECONDS=600

# LLM (Product Mapper)
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=gpt-4o-mini

# 渲染
CUSTOM_COMPOSITION_ENABLED=false       # 路径 A 不需要,路径 B 需要

# Mock 模式
MOCK_OPENMONTAGE=false
MOCK_FIXTURE_DIR=./fixtures

# 存储
DATA_DIR=./data
```

不要把 API Key / Token 写死。

### 35.1 MCP Server 启动

```bash
# 启动 MCP Server(独立进程)
cd /opt/video_web/OpenMontage
python mcp_server.py

# 或 stdio(仅本地 agent 用)
python mcp_server.py stdio
```

如果需要生成 token:

```bash
python mcp_server.py gen-token
# 输出: kCYnik7zip0QniCECr49ZhlCoXMzlfOY3hfH9QTYm-o
# 写入 MCP Server 所在机器的 .env
```

---

# 36. OpenMontage MCP 可替换性

必须设计:

```text
OpenMontageClient interface
```

未来可实现:

```text
OpenMontageHTTPMCPClient (默认)
OpenMontageZhMCPClient (未来官方)
MockOpenMontageClient (开发/验证)
```

测试时:

```text
MockOpenMontageClient
```

不依赖真实视频 API。

---

# 37. Mock 模式(v2 简化)

MVP 必须提供:

```text
MOCK_OPENMONTAGE=true
```

当开启:

```text
OpenMontageHTTPMCPClient.Ping() 失败
  或 MOCK_OPENMONTAGE=true
↓
MockOpenMontageClient 接管
↓
返回固定 reference_blueprint fixture
↓
target_blueprint fixture (可选)
```

用于验证:

```text
uni-app
→ Gin
→ Scene Review
→ Render
→ MP4
```

即使没有 OpenMontage,也能跑通。

**重要**:Mock 客户端**完全不走 MCP**,也不连 HTTP,纯本地返回 fixture,确保 Phase 1-3 零依赖。

---

# 38. 开发目录(v2 调整)

```text
bag-video-mvp-uniapp-gin-mcp/

frontend/
  pages/
    index/
      index.vue
    project/
      review.vue
  components/
    SceneCard.vue
    SceneEditor.vue
    AssetPicker.vue
    VideoPreview.vue
  stores/
    project.ts
  api/
    project.ts
    scene.ts
    job.ts
  manifest.json
  pages.json

backend/
  cmd/
    server/
      main.go
  internal/
    api/
      project_handler.go
      asset_handler.go
      scene_handler.go
      job_handler.go
      render_handler.go
    domain/
      project.go
      product.go
      asset.go
      scene.go
      blueprint.go
      job.go
      edit_decisions.go     # OpenMontage schema 的 Go struct
    service/
      project_service.go
      reference_analyzer.go
      blueprint_normalizer.go
      product_mapper.go
      scene_service.go
      scene_rewriter.go
      render_service.go     # RenderAdapter (target_blueprint → edit_decisions)
      blueprint_validator.go # 反幻觉校验
    mcp/
      transport/
        http_jsonrpc.go
        session.go
        types.go
      openmontage/
        client.go           # OpenMontageClient interface
        http_client.go      # OpenMontageHTTPMCPClient
        mock_client.go      # MockOpenMontageClient
        models.go
        adapter.go          # MCP result → 业务 model 归一化
    render/
      blueprint_to_edit_decisions.go  # BlueprintToEditDecisions
      blueprint_to_asset_manifest.go
      blueprint_to_scene_plan.go
    repository/
      sqlite/
  data/

fixtures/  (mock 用)
  video_analysis_brief.json
  reference_blueprint.json
  target_blueprint.json

.env.example
README.md
```

新增:

```text
internal/mcp/transport/         (HTTP JSON-RPC + Session 管理)
internal/mcp/openmontage/mock_client.go  (Phase 1-3 验证用)
internal/render/                (Blueprint → EditDecisions 转换)
internal/service/blueprint_validator.go (反幻觉校验)
internal/domain/edit_decisions.go (OpenMontage schema 对应)
```

---

# 39. 开发顺序(v2 调整)

严格按顺序。

## Phase 1 — Domain + Mock

实现:

```text
Project
Asset
Scene
Blueprint
Job
EditDecisions (Go struct)
```

加载 fixture:

```text
fixtures/reference_blueprint.json
fixtures/target_blueprint.json
```

## Phase 2 — uni-app Scene Review

完成:

```text
Scene list
up/down reorder
edit
replace asset
delete
save
preview
```

## Phase 3 — Render Adapter (本地转换)

完成:

```text
target_blueprint.json
  → BlueprintToEditDecisions()
  → edit_decisions.json (落盘)

target_blueprint.json
  → BlueprintToAssetManifest()
  → asset_manifest.json
```

不调 MCP,纯本地函数 + 单元测试。

**只有这一步跑通,才能进入下一阶段。**

## Phase 4 — MCP Transport + Client

拆成两个子阶段:

### Phase 4a — HTTP JSON-RPC Transport

```text
internal/mcp/transport/http_jsonrpc.go
- POST /mcp
- JSON-RPC 2.0 编码
- Bearer Token 注入
- Mcp-Session-Id 管理
```

单元测试:用 `httptest` mock MCP Server。

### Phase 4b — OpenMontageHTTPMCPClient

```text
internal/mcp/openmontage/http_client.go
- Ping
- ListCapabilities
- AnalyzeReference (包装 video_analyzer)
- UploadAsset
- Render (包装 video_compose)
- GetRenderStatus
```

集成测试:对接真实 MCP Server。

## Phase 5 — Reference Analysis

完成:

```text
URL
→ OpenMontage MCP (video_analyzer)
→ video_analysis_brief.json
→ BlueprintNormalizer
→ reference_blueprint.json
```

## Phase 6 — Product Mapper

完成:

```text
reference
+
product
+
assets
→
LLM
→
BlueprintValidator
→
target blueprint
```

## Phase 7 — Single Scene Rewrite

实现:

```text
scene
+
instruction
→
updated copy
```

## Phase 8 — 端到端 Render 集成

完成:

```text
target_blueprint
→ RenderAdapter (Phase 3)
→ execute_tool("video_compose", operation="render")
→ final.mp4
```

---

# 40. MVP 验收

准备:

```text
1 reference URL
1 SKU
5 images
2 product videos
```

完成:

### 1
创建项目。

### 2
OpenMontage MCP 正常执行 `video_analyzer` 任务,返回 `video_analysis_brief.json`。

### 3
Gin 获取并保存:

```text
video_analysis_brief.json
reference_blueprint.json
```

### 4
生成至少 5 Scene:

```text
Hook
Reveal
Feature
Feature
CTA
```

### 5
用户把 Scene 4 移到 Scene 2。

### 6
用户替换 Scene 3 素材。

### 7
修改 headline。

### 8
修改 duration。

### 9
Preview 正常。

### 10
调用 Render API,生成:

```text
edit_decisions.json (本地落盘)
final.mp4 (1080×1920)
```

### 11
最终 MP4:

```text
scene order
asset
headline
duration
```

与 target_blueprint 一致。

### 12(v2 新增)
目标 blueprint 经过 `BlueprintValidator` 反幻觉校验,所有 scene 文案不引入 product.features 之外的特性声明。

达到以上即 MVP 完成。

---

# 41. 未来 OpenClaw 批量化约束

当前 MVP 暂不实现 OpenClaw。

但以下能力必须全部暴露为 API:

```text
create_project
create_from_product
upload_asset
analyze_reference
generate_blueprint
list_scenes
update_scene
reorder_scene
replace_asset
rewrite_scene
render
get_job
```

未来 OpenClaw 不操作 Web。OpenClaw 直接:

```text
Excel
↓
parse rows
↓
Gin API
```

---

# 42. Excel 未来数据格式预留

未来建议:

```text
sku
product_name
description
features
price
offer
cta
target_market
scenario
variant
reference_url
asset_folder
```

每行:

```text
1 production task
```

或:

```text
1 SKU × scenario × variant
```

---

# 43. Multi-Agent 未来拆分

Phase 2 可以采用:

```text
Planner Agent
Reference Agent
Copy Agent
Asset Agent
QA Agent
Render Agent
```

但当前 Gin API 不应绑定 Agent 名称。Agent 只是 Client。

---

# 44. 失败恢复

必须做到:

OpenMontage MCP 失败:

```text
job = failed
project 保留
assets 保留
可 retry
```

LLM 失败:

```text
reference_blueprint 保留
可重新 generate
```

Render 失败:

```text
target_blueprint 保留
edit_decisions 保留(可复用)
可重新 render
```

单 Scene rewrite 失败:

```text
原 Scene 不修改
```

**Render 失败时不要清理 edit_decisions.json**(v2 新增),便于复用 + 调试。

---

# 45. 安全边界

MCP tool 的输入不得直接来自未经验证的前端 JSON。

Gin 必须验证:

```text
project ownership
file path
asset id
scene id
duration
allowed scene type
allowed transition
```

禁止用户输入任意:

```text
shell command
local file path
MCP tool name
MCP output_path
MCP render inputs (raw edit_decisions)
```

`MCP_API_TOKEN` 必须:

- 仅在 Gin 服务端环境变量持有,不暴露前端
- 通过 `.env` 加载,不进 git
- 定期轮换

---

# 46. 参考视频原则

参考视频只用于:

```text
marketing structure
pacing
scene purpose
hook pattern
sales logic
```

最终视频默认只使用:

```text
用户自己的商品素材
```

不要直接复制:

```text
竞品 Logo
竞品人物画面
竞品商品画面
原配音
原广告文案
```

除非用户明确拥有合法使用权。

---

# 47. README 要求

最终实现必须提供:

```text
README.md
```

至少说明:

```text
1. uni-app install
2. H5 run
3. Go/Gin run
4. SQLite init
5. OpenMontage MCP install (单独启动)
6. MCP configuration (URL + Token)
7. LLM config
8. mock mode (MOCK_OPENMONTAGE=true)
9. full pipeline test
10. Render path A vs path B 的选择
```

---

# 48. 给代码模型的最终指令

你正在实现一个:

> **跨境箱包参考视频重构 MVP**

不是完整视频编辑平台。

判断任何新增需求时先问:

> 它是否是完成以下链路所必需?

```text
Reference URL
→ OpenMontage MCP (streamable-http, video_analyzer)
→ Reference Blueprint
→ Product Mapping
→ Scene Review
→ Render Adapter (target_blueprint → edit_decisions)
→ video_compose (MCP)
→ MP4
```

如果不是:

```text
不要加入 MVP
```

优先保证:

```text
1. Gin 业务 API 稳定
2. Scene Schema 稳定
3. MCP Client 可替换(OpenMontage / Mock)
4. uni-app H5 可编辑
5. Render 可通过 video_compose 确定性输出
6. Job 可恢复(假设 MCP Server 常驻不重启)
7. AI 不编造商品事实(BlueprintValidator)
8. Web 与未来 OpenClaw 共用 API
```

最低可运行基线(v2):

```text
MockOpenMontageClient
→ target_blueprint (fixture)
→ RenderAdapter (本地 BlueprintToEditDecisions)
→ (跳过 MCP,直接落 edit_decisions.json)
→ uni-app Scene Review
→ 重新生成 target_blueprint
→ (同上) 渲染验证路径
```

只有这个 baseline 可运行之后,才接入真实 OpenMontage MCP。

---

# 附录 A — v1 → v2 关键决策对照表

| 决策点 | v1 | v2 | 理由 |
|---|---|---|---|
| MCP transport | stdio | streamable-http | 与 mcp_server.py 实际实现一致 |
| MCP 鉴权 | 无 | 可选 Bearer Token | 生产安全 |
| Client 接口 | CreateProject / RunTool / ListJobs | ListCapabilities / AnalyzeReference / Render / GetRenderStatus | 与真实 MCP 工具命名对齐 |
| Render 入口 | 自建 Remotion renderer/ 目录 | 走 OpenMontage video_compose | 复用既有管线,省 5+ 人天 |
| reference_blueprint 来源 | 新建独立 | 从 video_analysis_brief 归一化 | OpenMontage 已有 schema,避免重复 |
| target_blueprint 渲染 | 直接给 Remotion | target_blueprint → edit_decisions Adapter → video_compose | 复用既有 schema |
| Mock 客户端 | 走 MCP fixture | 完全不连 MCP,纯本地 | Phase 1-3 零依赖 |
| MCP Server 生命周期 | Gin 启动时拉起 | 独立常驻,Gin 只连 | 进程隔离,职责清晰 |
| Job 模型 | external_job_id 泛指 | mcp_render_job_id 单独字段 + 假设 MCP 不重启 | 现实约束 |

---

# 附录 B — 必读源码文件清单(代码模型动手前)

| 文件 | 行数 | 关注点 |
|---|---|---|
| `/opt/video_web/OpenMontage/mcp_server.py` | ~2700 | MCP 顶层工具、transport、render 流程 |
| `/opt/video_web/OpenMontage/MCP_SERVER.md` | ~860 | MCP 工具清单、调用示例 |
| `/opt/video_web/OpenMontage/tools/analysis/video_analyzer.py` | ~500 | 输入/输出 schema、video_analysis_brief 结构 |
| `/opt/video_web/OpenMontage/schemas/artifacts/video_analysis_brief.schema.json` | — | 参考 blueprint 归一化的源 schema |
| `/opt/video_web/OpenMontage/schemas/artifacts/edit_decisions.schema.json` | — | 渲染输入 schema |
| `/opt/video_web/OpenMontage/pipeline_defs/animated-explainer.yaml` | — | 既有 reference_input 流程参考 |
| `/opt/video_web/OpenMontage/tools/video/video_compose.py` | — | `operation=render` 实际行为 |

---

> 本 v2 文档完成日期:2026-08-28
> 适用代码大模型:Claude Code / Codex / Gemini CLI / OpenClaw
