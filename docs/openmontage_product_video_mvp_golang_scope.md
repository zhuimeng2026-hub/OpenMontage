# OpenMontage 商品短视频 MVP 方案

## 1. 文档目的

本方案用于明确一个面向微信生态的商品短视频生成 MVP：

1. 引导用户上传完整、可用的商品素材；
2. 使用视觉模型自动理解、分类商品素材；
3. 通过 LLM 引导用户描述期望的视频效果；
4. 上传对标/参考视频，由 OpenMontage（OM）完成拆解与制作规划；
5. 结合用户商品素材重新生成，而不是机械替换关键帧；
6. 在正式高成本渲染前，提供三层预览确认机制；
7. 接入微信 openid、多租户、额度、任务状态等 SaaS 控制能力；
8. 明确 Golang/Gin 层究竟需要实现多少业务逻辑。

核心原则：

> Golang 层是 SaaS Control Plane，不是视频处理引擎。

> MiniMax M3 是 Product Asset Director，OpenMontage 是 Video Production Director，OpenClaw/Hermes 是业务 Agent 编排层。

---

# 2. 总体架构

```text
微信小程序 / H5 / 公众号
        │
        ▼
UniApp / Vue3 前端
        │
        ▼
Golang / Gin Control Plane
├─ 微信登录 / openid / unionid
├─ tenant / user / role
├─ Product Asset 管理
├─ Project / Job 管理
├─ Quota / Billing
├─ 状态聚合
├─ 文件访问授权
└─ Agent Gateway
        │
        ▼
OpenClaw / Hermes
├─ product-video skill
├─ 调用 MiniMax M3
└─ 调用 OpenMontage MCP
        │
        ├───────────────┐
        ▼               ▼
MiniMax M3         OpenMontage
Product Asset      Video Production
Director           Director
                        │
                        ├─ Reference Analysis
                        ├─ Scene Plan
                        ├─ Asset Binding
                        ├─ Stock / AI Image / AI Video
                        ├─ Remotion
                        ├─ FFmpeg
                        ├─ Storyboard
                        ├─ Sample
                        └─ Final Render
```

## 2.1 各层职责

### Golang/Gin
负责“用户、租户、任务、额度、文件和状态”，不负责创意编排。

### OpenClaw/Hermes
负责把“用户业务意图”转成 AI 工作流，组织 M3 和 OM 的调用。

### MiniMax M3
负责商品图片/视频素材理解、分类、质量评估和缺失提示。

### OpenMontage
负责参考视频分析、脚本/Scene Plan、素材调用决策、镜头制作、合成、预览和正式渲染。

---

# 3. 用户侧完整 MVP 流程

## Step 1：创建商品项目

用户进入“新建视频”页面。

输入：

- 商品名称；
- 商品品类；
- 可选 SKU / 型号；
- 可选品牌信息。

系统生成：

```text
project_id
product_id
tenant_id
user_id
```

注意：

- openid 只用于用户身份绑定；
- 不建议把 openid 直接传入 OM；
- 对 OM/OpenClaw 使用内部 `project_id`、`job_id`、`tenant_id`。

---

# 4. 商品素材上传与 AI 引导

## 4.1 不建议只按“图片张数”判断素材是否足够

应采用“素材槽位完整度”概念。

以箱包为例：

```text
hero_front       产品正面主图
hero_45          产品 45° 图
side             侧面图
back             背面图
open_view        打开状态
inside           内部容量
wheel_detail     轮子细节
handle_detail    拉杆细节
zipper_detail    拉链/五金
logo             品牌 Logo
lifestyle        真人/旅行场景
product_video    产品实拍短视频
```

MVP 推荐最低：

- 6–12 张商品图片；
- 可选 1–3 个实拍短视频；
- 默认目标视频：15–30 秒、9:16。

---

## 4.2 MiniMax M3：Product Asset Director

用户上传素材后，由 M3 自动执行：

1. 判断商品品类；
2. 判断每张图片角色；
3. 判断图片质量；
4. 判断是否重复；
5. 判断是否适合做 Hero / Detail / Lifestyle；
6. 判断素材是否缺失；
7. 输出 Product Manifest。

### Product Manifest 示例

```json
{
  "product_id": "luggage_x01",
  "category": "luggage",
  "assets": [
    {
      "asset_id": "asset_001",
      "file": "01.jpg",
      "role": "hero_front",
      "quality_score": 0.94
    },
    {
      "asset_id": "asset_002",
      "file": "02.jpg",
      "role": "hero_45",
      "quality_score": 0.91
    },
    {
      "asset_id": "asset_003",
      "file": "03.jpg",
      "role": "open_view",
      "quality_score": 0.88
    },
    {
      "asset_id": "asset_004",
      "file": "04.jpg",
      "role": "wheel_detail",
      "quality_score": 0.93
    }
  ],
  "missing_roles": [
    "lifestyle",
    "handle_detail"
  ]
}
```

---

## 4.3 前端素材完整度提示

建议用户看到：

```text
素材完整度：72%

✓ 产品正面
✓ 45°展示图
✓ 内部容量图
✓ 轮子细节
○ 真人使用场景
○ 拉杆细节
```

提示：

> 当前素材足够生成 15–20 秒商品视频。补充真人使用场景和拉杆细节，可提高视频丰富度。

### MVP 原则

AI 自动分类，但必须允许用户手工修正。

---

# 5. 用户效果描述：LLM 引导，而不是空白 Prompt

普通业务用户通常不知道怎样写视频 Prompt，因此前端应该提供 LLM 引导。

## 5.1 用户可直接自然语言输入

例如：

> 做一条 20 秒竖屏旅行箱获客视频，突出轻便、容量大、轮子顺滑，整体高级但不要太沉闷。

---

## 5.2 LLM 提供常用场景建议

可以根据商品品类自动提供：

### 箱包类

- 机场旅行
- 商务出差
- 高铁出行
- 家庭旅行
- 留学/跨境
- 登机箱轻便
- 大容量
- 轮子静音
- 抗摔耐磨
- 收纳展示

用户只需选择或补充。

---

## 5.3 将自然语言转成结构化 Creative Brief

```json
{
  "goal": "lead_generation",
  "duration": 20,
  "aspect_ratio": "9:16",
  "tone": "premium_modern",
  "selling_points": [
    "lightweight",
    "large_capacity",
    "smooth_wheels"
  ],
  "scenario": "airport_travel",
  "language": "zh-CN",
  "cta": "微信咨询"
}
```

Golang 层只负责存储，不需要自己理解这些内容。

---

# 6. 上传对标视频并拆解

用户上传：

- 本地视频；或
- 允许的参考视频 URL。

交给 OpenMontage 的 Reference Analysis。

OM 重点提取：

```text
Transcript
Pacing
Shot boundaries
Scenes
Representative keyframes
Visual style
Hook structure
Scene progression
Text / Caption logic
Audio rhythm
Marketing intent
```

注意：

> 不应把关键帧理解成“用户产品图片的直接替换目标”。

正确流程：

```text
Reference Frame
      ↓
理解 Scene 的角色 / 目的
      ↓
Scene Slot
      ↓
Product Asset Binding
      ↓
重新构建 Scene
```

---

# 7. Reference Influence：参考视频与用户描述冲突处理

这是 MVP 的核心参数之一。

前端建议只提供 3 档：

## 7.1 描述优先

```text
以我的需求为主
```

参考视频只作为创意灵感。

## 7.2 平衡模式（默认）

```text
兼顾我的需求和参考视频
```

保留：

- Hook；
- 结构；
- 节奏；
- 部分 Scene 逻辑。

但允许重写视觉、文案和具体镜头。

## 7.3 参考优先

```text
尽量贴近参考视频
```

优先保持：

- Scene 顺序；
- 大致时长；
- 镜头角色；
- 节奏。

但仍然重新制作自己的产品版本，而不是像素级复刻。

---

# 8. OpenMontage 的 Product Remix Pipeline

建议给 OM 增加一套轻量业务 Pipeline，而不是改 OM 核心。

```text
pipeline_defs/
└── product-remix.yaml

skills/pipelines/product-remix/
├── reference-director.md
├── scene-director.md
├── asset-director.md
├── preview-director.md
├── edit-director.md
└── compose-director.md
```

---

# 9. Scene Director：把参考视频转成“场景槽位”

例如参考视频：

```text
Scene 01  痛点 Hook
Scene 02  产品首次出现
Scene 03  轮子顺滑
Scene 04  容量展示
Scene 05  旅行场景
Scene 06  CTA
```

输出：

```json
{
  "scene_id": 3,
  "intent": "feature_proof",
  "required_asset_role": "wheel_detail",
  "duration": 2.4,
  "motion": "closeup_tracking",
  "reference_strength": 0.7
}
```

---

# 10. Asset Director：绑定用户商品素材

Asset Director 必须遵循优先级：

1. 优先用户真实产品图片/视频；
2. 真实素材足够时，不重新生成产品外观；
3. 静态商品镜头优先 `用户图片 + Remotion`；
4. 泛场景优先 Stock；
5. 人物与商品复杂互动，再调用 AI Image / AI Video；
6. 没必要时，不用昂贵的视频生成模型。

### 示例

```text
Scene 03 requires wheel_detail
        ↓
Product Manifest
        ↓
asset_004 / wheel_detail.jpg
        ↓
OM decides:
Remotion animation / real footage / AI motion
```

---

# 11. 四种 Scene 生产策略

| Scene 类型 | 例子 | 推荐策略 |
|---|---|---|
| 产品 Hero | 白底产品、产品亮相 | 产品图 + Remotion |
| 产品细节 | 轮子、拉杆、拉链 | 用户细节图/视频优先 |
| 泛环境 | 机场、酒店、街道 | Stock 优先 |
| 人物+产品互动 | 拖箱、开箱、使用 | 实拍优先；不足再 AI Video |

不要让所有 Scene 都走 AI Video。

---

# 12. 三层预览机制

这是本方案中非常重要的降成本设计。

正式生成前分三级确认。

---

## Level 1：Storyboard Preview

### 用户看到

每个 Scene 显示：

```text
参考视频关键帧
VS
新方案准备使用的素材/预览图
```

同时显示：

- Scene 用途；
- 时长；
- 用户素材；
- 文案；
- 预计生成方式。

### 示例

```text
Scene 03

参考：机场低机位轮子镜头
新版本：wheel_detail.jpg

用途：轮子卖点
预计：2.4 秒
方式：Remotion + motion
文案：顺滑静音
```

### 成本

极低。

---

# 13. Level 2：Animatic Preview

使用已有静态素材快速合成低成本预演。

可以包含：

- 图片；
- 临时字幕；
- 简单 Zoom / Pan；
- TTS；
- 音乐；
- Scene 时长；
- 简单转场。

推荐：

```text
360p / 540p
低码率
快速 Remotion Render
```

目的不是展示最终画质，而是确认：

- Scene 顺序；
- 节奏；
- 产品素材是否选对；
- 文案是否合适；
- 参考视频结构是否保留合理。

### 成本

很低。

---

# 14. Level 3：Representative Sample

只对 1–3 个最贵、最关键的 Scene 做真实高质量生成。

例如：

- 人拖着用户箱包走机场；
- AI 场景视频；
- 复杂产品运动镜头。

用户先看样片。

确认后才进入 Final Render。

### 成本

中等，但远低于完整生成失败后的返工成本。

---

# 15. Final Render

用户确认：

```text
Storyboard
   ↓ approved
Animatic
   ↓ approved
Representative Sample
   ↓ approved
Final Production
```

OM 再执行：

```text
Asset generation
Edit decisions
Composition
Remotion / FFmpeg
QA
Final MP4
```

---

# 16. 推荐的前端页面结构

## 页面 1：商品素材

```text
商品：XX 登机箱

素材完整度：72%

[主图 ✓]
[45°图 ✓]
[内部图 ✓]
[轮子 ✓]
[拉杆 ○]
[真人场景 ○]

[继续上传]
[下一步]
```

---

## 页面 2：视频目标

```text
我要制作：
[20秒]
[9:16]
[中文]

主要场景：
[机场旅行]

主要卖点：
✓ 轻便
✓ 大容量
✓ 轮子顺滑

其他要求：
[自然语言输入]
```

---

## 页面 3：参考视频

```text
[上传视频]
或
[粘贴参考视频链接]

参考程度：
○ 我的需求优先
● 平衡
○ 参考视频优先

[分析视频]
```

---

## 页面 4：Storyboard Compare

```text
Scene 01
参考关键帧 | 新版预览
Hook         | Hook
2.0s         | 2.2s

Scene 02
参考关键帧 | hero_45.jpg
产品亮相    | 产品亮相

Scene 03
参考关键帧 | wheel_detail.jpg
轮子展示    | 顺滑静音
```

操作：

```text
[换素材]
[修改文案]
[删除 Scene]
[调整顺序]
[重新生成此 Scene]
```

---

## 页面 5：Animatic

左右可选：

```text
参考视频
VS
低清预演视频
```

确认后：

```text
[生成关键场景样片]
```

---

## 页面 6：Sample / Final

```text
关键 Scene 样片
[播放]

预计本次正式生成消耗：XX credits

[确认正式生成]
```

---

# 17. Golang/Gin 层必须实现什么

这是开发评估最需要关注的部分。

## 17.1 必须实现

### A. 微信身份

- 微信登录；
- openid；
- unionid（如有）；
- session/token；
- internal user_id。

### B. 多租户

```text
tenant
user
tenant_user
```

所有业务资源必须带：

```text
tenant_id
user_id
```

### C. Product / Asset 管理

```text
product
product_asset
product_manifest
```

负责：

- 上传；
- 文件路径；
- 元数据；
- AI 分类结果；
- 人工修正结果；
- 素材可见性。

### D. Project / Job

```text
video_project
production_job
preview_job
render_job
```

负责：

- 创建任务；
- 记录 OM project_id；
- 记录 OpenClaw run_id；
- 状态；
- 错误；
- 重试；
- 结果文件。

### E. Quota / Billing

至少 MVP 需要：

```text
available_credits
reserved_credits
consumed_credits
```

高成本 Final Render 前建议先 reserve。

### F. Agent Gateway

Gin 不需要理解 OM 内部细节。

提供统一业务调用：

```text
AnalyzeProductAssets
AnalyzeReferenceVideo
GenerateStoryboard
GenerateAnimatic
GenerateSample
RenderFinal
GetProductionStatus
CancelProduction
```

内部可以转成 OpenClaw/MCP 调用。

### G. 状态聚合

前端不直接依赖 OM MCP 原始状态。

统一映射：

```text
CREATED
ASSET_ANALYZING
REFERENCE_ANALYZING
PLANNING
STORYBOARD_READY
ANIMATIC_RENDERING
ANIMATIC_READY
SAMPLE_RENDERING
SAMPLE_READY
WAITING_APPROVAL
FINAL_RENDERING
COMPLETED
FAILED
CANCELLED
```

### H. 文件权限

不要直接把 OM 工作目录裸露给微信用户。

应由 Gin 提供：

- signed URL；
- tenant 权限校验；
- 文件生命周期；
- 下载/预览鉴权。

---

# 18. Golang/Gin 层不应该实现什么

MVP 阶段不要自己实现：

- Scene Detection；
- Keyframe Extraction；
- Video Understanding；
- Script Generation；
- Scene Plan；
- AI Image Prompt Engineering；
- AI Video Prompt Engineering；
- Remotion Timeline；
- FFmpeg 编排；
- Stock 素材选择；
- AI 模型路由；
- 视频 QA；
- Product Asset 与 Scene 的创意绑定逻辑。

这些尽量交给：

```text
MiniMax M3
OpenClaw/Hermes
OpenMontage
```

---

# 19. 建议 Golang API

## Auth

```text
POST /api/auth/wechat/login
```

## Products

```text
POST /api/products
GET  /api/products/:id
POST /api/products/:id/assets
GET  /api/products/:id/assets
POST /api/products/:id/analyze-assets
GET  /api/products/:id/manifest
PUT  /api/products/:id/manifest
```

## Video Projects

```text
POST /api/video-projects
GET  /api/video-projects/:id
PUT  /api/video-projects/:id/brief
POST /api/video-projects/:id/reference
```

## Production

```text
POST /api/video-projects/:id/analyze-reference
POST /api/video-projects/:id/storyboard
POST /api/video-projects/:id/animatic
POST /api/video-projects/:id/sample
POST /api/video-projects/:id/render
POST /api/video-projects/:id/cancel
```

## Status

```text
GET /api/video-projects/:id/status
GET /api/jobs/:job_id
```

## Approval

```text
POST /api/video-projects/:id/approve-storyboard
POST /api/video-projects/:id/approve-animatic
POST /api/video-projects/:id/approve-sample
```

---

# 20. 核心数据库表建议

MVP 可以先 PostgreSQL；如果仍想保持轻量，也可以 SQLite 起步，但多租户 SaaS 更建议 PostgreSQL。

## users

```text
id
openid
unionid
nickname
status
created_at
```

## tenants

```text
id
name
status
created_at
```

## tenant_users

```text
tenant_id
user_id
role
```

## products

```text
id
tenant_id
name
category
sku
created_by
created_at
```

## product_assets

```text
id
tenant_id
product_id
file_key
media_type
role
quality_score
ai_metadata_json
created_at
```

## video_projects

```text
id
tenant_id
product_id
creative_brief_json
reference_mode
reference_file_key
status
created_by
created_at
```

## production_jobs

```text
id
tenant_id
video_project_id
job_type
external_run_id
om_project_id
status
progress
cost_reserved
cost_actual
error_message
created_at
updated_at
```

## preview_artifacts

```text
id
video_project_id
scene_id
preview_type
file_key
metadata_json
created_at
```

---

# 21. OpenClaw/Hermes Skill 建议

可以增加一个业务 Skill：

```text
product-video-production
```

负责：

```text
1. 读取 product manifest
2. 读取 creative brief
3. 调 OM reference analysis
4. 生成 scene plan
5. 要求 OM 绑定用户真实素材
6. 请求 storyboard
7. 请求 animatic
8. 等用户 approval
9. 请求 representative sample
10. 等 approval
11. 请求 final render
```

OpenClaw/Hermes 适合做业务 Agent，不应该承担：

- 用户认证；
- tenant 隔离；
- billing 真值；
- 数据库主状态。

这些仍由 Gin Control Plane 持有。

---

# 22. MCP / OM 侧建议新增的薄封装

如果当前 OM MCP 没有直接提供以下业务接口，可以在 OM 外围做非常薄的 MCP tool/skill：

```text
prepare_product_remix
prepare_storyboard_preview
prepare_animatic_preview
prepare_representative_sample
get_preview_artifacts
approve_stage
```

注意：

这些只是“业务入口封装”，不应重写 OM 内部视频处理能力。

---

# 23. 三层预览对应的数据结构

## Storyboard

```json
{
  "scene_id": 3,
  "reference_frame": "...",
  "new_asset": "...",
  "intent": "feature_proof",
  "duration": 2.4,
  "caption": "顺滑静音",
  "strategy": "remotion"
}
```

## Animatic

```json
{
  "preview_type": "animatic",
  "resolution": "540x960",
  "file": "animatic.mp4",
  "duration": 20
}
```

## Sample

```json
{
  "preview_type": "sample",
  "scene_ids": [3, 5],
  "files": [
    "scene_03_sample.mp4",
    "scene_05_sample.mp4"
  ]
}
```

---

# 24. 失败与降级策略

## 用户素材不足

```text
优先提示补传
↓
用户跳过
↓
OM 判断 Stock / AI Image / AI Video
```

## 参考视频解析失败

允许：

```text
重新上传
或
取消参考视频，按 Creative Brief 生成
```

## AI 视频生成失败

降级：

```text
AI Video
↓ failure
AI Image + Remotion
↓
Stock + 产品 Overlay
```

## Final Render 失败

必须保持：

- job_id；
- checkpoint；
- 已生成资产；
- 可恢复状态。

避免整条 Pipeline 从头重跑。

---

# 25. MVP 第一阶段建议明确不做

为了控制 Golang 工作量，第一版不建议做：

- 专业 NLE 时间线编辑器；
- 任意帧级手工替换；
- AE/Premiere 式复杂轨道；
- 用户自定义 Prompt Engineering；
- 全品类自动最佳素材模板；
- 多模型价格优化调度；
- 极细粒度 Scene 参数编辑；
- 多人在线协同；
- 完整 DAM 系统。

---

# 26. 推荐 MVP 范围

## 用户能做

```text
1. 微信登录
2. 创建产品
3. 批量上传素材
4. AI 自动分类 + 手工修正
5. 描述视频效果
6. 选择推荐场景/卖点
7. 上传参考视频
8. 选择参考强度
9. 看 Storyboard
10. 看 Animatic
11. 看关键 Scene Sample
12. 确认 Final Render
13. 查看任务进度
14. 下载/分享最终视频
```

## 系统自动做

```text
商品素材理解
参考视频分析
Scene Plan
素材 Binding
缺失素材补齐策略
Stock / AI Image / AI Video 路由
Remotion
FFmpeg
Preview
QA
Final Render
```

---

# 27. Golang 工作量判断

如果严格遵循本方案，Golang 层工作主要集中在 SaaS 基础设施，而不是 AI 视频算法。

大致可以分为：

## 较明确、常规开发

- 微信登录；
- 用户/租户；
- 产品/素材 CRUD；
- 上传；
- 任务 CRUD；
- 状态机；
- 额度；
- 权限；
- Agent Gateway；
- Webhook / Polling；
- 文件鉴权。

## 需要与 OM 联调

- OM project/job ID 映射；
- Product Manifest 传递；
- Creative Brief 传递；
- Reference Video 传递；
- 三层 Preview Artifact 获取；
- Approval Gate；
- Final Render；
- Error / Retry。

## 不建议 Golang 实现

- 视频智能理解；
- 产品图分类；
- Creative Planning；
- Scene 生成；
- AI 视频工具选择；
- Timeline 合成。

因此，若 OM/OpenClaw 接口打通，**Golang 层的新增 AI 专属逻辑应该很薄**。

---

# 28. 最终推荐边界

```text
微信生态 / SaaS 规则
            ↓
          Gin
            ↓
     OpenClaw / Hermes
            ↓
     ┌──────────────┐
     ↓              ↓
MiniMax M3      OpenMontage
商品素材理解     视频生产编排
     └──────┬───────┘
            ↓
       Preview / Final
```

一句话定义：

> **Gin 管“谁可以做、做什么、花多少、任务属于谁”；MiniMax 管“用户有哪些商品素材”；OpenMontage 管“这条视频具体怎么做”；OpenClaw/Hermes 管“把这些业务步骤串起来”。**

---

# 29. 建议开发优先验证的 5 个技术 Spike

在正式开发完整前端/后端前，建议先独立验证：

1. **M3：10 张箱包图 → Product Manifest** 是否稳定；
2. **OM：参考视频 → Scene Plan** 是否达到可用准确度；
3. **OM：Product Manifest + Scene Plan → Asset Binding** 是否能通过 Skill 实现；
4. **OM：Storyboard / Animatic / Sample** 是否可从现有输出稳定暴露；
5. **OpenClaw → MCP → OM** 的 project/job/status/render 调用链是否可靠。

这 5 个 Spike 全部通过后，再投入较多 Golang SaaS 开发，风险最低。

