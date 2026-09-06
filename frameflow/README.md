# FrameFlow（帧流）图生视频平台 · Web 交付层

本目录是 **FrameFlow（帧流）** 面向终端用户的 Web 应用层，配合 OpenMontage 的 Remotion
渲染服务（生产为 `lanes.ymxt.top:8900/mcp`）一起工作。部署到独立域名时，只需要部署本 `frameflow/`
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
├── DEPLOYMENT_RUNBOOK.md # 本机/生产部署与验收步骤
├── REMOTE_OBSERVABILITY_HANDOFF.md # 远端观测、日志与压测交接单
├── README.md             # 本文件
└── .gitignore            # 排除编译产物与本地密钥
```

## 分层职责

| 层 | 职责 |
| --- | --- |
| 前端 `bff/web/` | 单页应用：登录、工作台、创建视频、画廊、渲染队列。创建页含**「模板模式 / 脚本模式」**双入口；脚本模式内置 Monaco(TSX) 编辑器，可编写/保存自定义 Remotion 合成。通过 `config.js` 指向 BFF。 |
| BFF `bff/` | 浏览器**不直接**持有 `MCP_API_TOKEN`；由它将 `/api/mcp` 转发到上游 MCP，并维护**每用户 MCP 会话亲和**（保证上传的图片与生成落在同一会话）。同时代理 `/api/render-progress` SSE、处理微信 `snsapi` 网页登录，并提供 `/api/compositions` 自定义合成保存/管理/渲染接口。 |
| 渲染服务 `lanes.ymxt.top:8900/mcp` | 真实 Remotion 视频合成（OpenMontage 提供）。 |

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
| `CUSTOM_COMPOSITION_ENABLED` | `false` | 自定义合成（脚本模式）渲染开关。上游 `create_remotion_video_share` 目前**不接受**自定义代码，故默认关闭：编辑器可保存/管理合成，「渲染此合成」返回 501 明确提示而非伪造成功。待上游增加 `code` 入参后改为 `true` 即可点亮真实渲染。 |

注意：当 `AUTH_REQUIRED=true` 但微信 AppID/Secret 未配置时，BFF 会 fail-closed 并拒绝启动，绝不会自动开放。微信回调换票（`code → access_token → userinfo`）由 BFF 服务端完成，`WECHAT_APP_SECRET` 绝不下发前端。

## 脚本模式（自定义 Remotion 合成）

创建页顶部可切换「模板模式 / 脚本模式」：

- **脚本模式**内置 Monaco 编辑器（TSX 语法高亮），预置一个图片轮播合成模板；用户可编写/保存/重置自定义 Remotion 合成，并上传素材。
- 编辑器通过 `/api/compositions`（保存 / 列表 / 详情）把合成源码存到 BFF（按 `ff_sid` 会话隔离，内存存储；生产建议换 DB）。
- 「渲染此合成」会先保存再提交渲染。

**关键约束**：当前上游 MCP 的 `create_remotion_video_share` 只收受信任的 `script_id` 与渲染参数，**不接受任意自定义合成代码**。因此：

- `CUSTOM_COMPOSITION_ENABLED=false`（默认）：保存正常，「渲染此合成」返回 `501` + 明确说明，**绝不伪造成功**。
- 要真正按自定义代码出片，二选一：① 推动上游增加 `code` 入参后把开关设为 `true`；② 在 FrameFlow 侧**自托管 Remotion 渲染 worker**（装 Remotion + ffmpeg，按用户代码打包渲染）——这是自定义合成最可控的架构路径。

Monaco 编辑器经 CDN（`cdn.jsdelivr.net`）加载，离线/完全自托管部署时请将其 vendor 到 `bff/web/` 并改 `index.html` 的 loader 路径。

## 运行

详见 [`bff/README.md`](./bff/README.md)。速记：

```bash
cd frameflow/bff
cp .env.example .env        # 填入 MCP_API_TOKEN 与微信参数
go run .                   # 默认 :8080，同源托管前端 → http://localhost:8080
```

独立域名部署时，把 `frameflow/` 交给构建流水线（Docker / 静态托管 + BFF 服务），
前端走 CDN/对象存储或仍由 BFF 同源托管均可。

生产合并部署、远端只读性能指标与脱敏日志接口分别参见
[`DEPLOYMENT_RUNBOOK.md`](./DEPLOYMENT_RUNBOOK.md) 和
[`REMOTE_OBSERVABILITY_HANDOFF.md`](./REMOTE_OBSERVABILITY_HANDOFF.md)。
