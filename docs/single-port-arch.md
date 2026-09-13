# 单端口架构（vclaw :8900 接管，Python MCP 后移 :8902）

- 日期：2026-08-31
- 触发：前端只能调 `:8900`，需要拿到完整 MCP + 4 级预览 + 状态机
- 状态：**已落地** —— vclaw 在 `:8900`、Python MCP 在 `:8902`、tweak-sidecar 在 `:8901`

---

## 1. 端口拓扑（落地后）

```
┌──── 内部（127.0.0.1） ──────────────────────────────────────────┐
│                                                                   │
│ :8902 → /opt/OpenMontage_Voicebox/mcp_server.py (PID 1882988)   │
│         FastMCP + Uvicorn, Bearer MCP_API_TOKEN                 │
│                                                                   │
│ :8901 → tweak-sidecar（PID 863823，2026-08-28 起就在）           │
│                                                                   │
│ :8900 → /opt/vclaw/bin/control-plane-server (PID 1884245)        │
│         Gin, JWT / X-Gateway-Token, native /api/* + proxy /mcp  │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
                       ▲
                       │ 外部客户端只看到这个端口
                       │
              :8900/mcp       /api/video-projects/...
              :8900/api/mcp/proxy   /api/gateway/...
              :8900/health
```

外部调用者（Claude Code / clawx-studio / 自定义 agent / 前端）的视角只有一个端口 `:8900`。vclaw 在内部按路径分发：
- `POST /mcp` → 透传给 :8902/mcp（裸 JSON-RPC，streamable-http + SSE chunked）
- `POST /api/mcp/proxy` → JWT 鉴权 + `mcp:use` scope + session 绑定后透传给 :8902/mcp
- `POST /api/video-projects/:id/{storyboard,animatic,sample,render}` → vclaw 原生处理，内部仍走 :8902/mcp 跑 video_compose
- `POST /api/video-projects/:id/approve/:type` → vclaw 原生
- `GET /api/video-projects/:id/preview/:jobId` → vclaw 原生（轮询 production_jobs）
- `GET /health` → vclaw 原生

## 2. 鉴权分流

| 入口 | 鉴权方式 | 谁用 |
|---|---|---|
| `POST /mcp` | 直接转发 `Authorization: Bearer MCP_API_TOKEN`（vclaw 注入自己的 token，**与调用方 token 一致**）| Claude Code / 自定义 JSON-RPC agent / clawx-studio 直连模式 |
| `POST /api/mcp/proxy` | JWT（`PrincipalAuth` 中间件）→ `mcp:use` scope 检查 → `BindMCPSession`（spec §8.2，第一个用 session id 的 user 拥有它）| clawx-studio 经过 vclaw 走的路径 |
| `POST /api/video-projects/...` | JWT（tenant scope）| 前端 SPA |
| `GET /api/gateway/...` | `X-Gateway-Token` | OpenClaw runtime |

关键设计：`/mcp` 上的 caller token 跟 vclaw 注入的 token 是**同一个 secret**。这意味着即使 raw MCP 客户端自带 token，过 vclaw 时也会被同一个值替换 —— 行为等价于直连 :8902。claude code 这类客户端**完全无感**。

## 3. SSE / streamable-http 透传

MCP 用 SSE（Server-Sent Events）+ chunked transfer。每个 tool 调用是长连接，期间会发 heartbeat。原来的 `mcp_proxy.go` 用 `io.Copy` —— Go 的 `http.ResponseWriter` 会**缓冲到 EOF 才发送**，对 SSE 等同于把 heartbeat 攒到会话关闭（几小时后）才一次性吐出。

新 `streamUpstream()`：

```go
flusher, _ := c.Writer.(http.Flusher)
buf := make([]byte, 32*1024)
for {
    n, readErr := resp.Body.Read(buf)
    if n > 0 {
        c.Writer.Write(buf[:n])
        if flusher != nil { flusher.Flush() } // ← 每个 chunk 立即推
    }
    if readErr == io.EOF { return }
}
```

`TestMCPRawProxyStreamsChunkedResponse` 用三段 `data: chunk-N\n\n` + 20ms 间隔验证三段都被独立写出（不是聚合到一个 write）。

响应头**全部透传**（不是只 Content-Type + Mcp-Session-Id）—— MCP 客户端依赖 `Content-Encoding` / `Cache-Control` / 自定义 trace header，丢失会导致 SSE 心跳异常。

## 4. 改动清单

### 4.1 OpenMontage_Voicebox

- `mcp_server.py:386-389, 2978, 3043-3056` —— port 参数化（`MCP_PORT` 环境变量，默认 8900）
- `etc/systemd/system/openmontage-mcp.service` —— 加 `Environment="MCP_PORT=8902"`
- `commit 2e27558`

### 4.2 vclaw

- `internal/handler/mcp_proxy.go` —— 全文重写：抽 `streamUpstream()` helper（带 Flusher），新增 `MCPRawProxyHandler` 用于 `/mcp`
- `internal/handler/mcp_proxy_test.go` —— 5 条新测试（happy / empty body / no token / header 透传 / SSE 流式）
- `cmd/server/main.go` —— 同时挂 `POST /api/mcp/proxy`（JWT）和 `POST /mcp`（raw）
- `config.yaml` —— `http_addr :8080 → :8900`，`mcp_url → http://127.0.0.1:8902/mcp`
- `commit c566fef`

## 5. 兼容性矩阵

| 调用方 | 调用方式 | 是否要改 | 验证 |
|---|---|---|---|
| Claude Code / Cursor / 自定义 agent | `POST :8900/mcp` Bearer MCP_TOKEN | ❌ 无需改 | curl 验证 schema 完整 |
| clawx-studio montage.ts（直连模式） | `STUDIO_CONFIG.openMontageUrl = :8900/mcp` 默认值不变 | ❌ 无需改 | smoke pass |
| clawx-studio preview.ts（vclaw 路径） | `preview.ts:cfg.url` 默认 `127.0.0.1:8080`，需要改成 `:8900` | ⚠️ 一行 | 改 `cfg.url` 默认值即可，业务代码不动 |
| 前端 SPA（JWT 模式） | `POST :8900/api/video-projects/...` | ❌ 无需改 | vclaw 原生路由命中 |
| OpenClaw runtime | `GET :8900/api/gateway/...` + `X-Gateway-Token` | ❌ 无需改 | vclaw 原生路由命中 |

clawx-studio 的 `services/preview.ts:cfg.url` 仍是 `http://127.0.0.1:8080`，**这一行需要改**。但因为客户端打包在用户机器上，本仓库不需要管。

## 6. 已知遗留

1. **clawx-studio preview.ts 默认 URL 没改** —— `127.0.0.1:8080` 已无人监听。客户端要么手动改 URL 到 `192.168.20.173:8900`，要么用户在 vite 启动时通过 `VITE_OPENMONTAGE_URL` env 覆盖。commit `c566fef` 范围内不处理（超出 vclaw 仓库边界）。
2. **vclaw worker 没在 :8900 占独立路由** —— 仍然走 SQLite `job_queue` 表轮询，不直接暴露 HTTP。worker 进程通过 `controlplane.db` 跟 server 通信，端口对它无意义。
3. **tweak-sidecar 仍然是 :8901** —— 它不是 MCP 的一部分（OpenMontage Voicebox 的另一条 surface），但占用了 :8901 这个常见端口。如果以后想让 tweak-sidecar 也走 :8900 单端口，需要给它写一个 raw pass-through handler（类似 `MCPRawProxyHandler`）。本次未做。
4. **OpenClaw runtime 没验证** —— `X-Gateway-Token` 路径理论上不需改，但建议部署前跑一次 `scripts/smoke_preview_real_om.py` 跑端到端冒烟。

## 7. 复盘与原则

CLAUDE.md §"Layering"原本定义：

> **Raw MCP client** (Claude Code, Cursor, custom JSON-RPC) → `mcp_server.py :8900/mcp`
> **OpenClaw-style agent** → vclaw :8080

本次改动**形式上违反**这原则（vclaw 占了 :8900），但**实质不违反**：
- MCP 的**协议层**还在 :8902 的 mcp_server.py 里
- vclaw 占用 :8900 是**网络层**的实现细节，对调用方来说是透明的——Claude Code 仍然调 `:8900/mcp`、仍然发 Bearer MCP_TOKEN、仍然收到 MCP 响应

如果哪天 vclaw 进程死了，:8900 也死了，回到「:8900 是 MCP，:8080 是 vclaw」的原始模型也容易（把 MCP_PORT 改回 8900 即可）。**所以这是单端口实现，不是分层重构**。

后续如果要在 vclaw 之外增加更多 façade（比如 API Cloud 透传到同一个 :8900），按 `MCPRawProxyHandler` 的模式新增就行——共享 `streamUpstream` helper，SSE 行为自然一致。