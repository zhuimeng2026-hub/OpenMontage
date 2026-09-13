# 跨境箱包参考视频重构 MVP — 实现说明

> 目标读者：Claude Code / Codex / 其他代码大模型  
> 项目目标：基于一个已知参考视频 URL，分析其营销结构，并结合用户自己的箱包产品素材，生成一条可调整、可预览、可最终导出的短视频广告。  
> MVP 原则：**不做完整视频编辑器，不做逐帧替换，不做换脸/换物，不做复杂时间线。**

---

## 1. 产品目标

用户提供：

1. 一个参考视频 URL（优先 YouTube，后续可扩展 TikTok / Instagram）
2. 自己的箱包产品信息
3. 自己的商品图片 / 视频素材

系统完成：

1. 分析参考视频的营销结构
2. 生成标准化 `reference_blueprint.json`
3. 将参考结构映射到用户自己的箱包产品
4. 生成 `target_blueprint.json`
5. 在 Web Scene Review 页面允许用户：
   - 调整场景顺序
   - 更换场景素材
   - 修改文案
   - 修改显示时长
   - 删除场景
   - 重新生成单个场景文案
6. 快速预览
7. 使用 Remotion 输出最终 9:16 MP4

一句话产品定义：

> **粘贴一个优秀的箱包营销视频链接，上传自己的产品素材，自动生成自己的广告版本，并允许逐场景调整。**

---

# 2. MVP 范围

## 2.1 必须实现

### 输入
- Reference Video URL
- Product Name
- Product Description
- Product Features
- Price
- Offer
- CTA
- Target Market
- Product Images
- Product Videos

### 视频分析
通过 OpenMontage 或其内部能力完成：
- 视频获取
- transcript
- scene / keyframe / pacing 分析
- hook 分析
- 产品展示结构分析
- CTA 分析

### 标准化
必须将分析结果转换成统一的：

```text
reference_blueprint.json
```

### 产品映射
使用 LLM 将参考视频结构映射到用户 SKU，生成：

```text
target_blueprint.json
```

### Web Review
必须支持：
- Scene Cards
- 拖拽排序
- 替换素材
- 修改 headline
- 修改 voiceover
- 修改 duration
- 删除 Scene
- 单 Scene 重生成
- 保存

### Preview
MVP 使用简单顺序播放即可。

不要求每次编辑都执行 Remotion render。

### Render
最终点击：

```text
Generate Final Video
```

调用 Remotion 输出 MP4。

---

## 2.2 明确不做

MVP 禁止扩展以下功能：

- 完整 NLE timeline
- 多轨编辑
- AI 换脸
- AI 替换人物手中的包
- 视频逐帧 inpainting
- 自动口型同步
- 复杂音频混音器
- 多人协作
- 用户权限系统
- 支付
- SaaS 多租户
- 大规模任务队列
- A/B 测试平台
- 自动发布 TikTok
- 自动广告投放
- 长视频
- 复杂转场编辑器

只做：

```text
URL
→ Analyze
→ Blueprint
→ Product Mapping
→ Scene Review
→ Preview
→ Render
```

---

# 3. 推荐技术栈

## Frontend

优先：

```text
Vue 3
Vite
Pinia
Vue Router
SortableJS
```

可选：

```text
React + dnd-kit
```

不要同时支持两套。

---

## Backend

优先：

```text
FastAPI
Python 3.11+
```

原因：
- OpenMontage / 视频处理侧 Python 集成方便
- 后续调用 FFmpeg / LLM / OpenMontage 简单

---

## Video Intelligence

第一版：

```text
OpenMontage
```

OpenMontage 被视为外部视频理解 / 生产能力。

必须通过 Adapter 隔离：

```text
OpenMontageAdapter
```

禁止业务层直接依赖其内部输出格式。

---

## Rendering

```text
Remotion
Node.js
```

Remotion 只读取标准化 `target_blueprint.json`。

---

## Storage

MVP：

```text
./data/projects/{project_id}/
```

文件系统即可。

结构：

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

数据库第一版可不做。

如必须持久化索引：

```text
SQLite
```

---

# 4. 总体架构

```text
                         ┌────────────────────┐
                         │     Web Client     │
                         │                    │
                         │ Project Form       │
                         │ Scene Review       │
                         │ Asset Picker       │
                         │ Preview            │
                         └─────────┬──────────┘
                                   │
                                   │ REST
                                   ↓
                         ┌────────────────────┐
                         │     FastAPI API    │
                         └─────────┬──────────┘
                                   │
              ┌────────────────────┼────────────────────┐
              ↓                    ↓                    ↓
     OpenMontageAdapter      Product Mapper       Remotion Adapter
              │                    │                    │
              ↓                    ↓                    ↓
 reference_blueprint.json   target_blueprint.json   final.mp4
```

---

# 5. 核心原则

## 5.1 项目状态不属于 OpenMontage

系统状态必须由本项目保存。

OpenMontage 只负责：

```text
reference video
→ analysis
```

其结果必须转换成：

```text
reference_blueprint.json
```

---

## 5.2 Remotion 不做智能判断

Remotion 不需要知道：

- 什么是爆款
- 什么是 Hook
- 用户是谁
- 产品卖点是什么

Remotion 只负责：

```text
JSON
→ video
```

---

## 5.3 Scene JSON 是系统核心协议

Frontend / Backend / LLM / Remotion 都围绕同一份 Scene Schema 工作。

不要让各模块自行定义不同格式。

---

# 6. Scene 类型

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

如分析结果无法准确归类：

```text
feature_demo
```

优先作为 fallback。

不要增加更多类型，除非真实需求出现。

---

# 7. 数据模型

## 7.1 project.json

```json
{
  "id": "proj_001",
  "status": "draft",
  "reference_url": "https://youtube.com/...",
  "created_at": "2026-08-28T10:00:00Z",
  "product": {
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

---

## 7.2 Asset

```json
{
  "id": "asset_001",
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

# 8. reference_blueprint.json

示例：

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
      "start": 0.0,
      "end": 2.8,
      "duration": 2.8,
      "type": "hook",
      "sales_role": "pain hook",
      "visual_description": "woman struggling with multiple bags",
      "original_text": "Stop traveling like this",
      "pacing": "fast"
    },
    {
      "id": "ref_scene_02",
      "order": 2,
      "start": 2.8,
      "end": 6.0,
      "duration": 3.2,
      "type": "product_reveal",
      "sales_role": "introduce solution",
      "visual_description": "travel backpack hero shot",
      "original_text": "Everything fits in one bag",
      "pacing": "medium"
    }
  ]
}
```

---

# 9. target_blueprint.json

这是 Web 和 Remotion 的主要输入。

```json
{
  "project_id": "proj_001",
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
    },
    {
      "id": "scene_02",
      "order": 2,
      "type": "product_reveal",
      "duration": 3.5,
      "headline": "One bag. Everything you need.",
      "voiceover": "Meet the 40-liter travel backpack.",
      "asset_id": "asset_006",
      "transition": "cut"
    },
    {
      "id": "scene_03",
      "order": 3,
      "type": "feature_demo",
      "duration": 4.0,
      "headline": "40L Capacity",
      "voiceover": "Pack clothes, shoes and your laptop in one bag.",
      "asset_id": "asset_010",
      "transition": "cut"
    }
  ]
}
```

MVP `transition` 只支持：

```text
cut
fade
```

默认：

```text
cut
```

---

# 10. OpenMontage Adapter

必须创建：

```text
backend/services/openmontage_adapter.py
```

统一接口：

```python
class OpenMontageAdapter:
    async def analyze_reference(
        self,
        video_url: str
    ) -> ReferenceBlueprint:
        ...
```

业务代码不能直接解析 OpenMontage 内部文件。

Adapter 的职责：

```text
OpenMontage 原始输出
↓
Normalization
↓
ReferenceBlueprint
```

如果 OpenMontage CLI / Agent API 未来变化，只修改 Adapter。

---

# 11. Product Mapper

创建：

```text
backend/services/product_mapper.py
```

接口：

```python
class ProductMapper:
    async def create_target_blueprint(
        self,
        reference_blueprint,
        product,
        assets
    ) -> TargetBlueprint:
        ...
```

LLM Prompt 目标：

> 保持参考视频的营销结构、节奏和各 Scene 的销售作用，但不得照抄原文。将内容改写为给定箱包 SKU 的营销视频。优先使用真实产品卖点和用户提供的素材。

必须输出 JSON。

禁止自由文本。

---

# 12. 素材匹配逻辑

第一版不需要视觉向量数据库。

采用简单规则 + LLM：

```text
asset filename
asset label
asset description
scene type
feature
```

例如：

```text
Scene:
feature_demo
feature = shoe compartment

Assets:
bag-front.jpg
bag-shoe-compartment.mp4
bag-laptop.jpg
```

优先：

```text
bag-shoe-compartment.mp4
```

---

# 13. API

## 13.1 创建项目

```http
POST /api/projects
```

Body：

```json
{
  "reference_url": "...",
  "product": {}
}
```

Response：

```json
{
  "id": "proj_001"
}
```

---

## 13.2 上传素材

```http
POST /api/projects/{project_id}/assets
```

multipart/form-data

---

## 13.3 分析参考视频

```http
POST /api/projects/{project_id}/analyze
```

执行：

```text
OpenMontage
→ normalize
→ reference_blueprint.json
```

---

## 13.4 生成目标视频蓝图

```http
POST /api/projects/{project_id}/generate-blueprint
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
target_blueprint.json
```

---

## 13.5 获取 Scene

```http
GET /api/projects/{project_id}/scenes
```

---

## 13.6 更新 Scene

```http
PATCH /api/projects/{project_id}/scenes/{scene_id}
```

Body：

```json
{
  "headline": "New headline",
  "duration": 3.5,
  "asset_id": "asset_004"
}
```

---

## 13.7 Scene 排序

```http
POST /api/projects/{project_id}/scenes/reorder
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

---

## 13.8 删除 Scene

```http
DELETE /api/projects/{project_id}/scenes/{scene_id}
```

---

## 13.9 单 Scene 重写

```http
POST /api/projects/{project_id}/scenes/{scene_id}/rewrite
```

Body：

```json
{
  "instruction": "Make the hook stronger for US women travelers"
}
```

---

## 13.10 Render

```http
POST /api/projects/{project_id}/render
```

返回：

```json
{
  "status": "completed",
  "video_url": "/media/projects/proj_001/renders/final.mp4"
}
```

MVP 可以同步执行。

无需 Celery。

---

# 14. Web 页面

只做两个页面。

---

# Page 1 — Create Project

路由：

```text
/
```

界面：

```text
Reference Video URL
[____________________________]

Product Name
[____________________________]

Description
[____________________________]

Features
[ + Add Feature ]

Price
[________]

Offer
[________]

CTA
[________]

Target Market
[____________________________]

Product Assets
[ Upload Images / Videos ]

[ Analyze & Generate ]
```

按钮执行：

```text
create project
→ upload assets
→ analyze
→ generate blueprint
→ redirect /project/{id}
```

---

# Page 2 — Scene Review

路由：

```text
/project/:id
```

布局：

```text
┌─────────────────────────────────────────┐
│ Product / Project Info                  │
├─────────────────────────────────────────┤
│                                         │
│ Scene Cards                             │
│                                         │
│ [Scene 1] [Scene 2] [Scene 3] ...      │
│                                         │
├─────────────────────────────────────────┤
│ Selected Scene Editor                   │
├─────────────────────────────────────────┤
│ Preview                                 │
│                                         │
│                [Render Final Video]     │
└─────────────────────────────────────────┘
```

---

# 15. Scene Card

每张卡片：

```text
┌───────────────────────┐
│ Scene 1 — Hook        │
│                       │
│ [asset thumbnail]     │
│                       │
│ Still packing like    │
│ this?                 │
│                       │
│ 2.5 sec               │
│                       │
│ [Replace] [Delete]    │
└───────────────────────┘
```

必须支持拖拽。

拖拽完成：

```text
POST /scenes/reorder
```

---

# 16. Scene Editor

选择 Scene 后显示：

```text
Type
Hook

Asset
[ thumbnail ]
[ Replace Asset ]

Headline
[ Still packing like this? ]

Voiceover
[ Still carrying too much when you travel? ]

Duration
[ 2.5 ]

[ Rewrite with AI ]

Instruction:
[ Make this hook more direct ]

[ Save ]
```

---

# 17. Asset Picker

弹窗：

```text
Choose Product Asset

[bag-front.jpg]
[bag-side.jpg]
[capacity.mp4]
[shoe-compartment.mp4]
[laptop.jpg]
```

点击即替换：

```text
scene.asset_id = selected asset
```

---

# 18. Preview

第一版无需 Remotion Player。

实现简单 Preview：

依次播放：

```text
Scene 1
→ Scene 2
→ Scene 3
```

图片 Scene：
- `<img>`
- 使用 `duration`

视频 Scene：
- `<video>`
- 只播放 Scene duration

显示 headline overlay。

目标：

> 用户能大致判断素材顺序、文案和节奏。

不要求与最终 MP4 100% 相同。

---

# 19. Remotion

目录：

```text
renderer/
  src/
    Root.tsx
    ProductAd.tsx
    SceneRenderer.tsx
    scenes/
      HookScene.tsx
      ProductRevealScene.tsx
      FeatureScene.tsx
      OfferScene.tsx
      CTAScene.tsx
```

---

## SceneRenderer

伪代码：

```tsx
switch (scene.type) {
  case "hook":
    return <HookScene scene={scene} />

  case "product_reveal":
    return <ProductRevealScene scene={scene} />

  case "feature_demo":
    return <FeatureScene scene={scene} />

  case "offer":
    return <OfferScene scene={scene} />

  case "cta":
    return <CTAScene scene={scene} />

  default:
    return <FeatureScene scene={scene} />
}
```

---

# 20. 视频格式

MVP 固定：

```text
1080 x 1920
9:16
30 fps
H.264 MP4
```

不要增加格式选择。

---

# 21. 第一版视觉规范

保持简单。

### 字幕
- 大号
- 居中偏下
- 最大两行

### CTA
最后一幕固定：

```text
Product Name
Offer
Price
Shop Now
```

### 图片
使用：
- cover
- contain

由 Scene 类型决定。

### 动画
只允许：
- fade
- slight zoom
- slide up

不要做动画编辑器。

---

# 22. AI 重写规则

单 Scene rewrite 请求：

```text
current scene
+
product data
+
reference scene
+
user instruction
```

输出：

```json
{
  "headline": "...",
  "voiceover": "..."
}
```

禁止 AI：
- 自动删除其他 Scene
- 自动改变产品价格
- 自动增加不存在的功能
- 自动改变 SKU 属性
- 编造促销

---

# 23. 真实性约束

跨境电商必须避免 AI 编造商品能力。

所有 Feature 必须来自：

```text
product.features
```

所有价格必须来自：

```text
product.price
```

所有 Offer 必须来自：

```text
product.offer
```

不存在的数据不能生成。

例如：

用户没有提供：

```text
waterproof
```

则禁止生成：

```text
100% waterproof
```

---

# 24. 版权和参考视频原则

参考视频只用于：

```text
structure
pacing
marketing logic
scene purpose
```

最终视频默认使用：

```text
user-owned product assets
```

不要直接复用参考视频中的：
- Logo
- 品牌视觉
- 原产品素材
- 原人物素材
- 原配音
- 原文案

除非调用方明确确认拥有使用权限。

---

# 25. 错误处理

必须处理：

### URL 无法获取

返回：

```json
{
  "error": "REFERENCE_VIDEO_UNAVAILABLE"
}
```

---

### OpenMontage 分析失败

```json
{
  "error": "REFERENCE_ANALYSIS_FAILED"
}
```

保留项目和上传素材。

允许重新执行。

---

### 没有产品素材

禁止 Render。

提示：

```text
Upload at least one image or video.
```

---

### Remotion 失败

```json
{
  "error": "RENDER_FAILED"
}
```

保留 target blueprint。

---

# 26. 推荐目录结构

```text
bag-video-mvp/

frontend/
  src/
    views/
      CreateProject.vue
      SceneReview.vue
    components/
      SceneCard.vue
      SceneList.vue
      SceneEditor.vue
      AssetPicker.vue
      VideoPreview.vue
    stores/
      project.ts

backend/
  app/
    main.py
    api/
      projects.py
      scenes.py
      assets.py
      render.py
    models/
      project.py
      blueprint.py
      asset.py
    services/
      openmontage_adapter.py
      blueprint_normalizer.py
      product_mapper.py
      scene_rewriter.py
      render_service.py
    storage/
      project_store.py

renderer/
  package.json
  src/
    Root.tsx
    ProductAd.tsx
    SceneRenderer.tsx
    scenes/

data/
  projects/

README.md
```

---

# 27. 实现顺序

其他代码模型必须严格按以下顺序实现。

## Phase 1

先完成：

```text
project.json
target_blueprint.json
Scene Review
Scene reorder
Scene edit
```

使用 mock blueprint。

不要先接 AI。

---

## Phase 2

接 Remotion：

```text
target_blueprint.json
→ final.mp4
```

确认人工 JSON 可以正常生成视频。

---

## Phase 3

接 OpenMontage Adapter：

```text
URL
→ reference_blueprint
```

---

## Phase 4

接 Product Mapper：

```text
reference_blueprint
+
product
+
assets
→
target_blueprint
```

---

## Phase 5

单 Scene AI rewrite。

---

# 28. 最重要的开发原则

不要出现这种实现顺序：

```text
先把所有 AI 接完
↓
最后才做视频输出
```

正确顺序：

```text
Mock JSON
↓
Web 可以编辑
↓
Remotion 可以输出
↓
再接 OpenMontage
↓
再接 LLM
```

原因：

> 首先验证确定性 pipeline，然后再增加 AI 的不确定性。

---

# 29. MVP 验收场景

准备：

```text
reference URL:
一条 20~60 秒箱包广告

product:
40L Travel Backpack

assets:
5 张图片
2 个短视频
```

完成以下流程：

### 1
用户创建项目。

### 2
系统完成参考视频分析。

### 3
生成至少：

```text
5 scenes
```

例如：

```text
Hook
Product Reveal
Feature
Feature
CTA
```

### 4
用户在 Web：

把 Scene 4 拖到 Scene 2。

### 5
用户：

把 Scene 3 的图片替换成另一张产品图。

### 6
用户：

把 headline：

```text
40L Capacity
```

修改成：

```text
Pack More. Carry Less.
```

### 7
用户：

把 duration 从：

```text
3
```

修改：

```text
4
```

### 8
Preview 顺序正确。

### 9
点击：

```text
Generate Final Video
```

### 10
得到：

```text
1080x1920 MP4
```

内容顺序、素材、字幕、时长与 Scene JSON 一致。

达到以上条件即视为 MVP 完成。

---

# 30. 非验收项

以下不影响 MVP 验收：

- UI 不够漂亮
- 没有登录
- 没有任务队列
- 没有 CDN
- 没有支付
- 没有自动发布
- 没有高级动画
- 没有智能体聊天
- 没有 OpenClaw
- 没有数据库集群

---

# 31. OpenClaw 的位置

**MVP 第一版不要求 OpenClaw。**

第一版：

```text
OpenMontage
↓
Blueprint
↓
Web Review
↓
Remotion
```

后续再加：

```text
OpenClaw Agent Assistant
```

负责自然语言操作，例如：

```text
“把前三秒做得更抓人”
“把容量卖点提前”
“最后加入免邮”
“把第二幕换成黑色 SKU”
```

OpenClaw 未来只操作标准 API：

```text
get_project
update_scene
move_scene
replace_asset
rewrite_scene
render
```

禁止 OpenClaw 直接修改前端状态。

---

# 32. 最终系统边界

### OpenMontage

负责：

```text
Reference Video
→ Understand
```

### Product Mapper

负责：

```text
Reference Strategy
+
Product
→
Target Video Plan
```

### Web

负责：

```text
Human Review
+
Deterministic Editing
```

### Remotion

负责：

```text
Blueprint
→
MP4
```

### OpenClaw（后续）

负责：

```text
Natural Language
→
API Operations
```

---

# 33. 给代码模型的最终指令

你正在实现的是一个 **MVP**，不是完整视频平台。

任何新增功能都需要先判断：

> 它是否是完成 `URL → Blueprint → Scene Review → MP4` 所必需？

如果不是，禁止加入第一版。

优先保证：

1. Scene Schema 稳定
2. Web Scene Review 可操作
3. Remotion 输出可靠
4. OpenMontage 可替换
5. AI 失败不会破坏项目
6. 商品数据不被 AI 编造

最终必须提供：

```text
README.md
.env.example
frontend/
backend/
renderer/
```

README 必须包含：

```text
install
run frontend
run backend
run renderer
OpenMontage setup
LLM setup
example project
```

并提供一个无需 OpenMontage / LLM 即可运行的：

```text
mock project
```

用于验证：

```text
Scene Review
→ Remotion
→ MP4
```

这是整个 MVP 的最低可运行基线。
