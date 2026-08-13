# Remotion Studio 部署方案（方案一：独立子域名 + nginx 根路径反代）

> 状态：方案文档（待运维实施）
> 适用：OpenMontage 远程渲染服务器（即 `dw.aixifs.com` 后端所在主机）
> 关联：本方案与 Go MCP 代理（`OpenMontage-mcp-proxy`）**无关**，Studio 不经由该代理，避免路径前缀与浏览器鉴权两大坑。

## 1. 目标

让维护「标准脚本（Remotion composition）」的运营/开发同学，能在浏览器里可视化预览、微调 composition 的 props，并实时看到效果——即把 Remotion Studio 通过公网安全暴露出来供内部使用。

**明确边界**：Studio 用于「模板创作与预览」，**不用于终端用户的批量生成**。批量生成走已有的 MCP 流水线（`create_remotion_video_share` → 后台 `remotion render`）。详见文末「与业务场景的关系」。

## 2. 为什么是「独立子域名 + 根路径」，而不是挂在 `/mcp` 下

Remotion Studio（`npx remotion studio`）是开发服务器，它的页面、静态资源、实时预览 WebSocket **全部使用根相对 URL**（如 `/static/...`、`/api/...`、ws 在 `/`），且 Remotion **不提供 base path / mount path 配置**。

- 若经 Go 代理挂在 `dw.aixifs.com/studio/`，请求会被原样转发为 `127.0.0.1:3000/studio/`，Studio 不认 → 页面能开但资源全 404、WebSocket 断。
- 只有「**根路径**」才能原样工作 → 用独立子域名（如 `studio.dw.aixifs.com`），nginx 以根路径 `proxy_pass` 到 Studio 端口。

另一个坑：Studio 是浏览器 UI，浏览器没法像 MCP 客户端那样自动给每个请求（首屏页面 + WebSocket）带 `Bearer` 头。代理的 `auth` 中间件（`main.go` 精确比对 `Authorization`）对 Studio 不现实，故改用 basic auth / IP 白名单。

## 3. 架构位置

```
浏览器
  │  https://studio.dw.aixifs.com   (basic auth / IP 白名单)
  ▼
nginx  (独立 server block，根路径反代)
  │  proxy_pass http://127.0.0.1:3000
  ▼
Remotion Studio 进程  (npx remotion studio src/index.tsx --port 3000 --host 127.0.0.1)
  │  读取 /opt/OpenMontage/remotion-composer  （与渲染流水线同一份代码）

# 注意：Studio 与现有链路完全独立
nginx → OpenMontage-mcp-proxy(Go) → Python MCP  (MCP /mcp、/render-progress/，不变)
```

Studio 进程与后台渲染（`_remotion_render` 调 `npx remotion render`）共用同一份 `remotion-composer` 代码，但各自独立进程、独立端口，互不影响。

## 4. 前置条件

- 服务器已装 Node.js（`npx remotion studio` 需要；此前渲染链路通过 `_ensure_node_on_path` 自动定位 nvm 的 node，Studio 启动也需 node 在 PATH 上）。
- `remotion-composer/node_modules` 已安装（之前渲染已证明存在）。
- 域名 `studio.dw.aixifs.com` 已解析到服务器，且已配置 SSL 证书（与 `dw.aixifs.com` 同证书或单独签发）。

## 5. 启动 Studio 进程

加载 nvm 后启动（保证 node/npx 在 PATH 上）：

```bash
# 在加载了 nvm 的 shell 中
cd /opt/OpenMontage/remotion-composer
npx remotion studio src/index.tsx --port 3000 --host 127.0.0.1
# 默认 composition 为 Explainer（与渲染流水线一致）
```

### 5.1 建议用 systemd 托管（可选但推荐）

`/etc/systemd/system/remotion-studio.service`：

```ini
[Unit]
Description=Remotion Studio (OpenMontage template preview)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/OpenMontage/remotion-composer
# 关键：source nvm 使 node/npx 进入 PATH
Environment=PATH=/root/.nvm/versions/node/v22.22.1/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=/root/.nvm/versions/node/v22.22.1/bin/npx remotion studio src/index.tsx --port 3000 --host 127.0.0.1
Restart=on-failure
# 限制资源，避免 Studio 打包抢占渲染作业资源
MemoryMax=2G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now remotion-studio
journalctl -u remotion-studio -f   # 看启动日志，确认监听 3000
```

## 6. nginx 配置

新增独立 `server` block（建议单独文件 `/etc/nginx/sites-available/studio.dw.aixifs.com.conf`，软链到 `sites-enabled/`）：

```nginx
server {
    listen 443 ssl;
    server_name studio.dw.aixifs.com;

    # —— SSL（与 dw.aixifs.com 同证书或单独签发）——
    ssl_certificate     /etc/ssl/certs/dw.aixifs.com.fullchain.pem;
    ssl_certificate_key /etc/ssl/private/dw.aixifs.com.key;
    ssl_protocols TLSv1.2 TLSv1.3;

    # —— 鉴权（Studio 无内置鉴权，二选一或叠加）——
    # 方式 A：basic auth
    auth_basic           "Remotion Studio";
    auth_basic_user_file /etc/nginx/studio.htpasswd;
    # 方式 B：IP 白名单（放 VPN / 办公室出口 IP 后）
    # allow 1.2.3.4; deny all;

    # —— 根路径反代到 Studio ——
    location / {
        proxy_pass http://127.0.0.1:3000;
        proxy_http_version 1.1;

        # WebSocket 透传（Studio 实时预览依赖）
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";

        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # HMR / 长连接不要缓冲、不要提前断
        proxy_buffering off;
        proxy_request_buffering off;
        proxy_read_timeout 600s;
        proxy_send_timeout 600s;
    }

    # 健康检查（可选）
    location = /healthz {
        proxy_pass http://127.0.0.1:3000;
        auth_basic off;   # 探活不鉴权
    }
}
```

生成 basic auth 密码文件：

```bash
# 需 apache2-utils（htpasswd）；或用 openssl 替代
htpasswd -c /etc/nginx/studio.htpasswd <your_user>
# openssl 替代：
# printf "<user>:$(openssl passwd -apr1 '<pass>')\n" > /etc/nginx/studio.htpasswd
```

生效：

```bash
nginx -t && systemctl reload nginx
```

## 7. 验证

```bash
# 1) 本机直连 Studio 端口（绕过 nginx）
curl -s -o /dev/null -w "studio local: %{http_code}\n" http://127.0.0.1:3000/

# 2) 经子域名 + basic auth
curl -s -o /dev/null -w "studio https: %{http_code}\n" \
  -u <your_user>:<your_pass> https://studio.dw.aixifs.com/

# 3) 浏览器打开 https://studio.dw.aixifs.com/ ，输入 basic auth
#    应看到 Remotion Studio 界面、Explainer composition 预览、可拖动时间轴、可改 props。
#    改一个 prop（如文案/图片）→ 预览实时更新 → 确认 WebSocket 正常。
```

## 8. 安全与运维注意

- **Studio 是开发服务器，不是生产组件**：首屏要现场打包（慢）、单用户、未硬化。务必加 basic auth **且** IP 白名单（叠加更稳）。
- **资源抢占**：Studio 打包/预览吃 CPU 与内存，与渲染作业同机时建议用 `MemoryMax`/`CPUQuota` 限制（见 5.1），或干脆跑在独立 dev 实例。
- **不要**把 Studio 端口 3000 直接暴露在公网（`--host 0.0.0.0` + 无 nginx 鉴权）。保持 `--host 127.0.0.1`，由 nginx 这一层做鉴权与 TLS。
- **版本一致性**：Studio 预览的是 `remotion-composer` 当前代码。若远程 Studio 与本地/服务器渲染用的代码不一致，预览效果会和最终渲染有偏差——保持二者同源（同一 git 工作副本 / 同一 tag）。
- **与 Go 代理无关**：本方案不经过 `OpenMontage-mcp-proxy`，无需改 `main.go`，也不影响 `/mcp`、`/render-progress/`。

## 9. 回滚

```bash
# 停止 Studio
systemctl stop remotion-studio
# 移除 nginx 站点并 reload
rm -f /etc/nginx/sites-enabled/studio.dw.aixifs.com.conf
nginx -t && systemctl reload nginx
```

## 10. 与业务场景的关系（重要）

本项目的实际业务场景是：**用户基于标准脚本做内容微调，批量生成多条短视频**。

- **Studio 不是终端用户的批量入口**。它一次渲染一条、交互式，没有「按 N 套 props 批量出片」的按钮。批量应由 MCP 流水线（`create_remotion_video_share` 异步 + 轮询 / SSE）承载——它已经能把「标准脚本 + 微调参数」批量渲染成片并出微云链接。
- **Studio 的正确定位**：给**维护标准脚本的人**当「模板创作与预览工作台」——在 Studio 里把 composition 的 props 设计成清晰可编辑的字段（文案、图片、配色、时长等），可视化调好版式；然后把这些 props 字段作为「微调」输入暴露给 MCP 批量流水线。
- **推荐分工**：Studio（远程或本地）做模板设计与校验 → 模板代码进 `remotion-composer` 仓库 → 终端用户通过 MCP 接口传入不同 props 批量出片。
- **远程 vs 本机**：见部署讨论。远程适合「需预览服务器侧资源 / 本机跑不动 node」的情况；本机更干净、隔离开发态与产线。批量渲染无论哪种都跑在远程渲染服务器。

## 11. 为什么不走 Go 代理（备查）

若强行让 Studio 也经 `OpenMontage-mcp-proxy`：需在 `main.go` 加一条 `/` 的 `buildProxyPreservePath` 转发到 `127.0.0.1:3000`，且 Studio 需走独立子域名（否则根路径冲突 `/mcp`）。但浏览器无法自动带 `Bearer`，`auth` 中间件会让首屏与 WebSocket 直接 401。要解就得在代理里做 cookie/session 网关，性价比远低于上面的 nginx basic auth 方案。故不推荐。
