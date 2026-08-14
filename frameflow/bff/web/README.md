# 帧流 FrameFlow · 前端 SPA（`bff/web/`）

本目录是 FrameFlow 的**纯静态单页应用（SPA）**，由 Go BFF 同源托管，零构建、零框架。

> 架构铁律：浏览器**绝不**直接持有 `MCP_API_TOKEN` / 微信 `appSecret` 等敏感凭证。
> 所有后端调用统一经 `bffBaseUrl` 指向的 BFF 转发，由 BFF 持有 token 并维护 MCP 会话。

## 文件清单

| 文件 | 作用 |
| --- | --- |
| `index.html` | 单文件 SPA：登录 / 工作台 / 创建视频 / 画廊 / 渲染队列；HTML + 内联 CSS + 内联 JS 全部在此（含 favicon 与社交分享 meta）。 |
| `config.js` | 前端运行配置，挂载到 `window.FF_CONFIG`（微信登录参数 + Remotion/MCP 连接参数）。 |
| `mcp-client.js` | 浏览器侧 MCP 调用骨架，挂载到 `window.FFMCP`；统一走 BFF，并实现「演示模式」降级。 |

## 配置契约（`config.js` → `window.FF_CONFIG`）

```js
window.FF_CONFIG = {
  wechat: { appId, appSecret, redirectUri, scope, state, token, encodingAESKey }, // 仅 appId 下发前端，其余服务端专用
  remotion: {
    mcpUrl:      "https://dw.aixifs.com/mcp",          // 真实 MCP 端点（仅展示/参考，前端不直接调用）
    progressUrl: "https://dw.aixifs.com/render-progress", // SSE 进度（同样经 BFF 代理）
    bffBaseUrl:  "http://localhost:8080"               // 前端统一请求的 BFF 地址；留空即进入演示模式
  }
};
```

- `bffBaseUrl` 为**空** → `mcp-client.js` 进入 `DEMO` 模式：本地模拟上传与渲染进度，不发任何网络请求，仅用于评审交互。
- `bffBaseUrl` 非**空** → 走真实调用：`POST {bffBaseUrl}/api/mcp`、`GET {bffBaseUrl}/api/render-progress/{jobId}`（SSE）。

## 本地真实测试（非演示）

真实后台就是 `https://dw.aixifs.com/mcp`，但它由 **BFF 服务端**通过 `MCP_BASE_URL` 持有，
前端只跟本地 BFF 对话。要跑通端到端真实调用：

```bash
# 1) 准备服务端凭证（含 MCP_API_TOKEN）
cd frameflow/bff
cp .env.example .env
#   编辑 .env：填 MCP_API_URL/Token（默认 MCP_BASE_URL 已是 https://dw.aixifs.com/mcp）、按需填微信参数
#   上线前把 AUTH_REQUIRED 改为 true

# 2) 启动 BFF（它同时托管 ./web 下的 SPA，STATIC_DIR 默认 ./web）
go run .            # 监听 :8080，静态目录即本目录

# 3) 打开前端
#   浏览器访问 http://localhost:8080
#   - config.js 的 bffBaseUrl=http://localhost:8080，与 BFF 同源 → 非演示模式
#   - 上传图片 → BFF 代理 upload_asset_chunk 到真实 MCP → create_remotion_video_share → SSE 拉进度
```

> 若 `bffBaseUrl` 指向与 BFF **不同源**的地址（如独立静态服务器 `http://localhost:5173`），
> 需在 BFF 的 `FRONTEND_ORIGIN` 里登记该源以放行 CORS（`main.go` 的 `corsMiddleware`）。

## 独立前端 dev server（可选，不依赖 Go）

仅做纯前端联调、且接受演示模式时，可用任意静态服务器托管本目录并代理 `/api`：

```bash
npx serve .            # 或 python -m http.server 8081
# 需要真实调用时，把 config.js 的 bffBaseUrl 指向正在运行的 BFF（默认 :8080）
```

## 注意事项

- **敏感凭证不下发**：`config.js` 里的微信 `appSecret` / `token` / `encodingAESKey` 应始终留空，
  正式环境只存在于服务端 `.env`。当前文件为占位模板（`localhost` 默认值），可入库。
- **Monaco 走 CDN**：脚本模式编辑器从 `cdn.jsdelivr.net` 加载。离线 / 完全自托管部署时，
  需把 monaco-editor vendor 到本目录并把 `index.html` 的 loader 路径改为本地。
- **单文件结构**：`index.html` 内联了全部样式与脚本，便于零构建分发，但不利于单测与组件化拆分。
