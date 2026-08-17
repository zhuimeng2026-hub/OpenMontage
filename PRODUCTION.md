# OpenMontage / FrameFlow 生产环境迁移与微信登录落地规划

> 状态：PR-2（登录态 SQLite 持久化，修复"刷新跳主页"）、PR-3（监控脚本微信登录调试）、PR-4（前端"登录已失效"提示）均已**实现**并通过 `go build`/`go test`/`go vet` 与 `python -m py_compile` 验证；PR-1（DNS/证书/微信凭据等运维变更）待部署阶段执行。qrTickets 多实例共享也已落地（见下）。
> 适用范围：FrameFlow BFF（端口 8080，Go/gin）+ nginx + OpenMontage 上游 MCP（端口 8900，`lanes.ymxt.top`）+ 监控脚本 `om_mcp_probe.py`。
> 当前已实现的事实来源（务必以此为准，不要被旧文档误导）：
> - 登录实现位于 `frameflow/bff/handlers/auth.go` + `wechat.go`，路由前缀 `/api/wechat/*`、`/api/me`、`/api/logout`。
> - 环境变量名是 `WECHAT_APP_ID` / `WECHAT_APP_SECRET` / `WECHAT_REDIRECT_URI` / `WECHAT_SCOPE`（**不是** `docs/web-multiuser-auth.md` 里的 `WECHAT_MP_*`）。
> - 用户态 `userStore` 与扫码票据 `qrTickets` 现在都**持久化到 SQLite**（`wechat_users` / `wechat_qr_tickets` 表，写穿内存+DB，dev 无 DB 时回退内存）。`qrTickets` 持久化修复了多实例部署下"手机授权落 A 实例、PC 轮询在 B 实例导致扫码卡 pending"的缺口；前提是多实例共享同一 DB 卷（与 `wechat_users` 一致）。
> - `docs/web-multiuser-auth.md` 描述的是另一套跑在 8900 端口 mcp_server.py 上的 `/web/*` 方案，**当前 BFF 未采用**，规划不依赖它。

---

## 0. 目标拓扑（生产双机）

```
浏览器 ──HTTPS──> render.mengxa.com (nginx + BFF :8080)
                              │
                              └─HTTP──> lanes.ymxt.top:8900/mcp (上游 OpenMontage，仅 IPv6)
```

- `render.mengxa.com`：只承载 Web + BFF，**对外网用户**。
- `lanes.ymxt.top:8900/mcp`：只承载 MCP 上传与 Remotion 渲染，**仅对 render 机器放行**（防火墙/安全组）。浏览器永不直接持有 MCP token。
- 微信网页授权域名配置为 `render.mengxa.com`；回调 `https://render.mengxa.com/api/wechat/callback`。

---

## 0.5 数据流拓扑（代码复核结论，2026-08-17）

> 上一轮仅做了规划，本轮逐行读了 `frameflow/bff/handlers/*`、`main.go`、`internal/config/config.go` 与 `OpenMontage-mcp-proxy/main.go`、`.env` / `.env.example` / `VERIFY.md`，修正一处与原规划的偏差：**OpenMontage-mcp-proxy 不是 BFF 的下游，二者是到同一上游渲染后端的并列网关。**

### 0.5.1 图片 / 图生视频任务的真实流向（已核对代码）

```
浏览器 / 外部客户端
   │
   ├─(前端主路径)──> render.mengxa.com (nginx) ──> frameflow-bff:8080
   │                    │ RequireAuth 校验 ff_sid（AUTH_REQUIRED=true 时）
   │                    │ 转发到 MCP_BASE_URL        ← frameflow/bff/.env
   │                    ▼
   │             上游 MCP（图片上传 + 图生视频渲染后端，lanes.ymxt.top:8900）
   │
   └─(独立网关)────> OpenMontage-mcp-proxy:8080      ← 需 PROXY_CLIENT_TOKEN
                        │ 转发到 UPSTREAM_MCP_URL     ← OpenMontage-mcp-proxy/.env
                        ▼
                  同一上游 MCP
```

**已验证事实：**

1. **frameflow-bff 是前端任务实际经过的组件**，它直接把 `/api/mcp`、`/api/render-progress`、`/api/image-batches/:id/render` 等转发到自己的 `MCP_BASE_URL`（`mcp.NewSessionStore(cfg.MCPBaseURL, …)`，见 `main.go:36`；转发逻辑在 `handlers/mcp.go`）。BFF 当前 `.env` 是 `MCP_BASE_URL=http://localhost:8900/mcp`（本地 Python 渲染服务），**并未指向 OpenMontage-mcp-proxy**。
2. **OpenMontage-mcp-proxy 是另一道独立网关**：监听 `:8080`，仅转发 `/mcp` 与 `/render-progress` 到自己的 `UPSTREAM_MCP_URL`，鉴权用 `PROXY_CLIENT_TOKEN`（与上游 `mcp_key`/`UPSTREAM_MCP_TOKEN` 分离，专门用于"藏上游 token + 加客户端鉴权"）。它当前 `.env` 指向 `https://dw.aixifs.com/mcp`（一个中继），`lanes.ymxt.top:8900/mcp` 仅出现在 `.env.example` 与 `VERIFY.md` 的目标值中。
3. **参数来源不同，不可混淆：**
   - BFF：`MCP_BASE_URL` + `MCP_API_TOKEN`
   - proxy：`UPSTREAM_MCP_URL` + `UPSTREAM_MCP_TOKEN`(或 `mcp_key`) + `PROXY_CLIENT_TOKEN`

### 0.5.2 生产拓扑两选一（取决于 BFF 部署机是否有 IPv6 出口）

`lanes.ymxt.top` 仅 IPv6。因此：

- **路径 A（BFF 直连，推荐当 BFF 机有 IPv6）**：`frameflow/bff/.env` 设 `MCP_BASE_URL=http://lanes.ymxt.top:8900/mcp`。最少一跳，proxy 可不用。部署前用 `curl -6 … http://lanes.ymxt.top:8900/mcp` 探活。
- **路径 B（经 proxy 桥接，当 BFF 机仅 IPv4）**：BFF 的 `MCP_BASE_URL` 改为 **proxy 的地址**（如 `https://dw.aixifs.com/mcp`），由 proxy（具备 IPv6 出口）桥到 `lanes`。此时 proxy 的 `UPSTREAM_MCP_URL` 必须为 `http://lanes.ymxt.top:8900/mcp`。这正对应"proxy 把任务转发到 lanes"——**但前提是 BFF 当前指向 `localhost:8900` 的那行要先改成指向 proxy**，否则二者仍是并列而非串联。

> 切生产时按所选路径改 `.env`（见 1.1 / 2.1）。**当前运行中的 `.env` 不要现在改**，否则会把正在用的测试页指到生产上游而中断。

### 0.5.4 切生产 `.env` 速查（选定路径后直接覆盖）

**路径 A — BFF 直连 lanes（BFF 部署机有 IPv6 出口）**

`frameflow/bff/.env`：
```ini
SESSION_SECURE=true
AUTH_REQUIRED=true
FRONTEND_ORIGIN=https://render.mengxa.com
WECHAT_APP_ID=<正式服务号AppID>
WECHAT_APP_SECRET=<正式服务号Secret>
WECHAT_REDIRECT_URI=https://render.mengxa.com/api/wechat/callback
WECHAT_SCOPE=snsapi_userinfo
MCP_BASE_URL=http://lanes.ymxt.top:8900/mcp
MCP_PROGRESS_URL=http://lanes.ymxt.top:8900/render-progress
MCP_API_TOKEN=<上游token>
RATE_LIMIT_PER_MIN=30
DEV_LOGIN_ALLOWED=false
STATE_DB_PATH=./data/frameflow.db
```
proxy 可不部署（或保留做独立网关，其 `UPSTREAM_MCP_URL` 同样设 `http://lanes.ymxt.top:8900/mcp`）。

**路径 B — BFF 经 proxy 桥接 lanes（BFF 部署机仅 IPv4）**

`frameflow/bff/.env`（仅改上游两行，其余同路径 A）：
```ini
MCP_BASE_URL=https://dw.aixifs.com/mcp            # 或自建 proxy 的公网地址
MCP_PROGRESS_URL=https://dw.aixifs.com/render-progress
```
`OpenMontage-mcp-proxy/.env`：
```ini
UPSTREAM_MCP_URL=http://lanes.ymxt.top:8900/mcp
UPSTREAM_MCP_TOKEN=<上游token>          # 或 legacy mcp_key
PROXY_CLIENT_TOKEN=<独立客户端token>     # 必须与上游 token 不同
PORT=8080
```

### 0.5.3 生产缺口跟踪

- ~~扫码票据 `qrTickets` 仍是进程内内存（`wechat.go:58`）~~ **已修复**：`qrTickets` 改为写穿 `wechat_qr_tickets` 表（2026-08-17 第二轮），多实例下手机回调与 PC 轮询落在不同实例也能共享扫码状态。前提：多实例共享同一 DB 卷（与 `wechat_users` 一致）；若各实例独立 SQLite 文件则仍需粘性会话或改 Redis。可用监控命令 `qr-cross-instance` 验证。
- 多实例可观测性已补齐：监控脚本新增 `instances`（多实例健康检查 + 微信 APPID 配置一致性）与 `qr-cross-instance`（A 建票 / B 查状态，验证票据跨实例共享）两个子命令，详见第 5 节。

---

## 1. 任务一：`render.mengxa.com` 迁到正式外网服务器

**前提**：用户称"测试环境已重新配置好"。本任务把该域名最终指向**正式外网服务器**并切换为生产级配置。

### 1.1 需要修改的参数位置清单

| 位置 | 参数 | 测试环境（现状） | 生产值 | 说明 |
|---|---|---|---|---|
| DNS | `render.mengxa.com` A/AAAA | 指向测试机 | **指向正式外网服务器 IP** | 域名解析切换，外部可见 |
| `nginx/frameflow-render-production.conf.template` | `ssl_certificate` / `ssl_certificate_key` | 模板占位 `/etc/letsencrypt/live/render.mengxa.com/...` | 替换为正式证书真实路径 | 证书由正式服务器 ACME 签发，**不入库** |
| 同上 | `server_name` | `render.mengxa.com` | 不变 | 已在模板中 |
| `frameflow/bff/.env`（生产） | `SESSION_SECURE` | `false` | **`true`** | 仅 HTTPS 下发 cookie |
| 同上 | `AUTH_REQUIRED` | `false` | **`true`** | 生产必须登录 |
| 同上 | `FRONTEND_ORIGIN` | 空 | `https://render.mengxa.com` | 防 CSRF / CORS 一致性 |
| 同上 | `WECHAT_REDIRECT_URI` | 空 | `https://render.mengxa.com/api/wechat/callback` | 防止代理头错把回调协议变 HTTP |
| 同上 | `WECHAT_APP_ID` / `WECHAT_APP_SECRET` | 空 | 填正式服务号凭据 | 缺失时 `config.Validate` 直接 fail-closed 拒绝启动 |
| 同上 | `MCP_BASE_URL` / `MCP_PROGRESS_URL` | 视拓扑 | `http://lanes.ymxt.top:8900/mcp` 或合并部署 `http://127.0.0.1:8900/mcp` | 见任务二 |
| 同上 | `RATE_LIMIT_PER_MIN` | `0`/`60` | `30`（或按容量） | 关闭测试期宽松限流 |
| 微信公众平台后台 | 网页授权域名 | 测试域名 | `render.mengxa.com` | 与 `WECHAT_REDIRECT_URI` 域名一致 |
| 微信公众平台后台 | 服务器 IP 白名单 | 测试机 | 正式外网服务器出口 IP | 若用安全模式消息校验 |

### 1.2 部署步骤（给执行大模型）

1. 在正式外网服务器用 `nginx/frameflow-render-production.conf.template` 部署 nginx，签发/放置 Let's Encrypt 证书，执行 `nginx -t` 并重载。
2. 准备生产 `frameflow/bff/.env`（上表），`godotenv.Load()` 已支持从 `.env` 读取（见 `internal/config/config.go`），**不要把证书或 `.env` 提交仓库**（已在 `.gitignore`）。
3. 启动 BFF（`go run .` 或编译后的二进制）。`config.Validate` 会在 `AUTH_REQUIRED=true` 且缺微信凭据时 `log.Fatal` 拒绝启动——这是预期的安全闸，需先填好微信参数。
4. 验证（见第 5 节验收）。

### 1.3 注意点

- `nginx/sites-enabled/render.mengxa.com.conf` 是当前**自签证书开发环境**配置（证书在 `nginx/ssl/render.mengxa.com/`，含私钥已 gitignore）。生产服务器改用 `frameflow-render-production.conf.template`，不要直接复用开发 conf。
- 微信「网页授权域名」校验文件（`MP_verify_*.txt`）需在 HTTP 80 下 200，开发 conf 已单独放行；生产模板的 `location /.well-known/acme-challenge/` 仅处理 ACME，需确保 `MP_verify_*.txt` 也在 80 端口可访问（或在微信后台改用文件校验路径一致）。

---

## 2. 任务二：主力配置机 `lanes.ymxt.top:8900/mcp`（域名→IPv6）的测试页参数清单

**核心事实**：`lanes.ymxt.top` 仅解析到 **IPv6（AAAA）**，无 IPv4 路由。因此"域名转换为 IPv6"是 **DNS 层行为，不是代码层转换**——只要 DNS 返回 AAAA 且**部署机具备 IPv6 出口**，BFF 用域名直连即可；若部署机无 IPv6（如本机开发机），必须走中转代理。

### 2.1 "当前测试页面"转换为生产时，需要改动的参数位置

| 文件 | 参数 / 字段 | 测试期常见值 | 生产期应有值 | 依赖条件 |
|---|---|---|---|---|
| `frameflow/bff/web/config.js` | `FF_CONFIG.remotion.bffBaseUrl` | 测试时可能写死 `http://localhost:8080` 或测试机 IPv4 | **`https://render.mengxa.com`** 或保持 `window.location.origin`（BFF 同源托管时） | 必须同源或填 BFF 公网域名，否则 cookie 跨域带不上 |
| `frameflow/bff/.env` | `MCP_BASE_URL` | 测试机内网 `http://127.0.0.1:8900/mcp` | `http://lanes.ymxt.top:8900/mcp` | **部署机需 IPv6 出口** |
| 同上 | `MCP_PROGRESS_URL` | `http://127.0.0.1:8900/render-progress` | `http://lanes.ymxt.top:8900/render-progress` | 同上 |
| `OpenMontage-mcp-proxy/.env` | `UPSTREAM_MCP_URL` | 开发机无 IPv6 时 `https://dw.aixifs.com/mcp` | `http://lanes.ymxt.top:8900/mcp`（若 BFF 经 proxy 桥接，保持/改用 proxy 公网地址；见 0.5.2） | proxy 是 BFF 的**并列网关而非下游**；仅当 BFF 机无 IPv6 时经 proxy 桥接 IPv6 上游 |
| `nginx/...conf` | `server_name` / `proxy_pass` | 测试机 | 生产服务器 | 见任务一 |
| `frameflow/bff/.env` | `WECHAT_REDIRECT_URI` / `FRONTEND_ORIGIN` / `SESSION_SECURE` / `AUTH_REQUIRED` | 测试宽松 | 生产严格（见任务一） | — |

### 2.2 IPv6 连通性判定（给执行大模型的关键检查）

- 部署脚本应先做连通性探测：`curl -6 -sS -o /dev/null -w "%{http_code}" -X POST http://lanes.ymxt.top:8900/mcp`（带 Bearer）。成功 2xx 说明该机 IPv6 可达上游。
- 若探测失败：**不要**改回 `dw.aixifs.com` 中转（那是本地权宜，生产不应依赖第三方中转）。正确做法是给部署机配置 IPv6 出口，或让 `lanes` 机器同时暴露一个该部署网段可达的地址。
- 备注：`mcp_server.py` 的 uvicorn 在 IPv6 环境会 `sock.bind(("::", 8900))`（`mcp_server.py:1759` 附近），确认上游确实在监听 `:::8900` 且防火墙放行 render 机器来源。

### 2.3 合并部署的例外

当 Web/BFF/MCP 部署在同一台 `render.mengxa.com` 机器（合并部署，参考 `DEPLOYMENT_RUNBOOK.md` "生产合并部署"），`MCP_BASE_URL` 应改回 `http://127.0.0.1:8900/mcp`，**避免经域名/LAN 回环穿过防火墙导致上传卡住**——这是 runbook 明确要求的边界。

---

## 3. 任务三：监控脚本 `om_mcp_probe.py` 新增微信登录调试能力  【已实现】

**目的**：生产环境微信扫码登录出问题时，用脚本黑盒复现整条登录链路，定位是"微信未配置 / 回调不通 / cookie 不持久 / 状态轮询卡住"。

### 3.1 需要新增的子命令（在现有 `MCPClient`/`main` 基础上扩展）

沿用现有 `curl + SID 轮换 + 重试` 基础设施，新增对 BFF（8080）的探测。建议新增 `--bff` 参数（`OM_BFF_URL`，默认 `https://render.mengxa.com`）。

| 子命令 | 作用 | 关键检查点 |
|---|---|---|
| `wechat-config` | `GET /api/wechat/qrlogin` | 返回 400 + "wechat not configured" ⇒ 微信未配；200 + `auth_url` ⇒ 已配。顺便打印 `auth_url` 里的 `appid` 与 `redirect_uri` 是否一致 |
| `me` | `GET /api/me`（带 cookie jar） | `authenticated:true/false`；配合 `-c cookie.txt` 持久化 `ff_sid` |
| `qr-create` | `GET /api/wechat/qrlogin` | 取 `ticket` + `auth_url`；用 `qrcode`/纯文本打印 `auth_url` 供人工扫码 |
| `qr-status` | `GET /api/wechat/qrlogin/status?ticket=` | 轮询打印 `pending/authorized/expired/invalid`；`authorized` 时一并 `GET /api/me` 确认用户已绑定 |
| `qr-wait` | 组合：create → 持续轮询 → 授权后 `me` | 一键端到端，模仿前端轮询；超时（默认 300s）退出并打印最终状态 |
| `cookie-check` | 解析某次响应的 `Set-Cookie` | 校验 `ff_sid` 的 `Secure` / `HttpOnly` / `SameSite` / `Path=/` / `Max-Age` 是否符合生产预期（`SESSION_SECURE=true` 时应带 `Secure`） |
| `login-flow` | 完整链路：`qr-create` → 提示扫码 → `qr-wait` → `me` | 输出一个判定：扫码登录在生产环境是否真正可用 |

### 3.2 实现要点（给执行大模型）

1. **Cookie jar 持久化**：新增 `-c/--cookie-jar` 参数，把 `ff_sid`（及 `ff_wx_state`）写入/读取文件（Netscape 格式或自管 dict），让 `me`/`qr-status` 跨子命令共享会话——这正是 `projects/probe_render_loop/repro_2inst.py` 踩过的坑（cookiejar 在 localhost 下不随请求发出，需显式 `Cookie:` 头）。建议脚本内部统一用显式 `Cookie: ff_sid=...` 头，避免依赖 curl 的 jar 自动合并。
2. **新增 BFF 客户端类**（或复用 `MCPClient` 的 `_request` 思路）：`BFFClient(url, cookie_jar)`，方法 `get(path)` / `poll_qr(ticket, timeout)`。
3. **`auth_url` 校验**：从 `qr-create` 返回的 `auth_url` 解析 query，比对 `redirect_uri` 的 host 是否等于 `render.mengxa.com`，`appid` 是否非空。
4. **`cookie-check`** 用正则解析 `curl -D` 出的 `Set-Cookie` 头，列出属性并给出告警（如生产环境缺 `Secure`）。
5. 不回显任何微信 Secret / MCP token；日志只记录脱敏后的状态。
6. 维持现有 `OM_MCP_URL` / `OM_MCP_TOKEN` 的 MCP 探测能力不变，新增的是 BFF 登录探测，**互不干扰**。

---

## 4. 任务四：刷新跳回主页的根因 + 微信扫码登录持久化实现  【已实现】

### 4.1 根因分析（务必先对齐，再编码）

**现象**：进入演示环境后功能正常，但**刷新页面立即回到主页（登录页）**。

**前端行为**（`frameflow/bff/web/index.html` 加载时 IIFE，约 803–828 行）：
- 页面默认显示 `login-view`；只有 `GET /api/me` 返回 `authenticated:true` 才调用 `enterApp()` 进入应用。
- 刷新时走 `else` 分支（820 行）：`fetch('/api/me')` → 若 `authenticated` 为假，**什么都不做**，页面停在无登录态的 `login-view` —— 用户感知即"跳回主页"。
- 前端逻辑本身**正确**：它如实反映后端会话状态。问题在后端会话"丢了"。

**后端根因**（`frameflow/bff/handlers/auth.go`）：
- `userStore`（`auth.go:107`）是 **BFF 进程内 `map`**，用户记录（openid/昵称）只存在内存。
- `ff_sid` cookie 本身设了 7 天有效期 + `HttpOnly` + `SameSite=Lax`（`ensureSession`，`auth.go:72–80`），**cookie 没丢**；但 `Me()`（`auth.go:255`）用 cookie 去 `loadUser(sid)`，内存里没这条记录就返回 `authenticated:false`。
- 触发"内存丢失"的两种真实场景，正好对应生产：
  1. **BFF 重启 / 滚动发布** → 内存清空 → 全部用户被登出。
  2. **多实例负载均衡** → 用户 A 的 session 落在实例 1，刷新后请求落到实例 2，实例 2 内存无该记录 → 登出。
- runbook 已预警："桌面二维码登录依赖 BFF 进程内票据；单实例联调可用，多实例/滚动发布前需迁移到共享存储。" 本任务即落地该迁移。

**结论**：刷新跳主页 = 登录态未持久化（不是 cookie 问题，是后端用户态内存态丢失）。必须做"微信扫码登录 + 会话持久化到共享存储"，才能在生产多实例/重启下保持登录。

### 4.2 实现方案（给执行大模型）

**A. 用户态持久化到 SQLite（与既有 `mcp_user_sessions` 表同构）**

- 新增表 `wechat_users(ff_sid TEXT PK, openid TEXT, nickname TEXT, scope TEXT, profile_json TEXT, created_at, expires_at)`。
- 在 `handlers/auth.go` 的 `saveUser`/`loadUser`/`dropUser` 中：写时同时写 SQLite，读时优先内存、miss 则回查 SQLite 恢复（参考 2026-08-15 对 `mcp_user_sessions` 的"冷实例恢复上游 sid"做法）。
- `ff_sid` 7 天过期：登录时写 `expires_at = now+7d`；`Me()` 命中但已过期的视为未登录并清记录。
- 切勿在 DB/日志落敏感信息；`profile_json` 只存昵称等非敏感摘要，不存微信 `access_token`。

**B. 扫码票据 `qrTickets` 的共享（影响多实例）**

- ~~当前 `qrTickets`（`wechat.go:58`）也是进程内 map~~ **已落地轻量方案**：改为写穿 `wechat_qr_tickets` 表（同一 SQLite），`QrLoginStatus`/`WechatCallback` 跨实例可见。多实例须共享同一 DB 卷；独立 SQLite 文件时仍建议单实例 + 粘性会话过渡。
- 监控命令 `qr-cross-instance`（A 建票 / B 查状态）可直接验证该共享是否生效。

**C. 前端无需大改，但建议加固**

- 前端 `enterApp`/`fetch('/api/me')` 逻辑正确，保持。
- 建议补充：刷新后若 `?login=wechat` 或带 `code`，已正确处理；确认 `config.js` 的 `bffBaseUrl` 在生产为 `https://render.mengxa.com` 或同源（`window.location.origin`），否则 `fetch` 跨域带不上 `ff_sid` cookie（见任务二 2.1）。
- 可选 UX：当 `authenticated:false` 且非首次，显示"登录已失效，请重新扫码"而非静默回到登录页。

**D. 验证登录持久化**

- 用任务三新增的 `qr-wait` + `me` 模拟登录；登录后**重启 BFF**，再 `me -c cookie.txt` 应仍返回 `authenticated:true`（验证 SQLite 恢复）。
- 多实例场景：起两个 BFF 共享同一 SQLite，登录后请求落到另一实例，`me` 仍 `authenticated:true`。

---

## 5. 验收清单（生产上线前）

1. `nginx -t` 通过；BFF 启动无 `AUTH_REQUIRED is false` / `WechatAppID not configured` 告警。
2. `GET /api/me` 在匿名时返回 `{"authenticated":false}`；登录后 `{"authenticated":true,"user":{...}}`。
3. 微信扫码登录：手机扫码授权 → 桌面轮询 `authorized` → `enterApp` 进入；`om_mcp_probe.py qr-wait` 端到端通过。
4. **刷新页面不跳回登录页**（核心回归，验证任务四）：登录态跨刷新保持；BFF 重启后 `ff_sid` 仍有效。
5. `cookie-check` 显示 `ff_sid` 带 `Secure; HttpOnly; SameSite=Lax; Path=/`，`Max-Age≈604800`。
6. 5–10 张大图分块上传（`upload_asset_chunk`）同一 `project_id`；提交渲染 `/api/render-progress/:jobId` SSE 不被 nginx 缓冲。
7. 两个独立微信用户各提交任务，`/api/render-queue` 仅返回本人任务（数据隔离，参考 `docs/user-data-isolation-analysis.md`）。
8. 监控脚本 `wechat-config` 显示微信已配置；`me`/`qr-*` 全链路可用。

## 6. 风险与回滚

- **风险**：`SESSION_SECURE=true` 但前端经 HTTP 访问（如证书/代理错误）→ cookie 不下发 → 永远未登录。回滚：`SESSION_SECURE=false` 临时降级 + 修 TLS。
- **风险**：`AUTH_REQUIRED=true` 但微信未配 → BFF 启动即 `log.Fatal`。务必先填 `WECHAT_APP_ID/SECRET` 再切 true。
- **风险**：持久化引入 SQLite 写竞争。复用既有 `state/db.go` 连接与 WAL，避免新建独立连接池。
- **回滚**：保留 `frameflow-render-local.conf.template` 与测试 `.env`，DNS 切回测试机即可回退；代码侧若持久化有问题，先以"单实例 BFF + 粘性"约束上线（见 4.2-B），不阻塞上线。

---

## 7. 执行切分建议（供后续大模型排期）

- **PR-1（任务一+二，运维/配置）**：生产 `.env`、nginx 生产模板证书路径、DNS 切换、IPv6 连通性探测与回退方案。低风险、纯配置。
- **PR-2（任务四，后端）**：`wechat_users` 表 + `saveUser/loadUser/dropUser` 持久化 + 过期清理；`qrTickets` 共享或单实例约束。需配套单测（参考 `auth_test.go`）。
- **PR-3（任务三，工具）**：`om_mcp_probe.py` 新增 `wechat-config/me/qr-create/qr-status/qr-wait/cookie-check/login-flow` 与 cookie jar。
- **PR-4（任务四，前端加固，可选）**：登录失效提示文案；确认 `bffBaseUrl` 同源。
- 验收以第 5 节清单为准，重点回归项 = 第 4 项（刷新不跳主页）。

## 8. 实现进度（截至 2026-08-17）

PR-2（任务四，后端登录态持久化）与 PR-3（任务三，监控脚本微信调试）已编码完成：

### PR-2 改动文件
- `frameflow/bff/internal/state/db.go`：新增 `wechat_users` 表（ff_sid/openid/nickname/scope/profile_json/created_at/expires_at + 过期索引）。
- `frameflow/bff/handlers/auth.go`：
  - 包级 `userDB *sql.DB` + `loadUserMap()`，作为内存热缓存与 SQLite 的回查层；
  - `saveUser/loadUser/dropUser` 同时写内存与 `wechat_users`，`loadUser` 命中但过期则删除并视为未登录；
  - `renderQueueOwnerID` 改为走 `loadUserMap`，多实例下也能稳定解析同一微信用户的队列身份；
  - 新增 `persistUser/findPersistedUser/deletePersistedUser/isExpired` 辅助。
- `frameflow/bff/main.go`：`handlers.New(...)` 注入 `db`。
- `frameflow/bff/handlers/auth_test.go`：适配 `New` 新签名；新增 `TestWechatSessionSurvivesInMemoryLoss`（验证登录态跨内存丢失从 SQLite 恢复，即"刷新跳主页"修复）。

### PR-3 改动文件
- `om_mcp_probe.py`：新增 `BFFClient` 类与子命令 `wechat-config / me / qr-create / qr-status / qr-wait / cookie-check / login-flow`；cookie jar 以显式 `Cookie:` 头跨子命令共享 `ff_sid`；`cookie-check` 解析 `Set-Cookie` 校验 Secure/HttpOnly/SameSite/Path/Max-Age；`qr-wait` 登录成功后落盘 `om_mcp_setcookie.txt` 供 `cookie-check` 离线分析。

### 验证
- `cd frameflow/bff && go build ./...` 通过；`go vet ./...` 无警告。
- `go test ./handlers/ -run 'WechatSessionSurvivesInMemoryLoss|TestRequireAuth|TestRenderQueueOwner' -v` 全部 PASS。
- `python -m py_compile om_mcp_probe.py` 通过；`python om_mcp_probe.py --help` 正确列出全部 BFF 子命令。

### 待部署阶段（PR-1 / PR-4，非本次编码）
- 真实 DNS 切换 `render.mengxa.com` → 正式外网 IP、Let's Encrypt 证书签发、微信公众平台网页授权域名与 IP 白名单配置。
- 生产 `.env`：`AUTH_REQUIRED=true`、`SESSION_SECURE=true`、`WECHAT_APP_ID/SECRET`、`WECHAT_REDIRECT_URI=https://render.mengxa.com/api/wechat/callback`、`FRONTEND_ORIGIN`、`MCP_BASE_URL=http://lanes.ymxt.top:8900/mcp`（需部署机 IPv6 出口）。
- 注意：生产 nginx 模板（第 1.3 节）仅放行 `/.well-known/acme-challenge/`，微信「网页授权域名」校验文件 `MP_verify_*.txt` 需确认在 80 端口根路径可访问，否则微信后台配置会失败。
- 前端登录失效提示（PR-4）为可选增强。
