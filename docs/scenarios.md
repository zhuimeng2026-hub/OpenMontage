# 双场景实现方案：OpenClaw × OpenMontage × Voicebox

> 日期：2026-08-19 · 状态：分析完毕，待选型
>
> 本文档在并行子代理（OpenClaw 网关审计 / FrameFlow BFF 协议审计 / 网络拓扑审计 / Voicebox 桥接审计）的基础上，给出两个具体场景的端到端实现。

---

## 0. 业务目标回顾

用 OpenClaw（或任意外部代理）调用 Voicebox 克隆声音，把克隆语音嵌入 Remotion 视频里渲染。两个系统的 MCP 端点：

| 系统 | MCP URL（内网） | 鉴权 |
|------|----------------|------|
| Voicebox | `http://127.0.0.1:17493/mcp` | `X-Voicebox-Client-Id`（身份标识，非凭据）|
| OpenMontage | `http://127.0.0.1:8900/mcp` | `Authorization: Bearer $MCP_API_TOKEN` |

外部 OpenClaw 客户端要与这两个端点建立 MCP 连接，必须先解决网络可达 + 鉴权中转。

---

## 1. 网络拓扑现状（先认清地基）

子代理 C 实测结果：

| 项 | 现状 |
|----|------|
| 本机公网 IPv4 | `113.67.8.67`（中国电信深圳，AS4134）|
| 本机公网 IPv6 | **无**（仅 `fe80::/64` link-local）|
| 入站 TCP | **全部被运营商封堵**（80/443/18789/8900 等宽端口全超时，ICMP 通）|
| LAN 内可达 | 仅 `192.168.20.173/24`（OpenWrt 路由器 LAN 侧）|
| `render.mengxa.com` | DNS → `1.14.182.208`（腾讯云广州 VPS），**不是本机** |
| 用户已拥有的公网 VPS | ① `1.14.182.208`（FrameFlow BFF 已部署）；② `8.134.147.195`（FRP server `7000`）|
| 现存隧道工具 | `frpc`（配置已改名 `frpc.toml-20250407`、unit `failed`）；`ddns-go`（指向不存在的 `ppp0`）；无 `cloudflared`/`tailscaled`/`bore`/`ngrok`/`wg` |
| 局域网网关 | OpenWrt LuCI（用户自管），未配 DNAT |

**结论**：本机**没有任何**外部可入站的网络路径；唯一现成的公网入口是 `render.mengxa.com`（在用户拥有的腾讯云 VPS 上）。

---

## 2. 鉴权与中转现状

子代理 A/B/D 共同确认的事实：

| 维度 | 现状 |
|------|------|
| FrameFlow BFF（`1.14.182.208`） | **仅** 微信 OAuth + `ff_sid` cookie；`AUTH_REQUIRED=true` 时 CLI 调用者 100% 401；无任何 Bearer/API key reader |
| BFF 与 OpenMontage 的会话亲和 | `SessionStore` 按 owner (`wechat:<openid>` 或 `session:<sid>`) pin 住 `*Client`，因为上游每响应轮换 `Mcp-Session-Id` |
| `MCP_API_TOKEN` | 单 token，环境变量加载，**无 rotation**，重启 BFF 才生效 |
| BFF 对 Voicebox 感知 | **零**（grep 全部 0 命中）|
| Voicebox `X-Voicebox-Client-Id` | 是身份标识（任何调用者可自填），**不是凭据**；无 rate limit / quota / 用户级 ACL |
| Voicebox 音频尺寸 | `MAX_TRANSCRIBE_BYTES=200MB` 已声明，但 Starlette 默认 `client_max_size=16MB`，公网 200MB 上传会被框架层先拒 |
| OpenClaw 网关（`18789`/`18791`） | 网关本身**不代理 MCP over HTTP**；内置 MCP 是 stdio serve（一次性）、in-process client（嵌入式 Pi agent）；远程暴露只有 Tailscale |

---

## 场景一：IPv6 线路 → 直连 MCP

### 1.1 适用前提

外部客户端有可达的 IPv6 出口；本机获得可路由的 IPv6 地址。

### 1.2 本机获得 IPv6 的三条路径（按成本/可靠性排序）

| # | 方案 | 部署成本 | 可靠性 | 备注 |
|---|------|---------|--------|------|
| A | **Cloudflare Tunnel（cloudflared）** | 低（单二进制 + systemd unit + 服务令牌）| 高（Cloudflare SLA）| 给一个 `*.trycloudflare.com` 或自有域；IPv4+IPv6 双栈可达；无需在本机开入站 |
| B | **Tailscale Funnel** | 低（`tailscaled` + `tailscale up` + `tailscale funnel 17493`/`8900`）| 高（NAT 穿透 over UDP 41641）| OpenClaw 本身已支持 `--tailscale funnel`；Funnel 模式要求 `gateway.bind=loopback` 且 `auth=password` |
| C | **HE 隧道代理** | 中（gogoc/hue 长驻进程）| 低（中国电信常封 protocol 41）| 不推荐生产 |

**推荐 A 或 B**。两条都能在**不要求本机有公网 IPv6** 的前提下对外提供 HTTPS 端点。

### 1.3 IPv6 路由一旦打通，本机侧要做的事

```bash
# 把 MCP 改为双栈监听（现在 OpenMontage 是 [::]:8900 OK，Voicebox 是 127.0.0.1:17493 需要改）
# /opt/voicebox/backend/main.py 启动参数：把 --host 127.0.0.1 改为 :: 或 0.0.0.0
# Cloudflared 路由：
cloudflared tunnel route dns voicebox-mcp voicebox.example.com
cloudflared tunnel route dns openmontage-mcp om.example.com
# ingress 配置（config.yml）：
#   - hostname: voicebox.example.com  →  http://127.0.0.1:17493
#   - hostname: om.example.com        →  http://127.0.0.1:8900
```

### 1.4 客户端（OpenClaw / Claude Code）配置

```jsonc
// ~/.openclaw/openclaw.json  （OpenClaw 客户端形态：mcp.servers）
{
  "mcp": {
    "servers": {
      "voicebox": {
        "url": "https://voicebox.example.com/mcp",
        "transport": "streamable-http",
        "headers": { "X-Voicebox-Client-Id": "openclaw-<host-id>" }
      },
      "openmontage": {
        "url": "https://om.example.com/mcp",
        "transport": "streamable-http",
        "headers": { "Authorization": "Bearer <MCP_API_TOKEN>" }
      }
    }
  }
}
```

```jsonc
// Claude Code 的 .mcp.json
{
  "mcpServers": {
    "voicebox":     { "type": "http", "url": "https://voicebox.example.com/mcp", "headers": { "X-Voicebox-Client-Id": "claude-code" } },
    "openmontage":  { "type": "http", "url": "https://om.example.com/mcp",     "headers": { "Authorization": "Bearer <MCP_API_TOKEN>" } }
  }
}
```

### 1.5 场景一的风险

- Voicebox 的 `X-Voicebox-Client-Id` **不是凭据**。任何能访问 `voicebox.example.com` 的人都可以伪造 `Client-Id` 占用他人 profile 绑定。需要前置一层 BFF 校验调用者（与场景二相同）。
- 200 MB 上传仍受 Starlette 16 MB 默认限制 — 必须在 voicebox 侧显式调高 `client_max_size`（`/opt/voicebox/backend/app.py` 创建 `FastAPI` 时传 `max_request_size`）。
- 旁路路由（Cloudflare/Tailscale）增加了新单点；任一侧故障都会让两端点一起不可达。

---

## 场景二：仅 IPv4 → 通过 `https://render.mengxa.com` 转发

### 2.1 为什么这是**当下**的现实路径

`render.mengxa.com` 已经在用户拥有的腾讯云 VPS（`1.14.182.208`）上公网可达，nginx + FrameFlow BFF 已经部署，BFF 已经持有 OpenMontage 的 `MCP_API_TOKEN` 并按 owner pin 住上游会话。**这是已经存在的「入站侧网关」**，剩下的只是补两块缺口：① CLI/外部代理的鉴权；② Voicebox 的转发。

### 2.2 需新增的两块能力

#### 缺口 A：CLI / 外部代理的 Bearer 鉴权

现状：BFF 只识 `ff_sid` cookie（来自微信 OAuth）。生产 `AUTH_REQUIRED=true` 时任何 CLI 调用者都会被 `RequireAuth` 中间件挡掉 401。

最小改动（在 `handlers/auth.go` 增加一个 `RequireBearer` 中间件，~40 行 Go）：

```go
// handlers/auth.go 新增
func (h *Handlers) RequireBearer() gin.HandlerFunc {
    return func(c *gin.Context) {
        auth := c.GetHeader("Authorization")
        if !strings.HasPrefix(auth, "Bearer ") {
            c.AbortWithStatusJSON(401, gin.H{"error": "missing bearer"})
            return
        }
        token := strings.TrimPrefix(auth, "Bearer ")
        // 简单做法：env 里读一份允许的 token 列表（生产应换成 DB 存 row）
        if token != h.Cfg.ExternalAgentToken {
            c.AbortWithStatusJSON(401, gin.H{"error": "bad bearer"})
            return
        }
        // 把 caller 映射成与 cookie 路径等价的 owner key
        c.Set("ownerKey", "agent:"+sha256Hex(token)[:16])
        c.Next()
    }
}
```

并在 `renderQueueOwnerID` 里识别 `agent:*` 来源，让 `SessionStore` 也按这个 key 缓存。

#### 缺口 B：Voicebox 转发路由

**推荐 Option B**（独立 Go relay，~60 行，借鉴现有 `OpenMontage-mcp-proxy/`）—— 不污染 BFF 的工具白名单、`maxMCPBodyBytes=2MB` 等语音不友好的默认配置。

把现有的 `/opt/OpenMontage/OpenMontage-mcp-proxy/main.go` 改为**多上游路径分派器**：

```go
// 新增 env:
//   VOICEBOX_UPSTREAM_URL=http://127.0.0.1:17493/mcp
//   VOICEBOX_LISTEN_PREFIX=/voicebox

// proxyConfig 改为 slice
type upstream struct {
    prefix    string                  // "/voicebox" 或 "/mcp"
    url       *url.URL
    token     string                  // OpenMontage 用；Voicebox 留空
    extraHdr  string                  // Voicebox 用 "X-Voicebox-Client-Id"
    identity  func(r *http.Request) string  // 从入站请求取 caller 标识
}

// 路由：
mux.Handle("/voicebox", auth(voiceboxProxy, cfg.clientToken))   // 路径保留转发
mux.Handle("/voicebox/", auth(voiceboxProxy, cfg.clientToken))
mux.Handle("/mcp", auth(proxy, cfg.clientToken))                 // 既有，不动
```

外部客户端通过 `https://render.mengxa.com/voicebox/mcp` 走到 voicebox：

```go
// Director（路径保留版，类比 buildProxyPreservePath）：
r.URL.Scheme, r.URL.Host = "http", "127.0.0.1:17493"
r.Host = "127.0.0.1:17493"
// 不注入 Authorization（voicebox 不要 Bearer）
// 注入 caller 提供的 X-Voicebox-Client-Id（透传，调用者自填）
if cid := r.Header.Get("X-Voicebox-Client-Id"); cid == "" {
    r.Header.Set("X-Voicebox-Client-Id", "anonymous")
}
// 调高 MaxBytesReader（200 MB 音频需要）
r.Header.Set("Accept", acceptHeader(r.Header.Get("Accept")))
```

### 2.3 客户端（OpenClaw / Claude Code）配置

```jsonc
// ~/.openclaw/openclaw.json  （场景二：经 render.mengxa.com BFF）
{
  "mcp": {
    "servers": {
      "voicebox": {
        "url": "https://render.mengxa.com/voicebox/mcp",
        "transport": "streamable-http",
        "headers": {
          "X-Voicebox-Client-Id": "openclaw-<host-id>",
          "Authorization": "Bearer <EXTERNAL_AGENT_TOKEN>"
        }
      },
      "openmontage": {
        "url": "https://render.mengxa.com/api/mcp",
        "transport": "streamable-http",
        "headers": { "Authorization": "Bearer <EXTERNAL_AGENT_TOKEN>" }
      }
    }
  }
}
```

注：`render.mengxa.com/api/mcp` 接收的是 BFF 自己的 `MCPProxy` 契约 `{tool, args}`，不是标准 MCP JSON-RPC 信封 — 这是 BFF 给浏览器封装的「薄层」。**外部 CLI 代理要直接拿到上游 MCP JSON-RPC，需要再加一条新路由**（如 `POST /api/mcp-raw`，由代理直发到 OpenMontage `/mcp`、不解析 `{tool, args}`）。或者更简单：CLI 走 `/voicebox/mcp` 这条**已经是标准 JSON-RPC 的**路由，把 OpenMontage 端也照样镜像一条 `/mcp-raw`。

### 2.4 场景二的风险

- 鉴权是单 token：`ExternalAgentToken` 一旦泄露就要轮换；与 `MCP_API_TOKEN` 一样需要重启服务。无自动 rotation。
- `SessionStore` 的会话亲和仍然关键：上传资产绑定到当前 `Mcp-Session-Id`，跨 owner 复用会丢素材。CLI 调用必须**同一 token 复用同一会话**。
- Voicebox 没有 rate limit：BFF 在 `/api/*` 上的 `RateLimit` 是基于 `ff_sid` cookie 桶；走 `/voicebox/mcp` 路径不经过 `/api` 组，**BFF 的限流不会覆盖它**。需要在独立 relay 里加 token-bucket 中间件（~20 行 Go），或把 `/voicebox` 也挂回 `/api` 组并扩 `maxMCPBodyBytes`。
- 16 MB Starlette 上传限制同样影响 voicebox 直连：需要在 voicebox `app.py` 创建 FastAPI 时显式声明 `max_request_size`，否则远端发 100 MB 音频会在 Starlette 层被截断。

---

## 3. 选型决策

| 条件 | 选 |
|------|---|
| 本机已有公网 IPv6 / HE 隧道稳定 / Tailscale 已起 | **场景一**（直连、零中转、最简单） |
| 本机无任何入站路径（**当前现状**） | **场景二**（唯一现实选项） |
| 客户端是浏览器 / 移动 App / WeChat 生态 | 场景二（沿用 FrameFlow 现有 SPA + BFF） |
| 客户端是 CLI / Claude Code / OpenClaw / 自动化脚本 | 场景二 + 必须补 Bearer 中间件 |
| 业务必须**双向**（Voicebox 也要被第三方公网调用） | 场景二 + 缺口 B |

**推荐组合**：场景二作主路径（`render.mengxa.com` 已部署就绪），同时**预埋**场景一的入口（cloudflared 跑起来后是增量配置，不阻塞场景二上线）。

---

## 4. 落地最小动作清单（按场景二）

### 阶段 0：基线（已完成）
- [x] 两个 MCP 服务在本机运行（Voicebox `:17493`、OpenMontage `:8900`）
- [x] `render.mengxa.com` 公网可达，BFF 已部署运行

### 阶段 1：BFF 加 Bearer 鉴权（让 CLI 能进）
- [ ] 在 `handlers/auth.go` 增加 `RequireBearer` 中间件（~40 行）
- [ ] `internal/config/config.go` 新增 `ExternalAgentToken`（env `EXTERNAL_AGENT_TOKEN`）
- [ ] `renderQueueOwnerID` 识别 `agent:<hash>` 来源
- [ ] `main.go` 在 `POST /api/mcp-raw` 路由上挂 `RequireBearer()`（不解析 `{tool, args}`，纯透传到 OpenMontage `/mcp`）
- [ ] 在腾讯云 VPS 上重启 BFF

### 阶段 2：Voicebox 转发（让客户端能 clone voice）
- [ ] 把 `/opt/OpenMontage/OpenMontage-mcp-proxy/` 复制为 `OpenMontage-voicebox-relay/`，重写为多上游路径分派（~60 行）
- [ ] 在 relay 内挂 token-bucket（按 caller IP + Bearer token）
- [ ] systemd unit：监听 `:18800`，env `VOICEBOX_UPSTREAM_URL=http://127.0.0.1:17493/mcp`
- [ ] 在腾讯云 VPS 上 nginx 加 location：`/voicebox/mcp → http://<LAN-IP-of-relay>:18800/mcp`
- [ ] 在 voicebox `/opt/voicebox/backend/app.py` 创建 FastAPI 时显式声明 `max_request_size = 256 MB`，让 200 MB 上传不被框架截断

### 阶段 3：客户端接线
- [ ] OpenClaw 用户填 `~/.openclaw/openclaw.json` 的 `mcp.servers.{voicebox,openmontage}`，URL 指向场景二的两个 endpoint
- [ ] Claude Code 用户填 `~/.claude/.mcp.json`，同上
- [ ] 业务流跑通：`voicebox.speak` → 拿到 `generation_id` → 把 audio_path 喂 `openmontage.execute_tool("upload_asset")` → `execute_tool("video_compose", {backgroundAudio: ...})`

### 阶段 4（可选）：预埋场景一
- [ ] 在本机装 `cloudflared`，跑 `cloudflared tunnel` 给 `voicebox.example.com` / `om.example.com` 双域名
- [ ] 当 IPv6 / cloudflared 路径稳定后，把 `mcp.servers.*.url` 切到直连地址，BFF 退化为可选

---

## 5. 关键文件路径速查

| 用途 | 路径 |
|------|------|
| FrameFlow BFF 路由 | `/opt/OpenMontage/frameflow/bff/main.go` |
| BFF 鉴权中间件 | `/opt/OpenMontage/frameflow/bff/handlers/auth.go` |
| BFF MCP 中继 | `/opt/OpenMontage/frameflow/bff/handlers/mcp.go` |
| BFF Session 亲和 | `/opt/OpenMontage/frameflow/bff/internal/mcp/session.go` |
| BFF 配置 | `/opt/OpenMontage/frameflow/bff/internal/config/config.go` |
| 单上游代理（要改造为多上游） | `/opt/OpenMontage/OpenMontage-mcp-proxy/main.go` |
| Voicebox FastAPI 入口 | `/opt/voicebox/backend/main.py`、`/opt/voicebox/backend/app.py` |
| Voicebox MCP 挂载 | `/opt/voicebox/backend/mcp_server/server.py` |
| Voicebox 客户端标识 | `/opt/voicebox/backend/mcp_server/context.py` |
| Voicebox 工具尺寸限制 | `/opt/voicebox/backend/mcp_server/tools.py:32` |
| Voicebox stdio→HTTP 桥（参考模式） | `/opt/voicebox/backend/mcp_shim/__main__.py` |
| OpenClaw 配置 | `~/.openclaw/openclaw.json` |
| OpenClaw 网关进程 | `/root/.nvm/.../openclaw/dist/index.js gateway` |
| 现有 tunnel 工具 | `/usr/local/frpc/frpc.toml-20250407`、`/opt/ddns-go/`、`/etc/mihomo/config.yaml` |
| 用户拥有的公网 VPS | `1.14.182.208`（render.mengxa.com）、`8.134.147.195`（FRP server）|