# 网页多用户登录与数据隔离

## 目标

OpenMontage 原有的 `MCP_API_TOKEN` 是服务级共享凭据，`Mcp-Session-Id` 只是一次 MCP 连接的会话标识，不是用户账号。本功能新增浏览器用户层：微信服务号网页 OAuth 登录后得到 OpenMontage 用户 ID，用户的网页项目存放在独立目录中。

当前边界：WorkBuddy/MCP 继续使用原有 Bearer Token + `Mcp-Session-Id` 协议；网页层不会把 MCP session 伪装成用户，也不会改变现有客户端兼容性。后续若要让 WorkBuddy 绑定网页账号，应在代理层增加显式的账号/设备绑定流程，而不是信任客户端自报的 user id。

## 配置

在 `.env` 设置：

```dotenv
WECHAT_MP_APP_ID=服务号 AppID
WECHAT_MP_APP_SECRET=服务号 AppSecret
WECHAT_MP_REDIRECT_URI=https://example.com/web/callback/wechat
WECHAT_MP_SCOPE=snsapi_userinfo
OPENMONTAGE_WEB_COOKIE_SECURE=true
```

微信公众平台的网页授权域名必须覆盖实际域名，回调地址必须与 `WECHAT_MP_REDIRECT_URI` 完全一致。服务端只保存微信身份映射和必要的非敏感 profile 摘要，不保存微信 access token。

## 路由

- `GET /web/`：未登录显示微信登录入口，已登录返回当前用户摘要。
- `GET /web/login/wechat`：创建一次性、10 分钟有效的 OAuth state 并跳转微信。
- `GET /web/callback/wechat`：校验 state、换取微信身份、建立 HttpOnly session cookie。
- `GET /web/api/me`：读取当前登录用户。
- `GET /web/api/projects`：只列出当前用户的项目。
- `POST /web/api/projects`：在当前用户目录创建项目并初始化 `assets/`、`renders/`、`artifacts/`，JSON body 为 `{"project_id":"demo"}`。
- `GET /web/api/projects/{project_id}`：查看当前用户项目的素材和渲染产物摘要。
- `POST /web/api/assets`：上传当前用户项目素材，JSON body 为 `{"project_id":"demo","filename":"a.png","content_base64":"..."}`。
- `GET/POST /web/logout`：撤销当前浏览器 session。

用户数据根目录为 `projects/users/<user_id>/`；SQLite 用户、session 和 OAuth state 存在 `projects/.users/users.sqlite3`。OAuth state 和 session token 只存 SHA-256 摘要，state 使用一次后立即删除，cookie 为服务端会话凭据并设置 HttpOnly + SameSite=Lax。网页层不会接受用户提交的磁盘路径，上传内容会重新写入当前用户目录。

## 启动

网页路由与 streamable HTTP MCP 共用 8900 端口。生产环境应通过 HTTPS 反向代理暴露 `/web/*` 和 `/mcp`，并保留 MCP Bearer 认证。未配置微信变量时，登录入口返回明确的 503 配置错误，不会回退到共享 token。
