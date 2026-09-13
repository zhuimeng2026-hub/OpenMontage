# frameflow 跨用户数据隔离修复计划

## Context

用户反馈：「本次提交的任务这里，显示的上传图片需要与不同的用户隔离，当前是不同的用户数据互相可见了。要修正到隔离不同用户的数据。」

在 frameflow 创建页，同浏览器标签页内 A 用户上传图片并提交渲染后退出登录，B 用户在同一标签页扫码登录并进入创建页，会看到 A 用户残留的"本次任务"卡片（含 A 的图片缩略图、jobId、脚本名、成片链接）。这一跨用户数据泄漏自 2026-08-19 上午的两次提交 `954871a`（引入 `currentTask`/`paintCurrentTask()`）和 `9ab14b0`（引入 5s 轮询）后开始出现，但根因是更早的 `doLogout()` / `onSessionLost()` 从未清理模块作用域的 per-user 状态。

完整审计已落盘到 `/opt/OpenMontage/docs/frameflow-user-isolation-audit-2026-08-19.md`（188 行）。结论：

- **后端 scope 隔离：结构性正确**，无需重构。
- **前端 SPA：6 个已确认 BUG（BUG1-6）**，根因单一：`__ffUser` 变更时不清理模块作用域状态。
- **触发提交**：`954871a` + `9ab14b0`。
- **测试覆盖**：仅函数级；**零 HTTP 级跨用户回归**。

目标：让用户在任意身份切换（同 tab 登出/扫码、同 tab 匿名→登录、跨 tab 不同 ff_sid）后，新视图只看到自己的数据；同 WeChat 用户的"切页不丢"语义不破坏。

## 方案概览

一个 PR 落地，三块改动：

| 改动 | 文件 | 行数 |
|------|------|------|
| SPA 端：新增集中 reset + 单一 setter + paintCurrentTask 守门 + create-view 反查 | `frameflow/bff/web/index.html` | +92 / -6 |
| 后端加固 S2：`/api/_dev_login` 仅在 `DevLoginAllowed` 时注册 | `frameflow/bff/main.go` | +2 / -1 |
| 后端加固 S3：`loadUserMap` 缓存写入后再次 `isExpired` 校期 | `frameflow/bff/handlers/auth.go` | +4 |
| 新增 HTTP 级跨用户回归测试 | `frameflow/bff/handlers/scope_cross_test.go` (新文件) | +280 |

**两个延后**：

- **S4** 写路由 `RequireAuth` 化（route group refactor ~35 行）— 单独 PR 2，独立审阅更安全；不阻塞当前用户报告的 bug。
- **S1** `QrLoginStatus` 票据绑定 `ff_sid` — 需要 `wechat_qr_tickets` 表加列 + DB 迁移 — 单独 PR 4 延后。

## A. SPA 端修复（实际根因）

### A1. 新增 `resetClientStateForAuthChange()`

定义位置：`frameflow/bff/web/index.html` 第 966 行（`onSessionLost` 之后、头像下拉 IIFE 之前）。

职责（按顺序执行）：

1. 复用既有 `clearUploadedImages()` (`:2043`) 与 `clearScriptUploadedImages()` (`:2501`)，后者当前是死代码，本 PR 顺手激活。
2. 清空模块作用域 per-user 变量：`submittedTasks`、`currentTask`、`batchSubmitted`、`currentImageBatch`、`lastBatchId`、`uploadedCount`、`currentProgressJobId`、`sessionAssets`、`savedScriptContent`、`pendingScriptContent`、`selectedScript`、`QUEUE_JOBS`。
3. 遍历 `queueSubs` 调用每个 unsubscriber，再清空 map。
4. 关闭三个计时器：`queueTimer`、`createTasksTimer`、`window.__ffSessionCheckTimer`（见 A3 的 setInterval 改造）。
5. 关闭 QR 轮询：`window.__qrPoll`。
6. UI 立即隐藏：`current-task-card`、`create-progress`、`script-progress`、`previewBox`。
7. Monaco：`ffMonaco.setValue(DEFAULT_COMP)`（`DEFAULT_COMP` 已 module-scope 声明于 `:2319`），清 `pendingScriptContent`。
8. 清 `window.__ffCompId`。

幂等。约 50 行。

### A2. 新增 `setCurrentUser(user|null)`

位置：紧邻 A1 之后。

- `prevOpenid === nextOpenid` → 仅刷新视图（不开 reset，保留"切页不丢"）。
- `prevOpenid !== null && nextOpenid === null`（登出）→ 先 `resetClientStateForAuthChange()` 再 `__ffUser = null`。
- `prevOpenid === null && nextOpenid !== null`（新登录）→ 直接 `__ffUser = user`，启动 `setInterval(checkSessionAlive, 60000)` 到 `window.__ffSessionCheckTimer`。
- `prevOpenid !== null && nextOpenid !== null && prev !== next`（同 tab 换账号）→ 先 reset 再 set。

接入5 个 `__ffUser` 写入点：

| 调用点 | 行号 | 当前写法 | 改为 |
|--------|------|----------|------|
| `doLogout` | 941-952 | `window.__ffUser = null` | `setCurrentUser(null)` |
| `onSessionLost` | 958-966 | `window.__ffUser = null` | `setCurrentUser(null)` |
| `pollQrStatus` 成功分支 | 1041-1067 | `window.__ffUser = u.user` | `setCurrentUser(u.user)` |
| 微信 OAuth 回调 | 1087-1103 | `window.__ffUser = u.user` | `setCurrentUser(u.user)` |
| 页面加载会话恢复 | 1106-1117 | `window.__ffUser = u.user` | `setCurrentUser(u.user)` |

约 22 行 setter + 5 个 1 行调用点修改。

### A3. 改造 `setInterval(checkSessionAlive, 60000)` 为可清除

`frameflow/bff/web/index.html:1004` 当前直接 `setInterval(checkSessionAlive, 60000)` 无赋值，**全程无法清除**。改为：

```js
window.__ffSessionCheckTimer = setInterval(checkSessionAlive, 60000);
```

+2 行（setInterval 改造）-1（去掉原裸调用）。

### A4. `paintCurrentTask()` 守门 + 服务端反查

**(a) 守门**：在 `frameflow/bff/web/index.html:2275` `paintCurrentTask()` 顶部，把

```js
if (!currentTask){ card.style.display = 'none'; return; }
```

改为

```js
if (!window.__ffUser || !currentTask){ card.style.display = 'none'; return; }
```

（+1 行）。

**(b) 服务端反查**：新增 `async function reconcileCurrentTask()`：用 `/api/render-queue` 取当前用户服务端作业列表，若 `currentTask.jobId` 不在其中则 `currentTask = null; submittedTasks = []; paintCurrentTask();`。这是兜底 —— A1/A2/A3 已覆盖正常路径，此处仅防回归。在 `frameflow/bff/web/index.html:1142` 创建页入口改为：

```js
paintCurrentTask();
reconcileCurrentTask();   // ← 新增
```

约 +12 行。

## B. 后端加固（顺带，必做）

### B1. S2 — `main.go:100` 路由注册门控

把

```go
api.GET("/_dev_login", h.DevLogin)
```

改为

```go
if h.Cfg.DevLoginAllowed {
    api.GET("/_dev_login", h.DevLogin)
}
```

避免误配 `DEV_LOGIN_ALLOWED=true` 到生产时暴露公共模拟登录入口。+2/-1。

### B2. S3 — `handlers/auth.go:147-165` `loadUserMap` 缓存写后再校期

在 `userStore.m[sid] = u` 写入后、`return u` 之前加：

```go
if isExpired(u) {
    dropUserMap(sid)
    return nil
}
```

防止读 DB 后、写入缓存前 `expires_at` 已过的窗口把过期用户当成在线。+4 行。

## C. 回归测试（新文件）

新文件：`frameflow/bff/handlers/scope_cross_test.go` (~280 行)

复用现有模式（`auth_test.go:17-28` 的 `httptest` 套路 + `scope_test.go:13-41` 的双 scope fixture + `state.Open(t.TempDir() + …)` 的 SQLite 临时文件），新增一个共享 helper `newCrossScopeRouter(t)` 集中搭建：临时 SQLite、inert `mcp.NewSessionStore("http://127.0.0.1:1", "", db)`、`Handlers` + `ImageBatchHandler` + `CompositionHandler` + `ScriptHandler` 全装好，挂到 `/api` group。然后 `callAs(r, method, path, sid)` 一行注入 `ff_sid` cookie。

测试清单（每个 10-20 行）：

| 函数 | 覆盖 |
|------|------|
| `TestSessionAssets_OtherWechatUserGetsEmptyList` | A1 |
| `TestServeAsset_DisallowedRelReturnsForbidden` | A2 |
| `TestImageBatch_OtherWechatUserCannotList` | A3 |
| `TestImageBatch_Get_ScopedToOwner` | A3 |
| `TestImageBatch_Render_ScopesUpstream` | A3 |
| `TestImageBatchRender_JobNotVisibleToAnotherWechatUser` | A4 |
| `TestRenderQueue_ShareURLIsolatedBetweenWechatUsers` | A5 |
| `TestSessionScope_LoginLogoutDoesNotPromoteAnonToWechat` | B8 + B11 |
| `TestLogout_AnonymousSessionHasNoAccessToWechatScopeBatches` | B9 |
| `TestCrossScopeIsolation` | 表驱动，遍历所有读端点 Alice↔Bob |
| `TestRenderProgress_EmptySIDReturns404` | 锁定 S5 |
| `TestAuthLoadUserMapRecheckExpiryAfterCacheWrite` | 锁定 S3 |

不建独立 `imagebatch/*_test.go`、`composition/*_test.go` 文件 —— 跨 scope 场景统一集中在 `scope_cross_test.go`，便于回归时一次跑全。

## D. 验证计划

### D1. 后端构建与测试

```bash
cd /opt/OpenMontage/frameflow/bff
go build -o frameflow-bff .
go vet ./...
go test ./...
go test -run CrossScope ./handlers -v
go test -run RenderProgress ./handlers -v
```

期望：`go test ./...` 全绿，新增 12 个测试全过。

### D2. SPA 手工回归（同 tab 跨用户）

`./dev.sh` 启动 BFF，浏览器单标签内：

1. 访问 `/api/_dev_login?as=alice` 登录 Alice → 上传 5 张图 → 提交渲染 → 确认顶部卡片渲染。
2. 切到其它页签再回创建页 → 卡片仍在（"切页不丢"语义保留）。
3. 头像下拉 → 退出登录 → **本次任务卡片立即消失**；"当前会话已上传图片"面板清空。
4. `/api/_dev_login?as=bob` 登录 Bob → 进创建页 → **不出现 Alice 的卡片 / 不出现 Alice 的图片缩略图**；"我的渲染任务"为空。
5. Bob 上传 3 张图 → 切到脚本模式 → 打开 Monaco → 输入代码 → 切走 → 切回 → 再次通过 `/api/_dev_login?as=alice` 切回 Alice → 进创建页 → Monaco 应为 `DEFAULT_COMP`，非 Bob 输入。

### D3. 跨 tab 同 WeChat 用户回归

1. Tab 1: dev-login Alice (`ff_sid-A`)。
2. Tab 2: dev-login Alice (`ff_sid-B`)。
3. Tab 1 上传 5 张图并提交，确认本 tab 卡片显示。
4. Tab 2 进创建页，"我的渲染任务"列里出现同一 job（**服务端按 openid scope 共享**）；本 tab 自己的 currentTask 卡片为空（"切页不丢"是 per-tab 的，未改）。

### D4. SPA 无 JS 测试框架

`frameflow/bff/` 下无 jest/vitest/playwright 配置（仅 `package.json` 出现在 demo/remotion-composer/hyperframes-demo），SPA 自动化测试留作后续工作，本 PR 不阻塞。

## E. 关键文件

- 修改：`frameflow/bff/web/index.html`
- 修改：`frameflow/bff/main.go`
- 修改：`frameflow/bff/handlers/auth.go`
- 新增：`frameflow/bff/handlers/scope_cross_test.go`

**复用既有**：

- `clearUploadedImages()`（`index.html:2043`）
- `clearScriptUploadedImages()`（`index.html:2501`，当前死代码，本 PR 激活）
- `DEFAULT_COMP`（`index.html:2319`）
- `renderQueueOwnerID()`（`handlers/auth.go:98`）
- `loadUserMap` / `dropUserMap` / `isExpired`（`handlers/auth.go:133,189,245`）
- `mcp.NewSessionStore("http://127.0.0.1:1", "", db)`（既有测试套路）
- `state.Open(filepath.Join(t.TempDir(), "x.db"))`（既有测试套路）
- `h.saveUser(sid, map[string]interface{}{"openid": …})`（`scope_test.go:13-41` 双 scope fixture）
- `gin.SetMode(gin.TestMode); gin.New(); httptest.NewRequest(...); httptest.NewRecorder(); r.ServeHTTP(...)`（`auth_test.go:17-28`）
- `randHex(16)`（`handlers/auth.go:87`）

## F. 风险与回滚

- **SPA 改动集中在 `index.html`**：约 100 行净增，分布在第 941-1142 行与第 2275-2305 行。回滚只需 revert 一个 commit。
- **后端 `loadUserMap` 加 4 行**：纯加法，不修改现有路径，回滚无副作用。
- **新测试**：纯增量，不影响生产路径。
- **未触动的关键文件**：`session_assets.go`（仅作 ServeAsset scoping 不变量参考）、`image_batch.go`（既有 `Batches.Get(scope, id)` 已正确 scope）、`wechat.go`（`Logout` 已正确清 cookie+持久化行）、`internal/imagebatch/store.go`、`internal/mcp/session.go`（均已按 `session_id` 过滤）。