# Frameflow 跨用户数据隔离审计 — 2026-08-19

> 本次提交后用户反馈"创建页显示的上传图片需要在不同用户之间互相隔离，目前不同用户数据互相可见"。本文档为多代理并行审计结论汇总，供后续修复与回归追踪使用。

## 0. TL;DR

- **后端 scope 隔离：结构性正确**，未发现直接泄漏。
- **前端 SPA：6 个已确认泄漏（BUG1-6）**，全部由"用户身份变更时模块作用域状态未清理"这一根因引起。
- **触发提交**：`954871a feat(frameflow): 创建页提交后保留本次任务(图+脚本)整包，切页不丢` 引入 `currentTask`/`paintCurrentTask()` 但未配套用户切换时的清理；`9ab14b0 feat(frameflow): 创建页支持多任务图生视频与实时任务可见` 增加了 5s 轮询，同样未清理。
- **测试覆盖**：仅函数级（`renderQueueOwnerID` 计算、`RenderJobStore` 隔离），**无任何 HTTP 级 Alice↔Bob 或 匿名↔登录 跨用户回归**。

## 1. 后端 scope 隔离审计（`frameflow/bff/`）

### 1.1 结论：无确认 Bug

`renderQueueOwnerID(sid)` 是唯一身份派生函数：

```go
// frameflow/bff/handlers/auth.go:95-107
func renderQueueOwnerID(sid string) string {
    identity := "session:" + sid
    if u := loadUserMap(sid); u != nil {
        if openid, ok := u["openid"].(string); ok && openid != "" {
            identity = "wechat:" + openid
        }
    }
    sum := sha256.Sum256([]byte(identity))
    return hex.EncodeToString(sum[:])
}
```

已审计的下述存储与处理器都按 `scope` (= `session_id`) 过滤，DB schema 用 `UNIQUE/PRIMARY KEY` 强制隔离：

| 文件 | 已校验的方法 |
|------|--------------|
| `internal/imagebatch/store.go` | `Create` / `Get` / `ByProject` / `IncAsset` / `SetAssetCount` / `Update` / `List` |
| `internal/mcp/session.go` | `Call` / `CallBatch` / `CreateBatch` / `DropBatch` / `RecordJob` / `UpdateJobResult` / `ResetAsset` |
| `internal/mcp/render_job_store.go` | `Record` / `List` / `UpdateStatus` / `UpdateResult` |
| `internal/composition/store.go`、`internal/script/store.go`、`internal/template/store.go` | 全部以 `sessionID` (= scope) 为 key |
| `handlers/session_assets.go` | `listSessionAssets` 始终按 scope 调 MCP；`ServeAsset` 在白名单 403 之前做 scope 校验 |
| `handlers/queue.go`、`handlers/progress.go` | `refreshJobStatuses`、`RenderProgress`、`RepublishRender` 全部走 `OwnsJob(scope, …)` / `ListJobs(scope)` |
| `handlers/mcp.go` | `queue_owner_id` 被服务器覆盖为 `renderQueueOwnerID(sid)`，不信任前端传入 |
| `internal/state/db.go` | `image_batches` 用 `UNIQUE INDEX(session_id, project_id)`；`mcp_batch_sessions` 用 `PRIMARY KEY(session_id, batch_id)` + `UNIQUE(session_id, project_id)`；`render_jobs` 用 `PRIMARY KEY(session_id, job_id)` |

### 1.2 后端 LOW 级隐患（不在本次用户报告范围，建议顺带修）

| ID | 位置 | 风险 |
|----|------|------|
| **S1** | `handlers/wechat.go:169-194` `QrLoginStatus` | 票据不绑发起方 `ff_sid`，扫码端之外的浏览器只要拿到 ticket id 也可绑定身份（缓解：5min TTL + 128bit 熵 + 单次消费） |
| **S2** | `main.go:100` | `/api/_dev_login` 路由无条件注册，仅靠 `DEV_LOGIN_ALLOWED` 三态开关保护，生产误配 `DEV_LOGIN_ALLOWED=true` 会变成公共模拟登录入口 |
| **S3** | `handlers/auth.go:133-165` `loadUserMap` | 缓存命中 → DB 回填之间存在 `isExpired` 检查窗口，DB 已过期但已落入 in-memory 的用户会被错误地认为仍登录（不影响跨用户隔离） |
| **S4** | `main.go:78-119` | 部分写路由（`/api/scripts`、`/api/compositions` Create/Render、`/api/templates` Create/AddScenario/BatchRender、`/api/quota`）未 `RequireAuth`，匿名用户可消耗上游配额 |
| **S5** | `handlers/progress.go:14-84` `RenderProgress` | 唯一防护是 `OwnsJob(scope, jobId)`，`scope` 为空时静默 404 — 不漏但易回归 |

## 2. 前端 SPA 审计（`frameflow/bff/web/`）

### 2.1 已确认 Bug

#### BUG1 — `currentTask` 卡片跨用户泄漏（A 的图片 → B 视图）

**位置**：`frameflow/bff/web/index.html:1814, 2159-2172, 2666-2671, 2275-2305, 1141-1142`

**路径**：

```js
// index.html:1814
var currentTask = null;     // 最近一次提交的任务（持久化展示在创建页顶部卡片）

// index.html:2159-2172 (模板模式提交)
currentTask = {
  jobId, mode: 'template',
  images: uploadedImages.map(im => ({url: im.url, name: im.name})),  // ← blob: URL 仍有效
  templateName, scriptName, status, statusClass, progress, shareUrl, submittedAt
};

// index.html:941-952 doLogout()
function doLogout(){
  fetch(bffBase() + '/api/logout', {method:'POST', credentials:'include'})
    .catch(function(){})
    .then(function(){
      window.__ffUser = null;     // ← 只置 null，从不清理 currentTask
      renderUserInfo(); clearLoginExpired();
      ...
    });
}

// index.html:1140-1146 任何用户进入创建页
if (view === 'create') {
  refreshQuota(); loadScriptPicker(); refreshSessionAssets();
  paintCurrentTask(); // ← 无条件渲染
  loadCreateTasks();
  ...
}
```

**实际场景**：A 上传 5 张图 → 渲染 → 卡片持有 5 个 `URL.createObjectURL` → A 退出登录 → B 在同标签内扫码登录 → B 进入创建页 → `paintCurrentTask()` 仍渲染 A 的 5 张缩略图（`<img src="blob:...">` 真实抓取 A 的文件字节）+ A 的 jobId / shareUrl。

#### BUG2 — `uploadedImages` / `scriptUploadedImages` 数组残留

**位置**：`index.html:1783, 2318, 1887-1909, 2474-2498, 2044-2049, 2501-2508`

**路径**：`renderUploadPreview()` 与 `renderScriptUploadPreview()` 都从这两个数组读取；`clearUploadedImages()` 仅 `restartForNewGroup` 在 `index.html:2219` 调用；`clearScriptUploadedImages()` 没有任何调用方（死代码）。

#### BUG3 — `paintCurrentTask()` 不读服务器、不检查 `__ffUser`

**位置**：`index.html:2275-2305`

纯本地缓存渲染，无 `/api/render-queue` 反查校准。

#### BUG4 — 轮询计时器跨登出/登录继续跑

**位置**：`index.html:1004 (checkSessionAlive 60s)`, `1144-1148 (createTasksTimer 5s)`, `1760-1770 (queueTimer 5s)`

`queueTimer` 在 `dashboard` nav 点击时**未被清除**（仅在非 queue/dashboard nav 才 clear）。`doLogout` / `onSessionLost` 都不清除任何 timer。

#### BUG5 — `sessionAssets` / `queueSubs` / SSE 未清理

**位置**：`index.html:941, 958, 1475, 2511, 2181, 2675, 1505`

SSE `EventSource` 从未被 `close()`，`queueSubs` map 累积。

#### BUG6 — Monaco 编辑器跨用户残留

**位置**：`index.html:2314, 2358-2378, 1956-1962`

`initMonaco()` 仅在首次 `ffMonaco == null` 时执行；用户 A 选了某个 user-script 打开后退出，B 进脚本模式仍看到 A 的代码。

### 2.2 加固项

| ID | 位置 | 说明 |
|----|------|------|
| S1 | `index.html:1797, 1780` `currentImageBatch` / `lastBatchId` 残留 | B 首次点"开始渲染"时携带 A 的 batchId，服务器返回 404（UX bug 而非数据泄漏） |
| S2 | `index.html:2448, 2651` `__ffCompId` | B 第一次保存前短暂保留 A 的 composition id（潜在面） |
| S3 | `index.html:1813, 2171, 2672` `submittedTasks` | 仅写不读，累积 PII 与内存 |
| S4 | `index.html:1619-1633` 头像 backgroundImage | `renderUserInfo()` 已清，但异步 `/api/logout` 与 UI 更新有竞争窗口 |
| S5 | `index.html:1004` `checkSessionAlive` | 登出后变 no-op 但 interval 未清除 |

### 2.3 无问题项

- `localStorage` / `sessionStorage` / `IndexedDB` / `BroadcastChannel`：grep 全无匹配 → 泄漏**仅限同标签**（页面刷新即丢失）
- `/api/session/assets`、`/api/assets?rel=`、`/api/render-queue`、`/api/render-progress/:jobId`：服务器端均按 scope 正确隔离
- `mcp-client.js`、`config.js`：无 per-user 缓存、无 token、无跨标签通讯
- `esc` / `escAttr` / `safeDownloadURL`：HTML 转义与 URL 校验完整，无 XSS 放大面

## 3. 最近提交回归分析

按可能性从高到低：

| # | Commit | 影响 |
|---|--------|------|
| **#1** | `954871a feat(frameflow): 创建页提交后保留本次任务整包，切页不丢` (Aug 19 08:23) | **主嫌疑**。`currentTask`/`submittedTasks`/`batchSubmitted` 闭包状态 + `paintCurrentTask()` 无条件渲染，无用户切换清理 |
| #2 | `9ab14b0 feat(frameflow): 创建页支持多任务图生视频与实时任务可见` (Aug 19 07:29) | 加 `createTasksTimer` 5s 轮询 + `loadCreateTasks()`；服务端 `/api/render-queue` 本身已正确 scope，但加剧了泄漏可见性 |
| #3 | `5d97c04 feat(frameflow): 创建页展示本会话已上传图片` | 加 `sessionAssets` 缓存，refresh 时机依赖 view-entry，匿名→登录切换窗口有泄漏面 |
| #4 | `22b852c fix(frameflow): route ServeAsset through MCP so 404 thumbnails get served` | 让泄漏**实际可见**（之前本地 fs 404 把 bug 遮住了） |
| #5 | `8f1f272 feat(bff): 渲染任务状态统一以 MCP 后台为唯一权威源实时刷新` | 服务端 refresh，本身不引入泄漏 |
| #6 | `a822870 fix(bff): 修复默认脚本选择的不确定性` | 无关 |

## 4. 测试覆盖缺口盘点

| 场景 | 现有覆盖 |
|------|----------|
| A1 `/api/session-assets` Alice 上传 Bob 不能列 | **无** |
| A2 `/api/asset?rel=` Bob 拿正确 rel 也 403 | **无** |
| A3 `/api/image-batches` List/Get/Render Bob 隔离 | **无**（仅函数级） |
| A4 `/api/image-batches/{id}/render` Bob 看不到 Alice 的 jobId | 部分（`republish_test.go:9-15` 仅 RenderQueue） |
| A5 render 完成 shareUrl 只在 Alice 视图 | 部分（store 级） |
| B6 匿名 A vs B 互不可见 | 函数级（`scope_test.go:46`） |
| B7 匿名 session 跨重启保留 | 仅 `wechat_users`（`auth_test.go:59`） |
| **B8 匿名→登录 过渡**（最微妙的潜在面） | **无** |
| **B9 登出→匿名 过渡** | **无** |
| C10 匿名设备 A vs B | scope 级，无端到端 |
| C11 匿名→登录→登出→匿名 | **无** |
| C12 同 WeChat 两个 ff_sid 跨设备一致 | 函数级 |
| C13 `/api/asset` 不存在 rel 返回 403 | **无** |

**根因**：每个消费 `scope` 的 handler（`SessionAssets` / `ServeAsset` / `ImageBatchHandler.{List,Get,Render}` / `RenderQueue` / `RepublishRender`）在 HTTP 层都**没有跨用户回归**。

## 5. 推荐修复路线（最小侵入）

1. **集中清理**：新增 `resetClientStateForAuthChange()` — 清空 `currentTask`/`submittedTasks`/`batchSubmitted`/`currentImageBatch`/`uploadedImages`/`scriptUploadedImages`/`sessionAssets`/`savedScriptContent`/`pendingScriptContent`/`selectedScript`/`QUEUE_JOBS`/`queueSubs`/`__ffCompId`，并 `URL.revokeObjectURL` 所有残留 blob。
2. **三处触发**：`doLogout()`（`index.html:941`）、`onSessionLost()`（`index.html:958`）、`/api/me` 探测到身份切换时（轮询里 `__ffUser` 与新用户 openid 不一致）。
3. **服务端正名**：`paintCurrentTask()` 改成读 `/api/render-queue` 服务端权威 jobId 校准，或加 `if (!window.__ffUser) { currentTask = null; hide; return; }` 守门。
4. **清理 timer / SSE**：`doLogout` / `onSessionLost` 中 `clearInterval` 三个 timer + 遍历 `queueSubs` 调用 unsubscriber + 关闭 SSE。
5. **Monaco**：`doLogout` 中 `ffMonaco && ffMonaco.setValue(DEFAULT_COMP); pendingScriptContent = null;`。
6. **回归测试**：补 `TestCrossScopeIsolation` 参数化测试（Alice↔Bob 在每个 handler 上）+ `TestLogout_AnonymousSessionHasNoAccessToWechatScopeBatches` + `TestSessionScope_LoginLogoutDoesNotPromoteAnonToWechat`。

---

> **产出文档**。后续 PR 描述、回归脚本与上线 checklist 应以此文档为入口。