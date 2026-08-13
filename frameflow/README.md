# FrameFlow（帧流）图生视频平台 · Web 交付层

本目录是 **FrameFlow（帧流）** 面向终端用户的 Web 应用层，配合 OpenMontage 的 Remotion
渲染服务（`dw.aixifs.com/mcp`）一起工作。部署到独立域名时，只需要部署本 `frameflow/`
目录即可，与 OpenMontage 的渲染内核、内部 MCP 代理（`OpenMontage-mcp-proxy`）互不干扰。

```
frameflow/
├── bff/                  # Go/Gin 后端（Backend-for-Frontend）
│   ├── main.go           # 入口：路由、CORS、同源托管前端 SPA
│   ├── internal/         # config（配置）、mcp（MCP 客户端 + 会话亲和）
│   ├── handlers/         # auth / mcp / progress(SSE) / wechat
│   ├── web/              # 前端静态资源（index.html / config.js / mcp-client.js）
│   ├── .env.example      # 配置模板（真实 .env 含密钥，勿入库）
│   └── README.md         # BFF 运行与部署细节
├── README.md             # 本文件
└── .gitignore            # 排除编译产物与本地密钥
```

## 分层职责

| 层 | 职责 |
| --- | --- |
| 前端 `bff/web/` | 单页应用：登录、工作台、创建视频、画廊、渲染队列。通过 `config.js` 指向 BFF。 |
| BFF `bff/` | 浏览器**不直接**持有 `MCP_API_TOKEN`；由它将 `/api/mcp` 转发到上游 MCP，并维护**每用户 MCP 会话亲和**（保证上传的图片与生成落在同一会话）。同时代理 `/api/render-progress` SSE、处理微信 `snsapi` 网页登录。 |
| 渲染服务 `dw.aixifs.com/mcp` | 真实 Remotion 视频合成（OpenMontage 提供）。 |

## 协议要点（已与上游实测对齐）

- **分块上传**：前端按 400KB 二进制切片 → base64 + SHA-256 → `upload_asset_chunk`
  （`start → append* → complete`）。单块远低于 nginx 默认 `client_max_body_size 1m`，
  走分块根本不触发 413；50m 仅为老的单次 `upload_asset` 路径兜底。
- **会话亲和**：BFF 每用户一个长驻 MCP 客户端并串行化，轮换 `Mcp-Session-Id` 回带。
- **SSE**：`GET /api/render-progress/{jobId}` 透传上游，nginx 前置时需 `proxy_buffering off`。

## 安全配置（上线前必读）

BFF 已内置两道护栏，默认**仅在配置后生效**，避免误伤本地联调：

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `AUTH_REQUIRED` | `false` | 设为 `true` 后，`/api/mcp` 与 `/api/render-progress` 必须携带已登录的微信会话（`ff_sid` cookie），否则返回 401。**上线前必须设 `true`。** |
| `RATE_LIMIT_PER_MIN` | `30` | 令牌桶限流，按会话（无会话时按 IP）对每个 `/api/*` 请求限速，超额返回 429（带 `Retry-After`）。 |

注意：当 `AUTH_REQUIRED=true` 但 `WECHAT_APP_ID` 仍未配置时，没有可登录的 IdP，BFF 会**自动降级为开放**并打印启动告警——所以「开启鉴权」的前提是先填好微信服务号参数。微信回调换票（`code → access_token → userinfo`）由 BFF 服务端完成，`WECHAT_APP_SECRET` 绝不下发前端。

## 运行

详见 [`bff/README.md`](./bff/README.md)。速记：

```bash
cd frameflow/bff
cp .env.example .env        # 填入 MCP_API_TOKEN 与微信参数
go run .                   # 默认 :8080，同源托管前端 → http://localhost:8080
```

独立域名部署时，把 `frameflow/` 交给构建流水线（Docker / 静态托管 + BFF 服务），
前端走 CDN/对象存储或仍由 BFF 同源托管均可。
