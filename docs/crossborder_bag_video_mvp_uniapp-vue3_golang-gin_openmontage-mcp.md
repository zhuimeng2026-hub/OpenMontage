# 跨境箱包参考视频重构 MVP — uni-app Vue3 + Go/Gin + OpenMontage MCP 架构版

> 目标读者：Claude Code / Codex / Gemini CLI / OpenClaw / 其他代码大模型  
> 本文件用于替代上一版 `Vue3 + FastAPI + OpenMontage Adapter + Remotion` 方案。  
> 本版核心变化：  
> - 前端改为 **uni-app + Vue3**
> - 后端改为 **Go + Gin**
> - OpenMontage 不再通过 Python 业务代码直连，而通过 **MCP Server**
> - Remotion 继续作为最终视频渲染引擎
> - 所有人工操作均通过 API 暴露，为未来 **OpenClaw + Excel + Multi-Agent 批量生成**预留
>
> MVP 原则：**先跑通单条视频生产闭环，再扩展到智能体批量生产。**

---

# 1. 产品目标

用户提供：

1. 一个参考视频 URL
2. 自己的箱包产品基础资料
3. 自己的产品图片 / 视频素材

系统完成：

```text
参考视频 URL
→ OpenMontage MCP 分析
→ reference_blueprint.json
→ 商品映射
→ target_blueprint.json
→ Web Scene Review
→ Preview
→ Remotion Render
→ final.mp4
```

一句话定义：

> **粘贴一个优秀的箱包获客视频，上传自己的商品资料和素材，自动生成自己的营销视频，并允许逐场景调整。**

---

# 2. 未来演进目标

MVP 完成后，保持业务 API 不变，新增：

```text
Excel
→ OpenClaw
→ 多 Agent
→ Gin API / MCP
→ 批量 Blueprint
→ 批量 Remotion Render
```

未来核心生产单位：

```text
SKU × Scenario × Variant
```

例如：

```text
20 SKU
× 5 场景
× 3 Hook 版本
= 300 条视频
```

因此本版所有接口必须考虑：

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

通过：

```text
OpenMontage MCP
```

实现：

- 创建 OpenMontage project
- 视频分析
- transcript 获取
- scene / keyframe / pacing 分析
- hook 分析
- 产品展示结构分析
- CTA 分析
- 获取项目状态
- 获取生成产物

### 标准化

必须生成：

```text
reference_blueprint.json
```

### 商品映射

生成：

```text
target_blueprint.json
```

### Web Scene Review

必须支持：

- Scene Cards
- 顺序调整
- 替换素材
- 编辑 headline
- 编辑 voiceover
- 编辑 duration
- 删除 Scene
- 单 Scene AI rewrite
- 保存

### Preview

MVP 使用前端轻量 Preview。

### Render

通过：

```text
Remotion
```

输出：

```text
1080 × 1920
9:16
30fps
H.264 MP4
```

---

# 3.2 明确不做

禁止扩展为完整视频平台。

MVP 不做：

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

后两项属于 Phase 2，不属于当前 MVP。

---

# 4. 技术栈

## 4.1 Frontend

```text
uni-app
Vue 3
Pinia
```

MVP 首要验收端：

```text
H5
```

不要同时把：

```text
H5
微信小程序
App
```

全部作为第一版验收目标。

未来兼容小程序即可。

---

# 4.2 Backend

```text
Go 1.23+
Gin
```

Gin 负责：

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

---

# 4.3 OpenMontage

本项目不 import OpenMontage Python package。

只通过：

```text
OpenMontage MCP Server
```

调用。

必须创建：

```text
OpenMontageMCPClient
```

作为唯一集成入口。

---

# 4.4 MCP

当前按 stdio MCP Server 设计。

典型能力：

```text
list_capabilities
create_project
get_project_status
run_tool
render_video
get_job_status
list_jobs
```

注意：

> MCP 接口必须被封装在 Adapter 内，不允许业务 Handler 直接调用 MCP tool 名称。

原因：

未来：

```text
openmontage-zh-mcp
→ 官方 OpenMontage MCP
```

可能发生变化。

业务层不应感知。

---

# 4.5 Rendering

```text
Remotion
Node.js
```

原则：

```text
target_blueprint.json
→ Remotion
→ MP4
```

Remotion 不承担 AI 判断。

---

# 4.6 Storage

MVP：

```text
本地文件系统
+
SQLite
```

SQLite 保存：

```text
projects
assets
jobs
```

Blueprint 仍保存 JSON 文件。

目录：

```text
data/
  projects/
    {project_id}/
      project.json
      reference_blueprint.json
      target_blueprint.json
      assets/
      previews/
      renders/
```

后续可以替换为：

```text
PostgreSQL + S3 / OSS
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
                       └─────────┬──────────┘
                                 │
             ┌───────────────────┼────────────────────┐
             ↓                   ↓                    ↓
      OpenMontage MCP       LLM HTTP API         Remotion CLI
             │                   │                    │
             ↓                   ↓                    ↓
    reference analysis    product mapping       final.mp4
```

---

# 6. Phase 2 架构预留

未来：

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
OpenMontage MCP      Scene Service        Remotion
```

重要：

> Web 和 OpenClaw 都只是 Gin API 的 Client。

禁止形成：

```text
Web 一套业务逻辑
OpenClaw 另一套业务逻辑
```

---

# 7. 核心状态原则

## 7.1 Gin 拥有项目状态

项目状态必须保存在：

```text
Gin
SQLite
JSON files
```

不保存在：

```text
OpenMontage
OpenClaw
LLM Context
Remotion
```

---

# 7.2 OpenMontage 是能力提供者

OpenMontage 负责：

```text
Reference Video
→ Analysis
```

不负责：

```text
业务 SKU 状态
Scene 最终顺序
用户修改状态
价格真实性
Offer
CTA
```

---

# 7.3 MCP 是 transport，不是业务模型

MCP 工具调用结果必须经过：

```text
OpenMontage MCP
→ Adapter
→ Normalizer
→ ReferenceBlueprint
```

业务层不直接使用 MCP 原始返回值。

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

必须预留：

```text
scenario
variant
sku
```

为以后批量生产使用。

---

# 8.2 Asset

```json
{
  "id": "asset_001",
  "project_id": "proj_001",
  "type": "image",
  "path": "/data/projects/proj_001/assets/bag-front.jpg",
  "label": "front",
  "description": "Black travel backpack front view"
}
```

支持：

```text
image
video
```

---

# 9. Scene Type

第一版只允许：

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

fallback：

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
    "duration": 38.4
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

transition 只支持：

```text
cut
fade
```

默认：

```text
cut
```

---

# 12. OpenMontage MCP Client

创建：

```text
backend/internal/mcp/openmontage/client.go
```

接口：

```go
type OpenMontageClient interface {
    ListCapabilities(ctx context.Context) ([]Capability, error)

    CreateProject(
        ctx context.Context,
        req CreateOpenMontageProjectRequest,
    ) (*OpenMontageProject, error)

    GetProjectStatus(
        ctx context.Context,
        projectID string,
    ) (*OpenMontageProjectStatus, error)

    RunTool(
        ctx context.Context,
        req RunToolRequest,
    ) (*MCPJob, error)

    GetJobStatus(
        ctx context.Context,
        jobID string,
    ) (*MCPJobStatus, error)

    RenderVideo(
        ctx context.Context,
        projectID string,
    ) (*MCPJob, error)
}
```

注意：

业务 Service 不得出现：

```text
run_tool
get_job_status
```

这样的 MCP 协议细节。

---

# 13. MCP Transport

创建：

```text
backend/internal/mcp/transport/
```

第一版支持：

```text
stdio
```

结构：

```text
mcp/
  transport/
    stdio.go
  openmontage/
    client.go
    models.go
    adapter.go
```

未来如切：

```text
HTTP
SSE
remote MCP
```

只替换 transport。

---

# 14. OpenMontage Adapter

创建：

```text
backend/internal/services/reference_analyzer.go
```

接口：

```go
type ReferenceAnalyzer interface {
    Analyze(
        ctx context.Context,
        project *Project,
    ) (*ReferenceBlueprint, error)
}
```

实现：

```text
OpenMontageReferenceAnalyzer
```

执行：

```text
Gin Project
↓
OpenMontageMCPClient.CreateProject
↓
Run OpenMontage analysis capability
↓
poll GetJobStatus
↓
读取结果
↓
BlueprintNormalizer
↓
reference_blueprint.json
```

---

# 15. Blueprint Normalizer

创建：

```text
backend/internal/services/blueprint_normalizer.go
```

职责：

```text
OpenMontage 原始分析结果
↓
统一 Scene Schema
↓
ReferenceBlueprint
```

不得把 OpenMontage 原始 JSON 直接暴露给前端。

---

# 16. Product Mapper

创建：

```text
backend/internal/services/product_mapper.go
```

通过 HTTP 调用 LLM。

接口：

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

LLM 输出必须：

```text
JSON only
```

---

# 17. 商品真实性约束

所有商品描述必须来自：

```text
Product
Assets
```

禁止 AI 编造：

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

例如：

没有输入：

```text
waterproof
```

禁止输出：

```text
100% waterproof
```

---

# 18. Gin API

Base：

```text
/api/v1
```

---

## 18.1 创建项目

```http
POST /api/v1/projects
```

---

## 18.2 获取项目

```http
GET /api/v1/projects/:id
```

---

## 18.3 上传素材

```http
POST /api/v1/projects/:id/assets
```

multipart。

---

## 18.4 获取素材

```http
GET /api/v1/projects/:id/assets
```

---

## 18.5 分析参考视频

```http
POST /api/v1/projects/:id/analyze
```

返回：

```json
{
  "job_id": "job_001",
  "status": "queued"
}
```

注意：

视频分析属于长任务。

Gin 不应长期阻塞 HTTP request。

---

# 19. Job Model

即使 MVP 不上 Redis / Celery，也必须存在 Job 概念。

```json
{
  "id": "job_001",
  "project_id": "proj_001",
  "type": "reference_analysis",
  "status": "queued",
  "external_job_id": "openmontage_job_xxx",
  "error": null
}
```

status：

```text
queued
running
completed
failed
```

---

# 20. Job 执行策略

MVP：

```text
Gin goroutine
+
SQLite job state
```

即可。

不要引入：

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

执行：

```text
reference_blueprint
+
product
+
assets
↓
ProductMapper
↓
target_blueprint
```

---

# 23. Scene API

## List

```http
GET /api/v1/projects/:id/scenes
```

## Update

```http
PATCH /api/v1/projects/:id/scenes/:sceneId
```

## Reorder

```http
POST /api/v1/projects/:id/scenes/reorder
```

Body：

```json
{
  "scene_ids": [
    "scene_03",
    "scene_01",
    "scene_02"
  ]
}
```

## Delete

```http
DELETE /api/v1/projects/:id/scenes/:sceneId
```

## Rewrite

```http
POST /api/v1/projects/:id/scenes/:sceneId/rewrite
```

---

# 24. Render API

```http
POST /api/v1/projects/:id/render
```

返回：

```json
{
  "job_id": "render_job_001"
}
```

后台：

```text
target_blueprint.json
↓
RemotionAdapter
↓
Node CLI
↓
final.mp4
```

---

# 25. Remotion Adapter

创建：

```text
backend/internal/render/remotion.go
```

接口：

```go
type Renderer interface {
    Render(
        ctx context.Context,
        blueprintPath string,
        outputPath string,
    ) error
}
```

MVP 可以：

```text
exec.Command()
```

调用：

```text
node / renderer CLI
```

不要在 Go 里重写 Remotion。

---

# 26. uni-app 页面

第一版只做：

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

执行：

```text
create project
→ upload assets
→ analyze
→ poll job
→ generate blueprint
→ review page
```

---

# 28. Page 2 — Scene Review

MVP 不做专业 Timeline。

用：

```text
Storyboard + Scene Cards
```

结构：

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

由于 uni-app 后续需要兼容小程序，第一版不要依赖只支持 H5 DOM 的复杂 drag library。

MVP 优先提供：

```text
↑
↓
```

调整顺序。

H5 可追加拖拽。

必须保证：

```text
Scene reorder
```

本质上通过 API 完成。

---

# 30. Asset Picker

使用 uni-app popup。

显示：

```text
图片 thumbnail
视频 thumbnail
label
description
```

点击：

```text
PATCH scene.asset_id
```

---

# 31. Preview

第一版前端 Preview：

```text
Image
→ timer
→ next scene

Video
→ play duration
→ next scene
```

Overlay：

```text
headline
```

即可。

不要求和 Remotion 100% 一致。

---

# 32. Remotion 目录

```text
renderer/
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

# 33. OpenMontage MCP 调用流程

参考流程：

```text
1. MCP startup
2. list_capabilities
3. create_project
4. run_tool / pipeline
5. get_job_status
6. collect artifacts
7. normalize
```

OpenMontage 长任务必须：

```text
job_id
→ polling
```

不要假设 MCP tool 同步返回最终视频。

---

# 34. MCP Server 生命周期

MVP 推荐：

```text
Gin 启动时
→ 启动 / 连接 OpenMontage MCP Server
```

不要：

```text
每次 analyze
→ 新启动 Python 环境
```

建议 MCP client 作为 singleton service。

---

# 35. MCP 配置

`.env.example`：

```text
OPENMONTAGE_MCP_COMMAND=python
OPENMONTAGE_MCP_ARGS=-m openmontage_mcp.server --project-dir /path/to/openmontage
OPENMONTAGE_PROJECT_DIR=/path/to/openmontage
LLM_BASE_URL=
LLM_API_KEY=
LLM_MODEL=
REMOTION_DIR=./renderer
DATA_DIR=./data
```

不要把 API Key 写死。

---

# 36. OpenMontage MCP 可替换性

必须设计：

```text
OpenMontageClient interface
```

未来可实现：

```text
OpenMontageZhMCPClient
OfficialOpenMontageMCPClient
MockOpenMontageClient
```

测试时：

```text
MockOpenMontageClient
```

不依赖真实视频 API。

---

# 37. Mock 模式

MVP 必须提供：

```text
MOCK_OPENMONTAGE=true
```

当开启：

```text
reference URL
↓
固定 reference_blueprint fixture
```

用于验证：

```text
uni-app
→ Gin
→ Scene Review
→ Remotion
```

即使没有 OpenMontage，也能跑通。

---

# 38. 开发目录

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
    service/
      project_service.go
      reference_analyzer.go
      blueprint_normalizer.go
      product_mapper.go
      scene_service.go
      scene_rewriter.go
      render_service.go
    mcp/
      transport/
        stdio.go
      openmontage/
        client.go
        adapter.go
        models.go
    render/
      remotion.go
    repository/
      sqlite/
  data/

renderer/
  src/
    Root.tsx
    ProductAd.tsx
    SceneRenderer.tsx
    scenes/
  package.json

fixtures/
  reference_blueprint.json
  target_blueprint.json

.env.example
README.md
```

---

# 39. 开发顺序

严格按顺序。

## Phase 1 — Domain + Mock

实现：

```text
Project
Asset
Scene
Blueprint
Job
```

加载 fixture。

---

## Phase 2 — uni-app Scene Review

完成：

```text
Scene list
up/down reorder
edit
replace asset
delete
save
preview
```

---

## Phase 3 — Remotion

完成：

```text
target_blueprint
→ final.mp4
```

只有这一步跑通，才能进入下一阶段。

---

## Phase 4 — MCP Client

完成：

```text
stdio MCP transport
OpenMontageClient
list capabilities
create project
job polling
```

---

## Phase 5 — Reference Analysis

完成：

```text
URL
→ OpenMontage MCP
→ reference_blueprint
```

---

## Phase 6 — Product Mapper

完成：

```text
reference
+
product
+
assets
→
target blueprint
```

---

## Phase 7 — Single Scene Rewrite

实现：

```text
scene
+
instruction
→
updated copy
```

---

# 40. MVP 验收

准备：

```text
1 reference URL
1 SKU
5 images
2 product videos
```

完成：

### 1
创建项目。

### 2
OpenMontage MCP 正常执行分析任务。

### 3
Gin 获取并保存：

```text
reference_blueprint.json
```

### 4
生成至少 5 Scene：

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
Remotion 输出：

```text
1080 × 1920 MP4
```

### 11
最终 MP4：

```text
scene order
asset
headline
duration
```

与 target_blueprint 一致。

达到以上即 MVP 完成。

---

# 41. 未来 OpenClaw 批量化约束

当前 MVP 暂不实现 OpenClaw。

但以下能力必须全部暴露为 API：

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

未来 OpenClaw 不操作 Web。

OpenClaw 直接：

```text
Excel
↓
parse rows
↓
Gin API
```

---

# 42. Excel 未来数据格式预留

未来建议：

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

每行：

```text
1 production task
```

或：

```text
1 SKU × scenario × variant
```

---

# 43. Multi-Agent 未来拆分

Phase 2 可以采用：

```text
Planner Agent
Reference Agent
Copy Agent
Asset Agent
QA Agent
Render Agent
```

但当前 Gin API 不应绑定 Agent 名称。

Agent 只是 Client。

---

# 44. 失败恢复

必须做到：

OpenMontage MCP 失败：

```text
job = failed
project 保留
assets 保留
可 retry
```

LLM 失败：

```text
reference_blueprint 保留
可重新 generate
```

Remotion 失败：

```text
target_blueprint 保留
可重新 render
```

单 Scene rewrite 失败：

```text
原 Scene 不修改
```

---

# 45. 安全边界

MCP tool 的输入不得直接来自未经验证的前端 JSON。

Gin 必须验证：

```text
project ownership
file path
asset id
scene id
duration
allowed scene type
allowed transition
```

禁止用户输入任意：

```text
shell command
local file path
MCP tool name
OpenMontage output_path
```

---

# 46. 参考视频原则

参考视频只用于：

```text
marketing structure
pacing
scene purpose
hook pattern
sales logic
```

最终视频默认只使用：

```text
用户自己的商品素材
```

不要直接复制：

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

最终实现必须提供：

```text
README.md
```

至少说明：

```text
1. uni-app install
2. H5 run
3. Go/Gin run
4. SQLite init
5. OpenMontage MCP install
6. MCP configuration
7. Remotion install
8. LLM config
9. mock mode
10. full pipeline test
```

---

# 48. 给代码模型的最终指令

你正在实现一个：

> **跨境箱包参考视频重构 MVP**

不是完整视频编辑平台。

判断任何新增需求时先问：

> 它是否是完成以下链路所必需？

```text
Reference URL
→ OpenMontage MCP
→ Reference Blueprint
→ Product Mapping
→ Scene Review
→ Remotion MP4
```

如果不是：

```text
不要加入 MVP
```

优先保证：

```text
1. Gin 业务 API 稳定
2. Scene Schema 稳定
3. MCP Client 可替换
4. uni-app H5 可编辑
5. Remotion 可确定性输出
6. Job 可恢复
7. AI 不编造商品事实
8. Web 与未来 OpenClaw 共用 API
```

最低可运行基线：

```text
Mock OpenMontage
→ target_blueprint
→ uni-app Scene Review
→ Remotion
→ MP4
```

只有这个 baseline 可运行之后，才接入真实 OpenMontage MCP。
