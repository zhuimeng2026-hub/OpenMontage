# 帧流 FrameFlow — BFF（Go / Gin）

把前端 SPA 与 OpenMontage 的 Remotion MCP 服务连起来的**后端代理层（BFF）**。

浏览器**绝不直接**持有 `MCP_API_TOKEN`；所有 MCP 调用统一经本服务转发，
由它持有 token 并维护「同一用户 → 同一 MCP 会话」的亲和性。

## 为什么需要它

- `dw.aixifs.com/mcp` 每次响应都会**轮换 `Mcp-Session-Id`**，且服务端用该会话
  把 `upload_asset_chunk` 上传的图片与后续的 `create_remotion_video_share` 绑定。
  因此每个用户必须复用**同一个长驻 MCP 客户端**（带 mutex 串行），否则素材会
  落到不同会话、生成视频时找不到图。
- 微信 `code → access_token` 换票必须在服务端做（`appSecret` 不能进浏览器）。

## 目录结构

```
bff/
  main.go                 gin 引擎、路由、CORS、SPA 托管
  go.mod
  .env.example
  internal/
    config/config.go      配置（从 .env 读取）
    mcp/
      client.go           Streamable-HTTP MCP 客户端（initialize→tools/call，轮换 SID）
      session.go          每个用户一个长驻 MCP 客户端（亲和性）
  handlers/
    auth.go               会话 cookie / 用户存储
    mcp.go                POST /api/mcp
    progress.go           GET  /api/render-progress/:jobId（SSE 透传）
    wechat.go             微信 snsapi 授权 / 回调 / /api/me / /api/logout
```

## 运行

```bash
cd bff
cp .env.example .env      # 填写 MCP_API_TOKEN 与微信参数
go mod tidy               # 拉取 gin / godotenv
go run .                  # 默认 :8080
```

## 前端接线

推荐部署：**让本 BFF 同时托管前端 SPA**（把 `index.html` / `config.js` /
`mcp-client.js` 放到 `STATIC_DIR`，默认 `./web`），这样前端与 `/api` 同源，
会话 cookie 自动生效、几乎无 CORS 问题。

然后在前端 `config.js` 里把 `remotion.bffBaseUrl` 指向 BFF 地址（同源时即
`http://localhost:8080`），前端即自动从「演示骨架」切换到真实调用。

若 SPA 与 BFF 不同源，则设置 `FRONTEND_ORIGIN` 为前端地址，并保持
`config.js` 的 `bffBaseUrl` 指向 BFF。

## 接口契约（与 mcp-client.js 对齐）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/mcp` | 请求体 `{tool, args}`，返回该工具 extract 后的结构化结果 |
| GET  | `/api/render-progress/:jobId` | `text/event-stream`，透传上游渲染进度 |
| GET  | `/api/wechat/login` | 跳转微信授权页（参数为空返回 400） |
| GET  | `/api/wechat/callback` | 微信回调：换票 + 落地用户 + 跳回 `/?login=wechat` |
| GET  | `/api/me` | 返回当前登录用户 |
| POST | `/api/logout` | 退出 |

## nginx 注意

若本 BFF 前置 nginx，务必关闭 SSE 缓冲，否则进度条卡住：

```nginx
location /api/render-progress/ {
    proxy_pass http://127.0.0.1:8080;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;
    proxy_cache off;
    chunked_transfer_encoding on;
}
```

## 已知边界（骨架，非生产完备）

- 用户会话与 MCP 客户端映射为**进程内内存**，多实例部署需换 Redis 并持久化 MCP SID。
- 微信 `state` 已做一次性 CSRF cookie 校验；正式环境建议把 state 与会话绑定并加过期。
- 没有限流 / 鉴权中间件（除微信换票）；多人生产需在 `/api/mcp` 前加用户身份校验。
