# 模板市场 · 前端交互说明（`bff/web/`）

本文档描述 FrameFlow 前端「模板市场」模块的交互契约，便于后续接入**真实市场 API** 时前后端对齐。

> 架构铁律（同 `web/README.md`）：前端**绝不**持有 `MCP_API_TOKEN` / 微信 `appSecret`。
> 任何市场数据获取、模板文件下载都应经 BFF 转发，由 BFF 统一鉴权与缓存。

---

## 1. 当前实现状态

| 维度 | 现状 |
| --- | --- |
| 数据来源 | **前端静态占位**（无后端市场 API） |
| 模板清单 | `index.html` 模板市场 6 张 `.vcard` 卡片，写死在 HTML |
| 详情数据 | `index.html` 内联脚本 `TEMPLATE_CATALOG`（key → 模板元数据） |
| 模板「文件」 | 由元数据在前端**合成**的 `template.json`（非真实文件） |
| 持久化 | 无；刷新即重置 |

> 修复记录：此前模板卡片**无点击事件**，点击无响应（打不开模板文件）。
> 已补全卡片 `data-tmpl` 属性 + 详情弹窗逻辑，点击可预览 / 下载 / 套用。

---

## 2. 模板「文件」Schema（TemplateFile）

弹窗与「下载文件」均基于以下结构。后续真实 API 返回的模板对象应**至少**覆盖这些字段，
以便前端零改造复用现有渲染 / 套用逻辑。

```json
{
  "name": "旅行混剪",                       // 模板显示名（必填）
  "aspect_ratio": "9:16",                  // 画幅比例：9:16 | 16:9 | 1:1（必填）
  "duration_per_image": 3,                 // 单张图片时长（秒，整数，必填）
  "variants": 12,                          // 变体数量（用于列表「N 个变体」展示，必填）
  "type": "frameflow.template",            // 类型标识（用于校验/路由，建议保留）
  "description": "将旅途照片按时间线混合剪辑…", // 详情说明（可选，缺省不展示）
  "gradient": "135deg,#7C5CFF,#4FD8FF",    // 卡片/横幅渐变（可选；缺省后端回退默认渐变）
  "category": "travel",                    // 业务分类 key（可选，用于筛选）
  "preview_url": "https://…/cover.jpg",    // 封面图（可选，替代纯渐变缩略）
  "create_match": "旅行混剪"                // 套用时在创建页匹配的同名模板（可选）
}
```

字段约束：
- `aspect_ratio` 仅允许 `9:16` / `16:9` / `1:1` 三者之一，否则套用页渲染需做兜底。
- `duration_per_image` 必须为正整数（当前创建页默认 30 帧≈对应秒数，前端按秒传 `create_remotion_video_share`）。
- `name` 与创建页模板模式 `.tmpl .tn` 文本一致时，「使用此模板」会**自动选中**对应模板；
  不一致时仅跳转创建页，由用户手动选择。

---

## 3. 交互流程

```
模板市场列表（view-templates）
   │  渲染 6+ 张 .vcard（data-tmpl=<key>）
   ▼
用户点击卡片
   │  click → openTemplate(key)
   ▼
详情弹窗（#tmpl-modal）
   │  展示：横幅 + 标题 + 变体/比例/时长 + 说明 + template.json 预览
   ├── 「下载文件」→ 导出 <name>.template.json（前端合成，Blob 下载）
   ├── 「使用此模板」→ 跳转创建页（view-create），按 create_match 自动选中 .tmpl
   └── 关闭：遮罩点击 / 关闭按钮 / Esc
```

- 弹窗为 `position:fixed` 覆盖层（`z-index:60`），打开时不影响其它视图状态。
- 套用跳转通过调用 `.nav-item[data-view="create"]` 的 `click()` 实现，复用已有路由，
  **不**引入新的路由/历史栈，避免与侧边栏导航状态冲突。

---

## 4. DOM 契约（前端对接锚点）

| 元素 / 属性 | 作用 | 接真实 API 时需关注 |
| --- | --- | --- |
| `#view-templates .vcard[data-tmpl]` | 列表卡片，点击触发弹窗 | 改为由数据渲染时，需保留 `data-tmpl` 为模板唯一 id |
| `TEMPLATE_CATALOG`（内联脚本） | 当前静态元数据字典 | 接 API 后改为 `fetch` 结果填充；建议保留该对象作为内存缓存 |
| `#tmpl-modal` 及子节点 `#tmpl-title/#tmpl-sub/#tmpl-variants/#tmpl-ratio/#tmpl-duration/#tmpl-desc/#tmpl-file` | 详情弹窗内容挂载点 | 字段映射见 §2，按 JSON 字段填值即可 |
| `#tmpl-use` / `#tmpl-download` / `#tmpl-close` | 套用 / 下载 / 关闭按钮 | 行为无需改动，仅需保证 `currentTmpl` 指向真实对象 |
| `#mode-template .tmpl[data-name]` 或 `.tn` 文本 | 创建页模板选择器 | 接 API 后确保 `create_match` 与某 `.tn` 文本一致以自动选中 |

---

## 5. 接入真实市场 API（建议方案）

### 5.1 BFF 侧（推荐）
新增市场代理路由，由 BFF 持有市场服务的鉴权凭证并缓存：

```
GET  {bffBaseUrl}/api/templates          → 模板清单（数组，字段见 §2）
GET  {bffBaseUrl}/api/templates/:id      → 单个模板详情（含 template.json 原文）
GET  {bffBaseUrl}/api/templates/:id/file → 模板文件下载（可直链 CDN）
```

前端仅与 BFF 对话，沿用 `mcp-client.js` 的 BFF 转发模式，**不**直连市场服务。

### 5.2 前端改造点（最小改动）
1. 列表渲染：将 `TEMPLATE_CATALOG` 改为 `await fetch('/api/templates')` 的结果，
   动态生成 `.vcard[data-tmpl=id]`（保留 §4 的全部属性）。
2. 详情打开：`openTemplate(id)` 改为先查本地缓存，未命中则 `fetch('/api/templates/'+id)`。
3. 下载：「下载文件」可直接指向 `/api/templates/:id/file` 或仍由前端合成（二选一）。
4. 套用逻辑不变（§3）。

### 5.3 待对齐事项（接 API 前需与后端确认）
- [ ] 市场服务返回的字段名是否已对齐 §2（尤其 `aspect_ratio` / `duration_per_image` / `variants`）。
- [ ] 分类 / 筛选（`category`）、搜索是否由后端支持，还是前端本地过滤。
- [ ] 模板封面：`preview_url` 是否提供；缺省时沿用渐变 `gradient`。
- [ ] 模板文件存储：是可信 CDN 直链，还是经 BFF 代理下载。
- [ ] 鉴权：市场接口是否需要登录态（`ff_sid` cookie 已具备），BFF 是否已统一注入。

---

## 6. 回归检查项（见对话回归清单）

模板市场卡片点击交互的回归用例（1–7 项）已随 Bug 修复一并给出，验收时逐项勾选即可。
重点覆盖：列表渲染、hover 提示、点击弹窗、template.json 预览、下载、套用自动选中、关闭（遮罩/Esc）。
