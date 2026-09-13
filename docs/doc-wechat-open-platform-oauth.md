# 微信开放平台网站应用 OAuth 用户认证说明

> **本项目实际采用：服务号网页授权 + 桌面扫码弹窗**，详见下文
> 「[桌面扫码登录（服务号方案）](#桌面扫码登录服务号方案本项目实际采用)」。
> 正文的开放平台网站应用（qrconnect / `snsapi_login`）方案需要微信开放平台账号并认证
> （约 ¥300/年），本项目未采用。阅读本文件时请以「桌面扫码登录」一节为准。

## 概述

微信开放平台的**网站应用**支持 PC 端网页的微信扫码登录。用户在浏览器中打开网站 → 跳转微信扫码 → 授权后回跳，前端拿到 JWT + openid。

它与微信公众号 OAuth 的核心区别：

| | 微信公众号 (服务号) | 微信开放平台 (网站应用) |
|---|---|---|
| 入口 | 微信内打开网页 / JS-SDK | PC 浏览器打开网页 |
| 授权方式 | 静默授权或弹窗授权 | 展示二维码，用户扫码 |
| scope | `snsapi_base` 或 `snsapi_userinfo` | `snsapi_login` |
| code 换 token 返回 | 仅 `openid` (scope=base 时) | `access_token` + `openid` + `unionid` |
| 用户信息接口 | `/sns/userinfo` (需 scope=userinfo) | `/sns/userinfo` (用网页授权 access_token) |

## OAuth 2.0 流程

```
  用户浏览器                  网关                      微信开放平台
  ─────────                  ────                      ────────────
      │                        │                           │
      │ GET /auth/:appId       │                           │
      │ ──────────────────────>│                           │
      │                        │                           │
      │      302 到微信授权页   │                           │
      │ <──────────────────────│                           │
      │                        │                           │
      │ 微信扫码授权 ──────────────────────────────────────>│
      │                        │                           │
      │                        │   GET /callback?code=&state=
      │ <────────────────────────────── 302 到网关回调 ─────│
      │                        │                           │
      │                        │ code 换 openid+unionid    │
      │                        │ ─────────────────────────>│
      │                        │                           │
      │   302 回跳前端          │                           │
      │   ?token=JWT&openid=xx │                           │
      │ <──────────────────────│                           │
      │                        │                           │
```

## API 端点

### 1. 发起授权 `GET /auth/:appId`

让用户浏览器跳转到微信扫码页。

```
GET /auth/{app_id}?redirect={回跳地址}
```

**参数说明：**

| 参数 | 必填 | 说明 |
|------|------|------|
| `app_id` | 是 | 在网关中注册的应用 ID |
| `redirect` | 否 | 授权后的回跳地址，默认 `/`。支持相对路径（`/page`）或绝对 URL（`https://example.com/page`） |

**实现细节：** 网关构建的微信授权 URL 格式如下（网站应用）：

```
https://open.weixin.qq.com/connect/qrconnect
  ?appid={AppID}
  &redirect_uri={url_encode(网关/callback)}
  &response_type=code
  &scope=snsapi_login
  &state={JWT(app_id + redirect)}
  #wechat_redirect
```

- `appid` — 微信开放平台网站应用的 AppID
- `redirect_uri` — 必须是**网关的 `/callback` 地址**（在开放平台后台配置的回调域名范围内）
- `scope` — `snsapi_login`，固定值
- `state` — 网关签发的 JWT，包含 `app_id` 和 `redirect` 信息，回调时用于恢复上下文
- `redirect_uri` 结尾的 `#wechat_redirect` 是微信要求的固定后缀

### 2. 授权回调 `GET /callback`

微信扫码成功后，微信服务器 302 到网关的这个地址：

```
GET /callback?code={code}&state={state}
```

网关处理流程：

1. 校验 `state` (JWT)，提取 `app_id` 和 `redirect`
2. 用 `code` 调用微信 `/sns/oauth2/access_token` 换取 `openid`
3. Upsert 用户（`provider=wx_web`）和身份关联
4. 签发用户 JWT (24h 有效期)
5. 302 回跳到前端：

```
{redirect}?token={JWT}&openid={openid}
```

> 如果回调中的 `code` 或 `state` 缺失或无效，返回 HTTP 400。

### 3. 静默登录 `POST /api/auth/login`

适用于无法走浏览器跳转的场景（小程序 webview 等），前端先通过微信 JS-SDK 拿到 code，然后调用此接口换取 JWT：

```json
POST /api/auth/login
{
    "code": "微信授权code",
    "app_id": "hotnews"
}
```

响应：

```json
{
    "code": 0,
    "token": "eyJ...",
    "openid": "oXXXX...",
    "user_id": 1
}
```

## 微信开放平台 API 详情

### code 换 access_token

```
GET https://api.weixin.qq.com/sns/oauth2/access_token
  ?appid={AppID}
  &secret={AppSecret}
  &code={code}
  &grant_type=authorization_code
```

**区别于公众号：** 域名是 `api.weixin.qq.com`（不是 `open.weixin.qq.com`），路径是 `/sns/oauth2/access_token`。

**成功响应：**

```json
{
    "access_token": "ACCESS_TOKEN",
    "expires_in": 7200,
    "refresh_token": "REFRESH_TOKEN",
    "openid": "oXXXX...",
    "scope": "snsapi_login",
    "unionid": "oXXXX_unionid"
}
```

关键字段：
- `access_token` — 网页授权 token（非普通 access_token），可用于调用户信息接口
- `refresh_token` — 可刷新 access_token（长期有效）
- `openid` — 用户在该应用下的唯一标识
- `unionid` — 用户在**同一开放平台主体**下的唯一标识，可用于打通多个应用的用户体系

**错误响应：**

```json
{
    "errcode": 40029,
    "errmsg": "invalid code"
}
```

常见错误码：

| errcode | 说明 |
|---------|------|
| 40029 | code 无效（已过期或已使用） |
| 40163 | code 已使用（微信 code 一次性） |
| 41001 | access_token 缺失 |
| 41002 | access_token 过期 |

### 获取用户信息（可选）

用网页授权拿到的 `access_token` 可以获取用户头像和昵称：

```
GET https://api.weixin.qq.com/sns/userinfo
  ?access_token={ACCESS_TOKEN}
  &openid={OPENID}
  &lang=zh_CN
```

**响应：**

```json
{
    "openid": "oXXXX...",
    "nickname": "用户昵称",
    "sex": 1,
    "province": "广东",
    "city": "深圳",
    "country": "中国",
    "headimgurl": "https://...",
    "privilege": [],
    "unionid": "oXXXX_unionid"
}
```

> 注意：此接口在不同环境下的规则可能有所不同，测试号返回的数据可能与正式应用不同。如果只需 `openid` 做标识，code 换 token 那一步就已经足够了。

### 刷新 access_token

```
GET https://api.weixin.qq.com/sns/oauth2/refresh_token
  ?appid={AppID}
  &grant_type=refresh_token
  &refresh_token={REFRESH_TOKEN}
```

**响应：**

```json
{
    "access_token": "NEW_ACCESS_TOKEN",
    "expires_in": 7200,
    "refresh_token": "NEW_REFRESH_TOKEN",
    "openid": "oXXXX...",
    "scope": "snsapi_login"
}
```

> 注意：刷新后会同时返回新的 `refresh_token`，旧的作废。`refresh_token` 有效期30天。

## 环境变量配置

在 `.env` 中添加开放平台网站应用的凭证：

```bash
# 开放平台网站应用：WX_ 前缀 + AppID + _SECRET
WX_{AppID}_SECRET={AppSecret}

# 注册应用：APP_ 前缀 + 应用名 = ProviderKey,RedirectBase
APP_{app_name}={AppID},{redirect_base_url}

# 网关基础配置
GATEWAY_URL=https://auth.example.com
JWT_SECRET=your-jwt-secret
```

**示例：**

```bash
# 一个开放平台网站应用，AppID 是 wxa1234567890abc
WX_wxa1234567890abc_SECRET=abcdef1234567890abcdef1234567890

# 注册为 hotnews 应用，前端地址是 https://hotnews.example.com
APP_hotnews=wxa1234567890abc,https://hotnews.example.com
```

配置后在日志中能看到：

```
[auth] appId=hotnews, state=eyJ..., redirect=/page
[callback] appId=hotnews, provider=wx_web, externalId=oXXXX
```

> 注意：开放平台网站应用的 `redirect_uri` 域名必须在微信开放平台后台 > 网站应用 > 授权回调域 中配置。例如网关域名为 `auth.example.com`，则在开放平台后台填 `auth.example.com`。

## 前端接入示例

```javascript
// 1. 跳转微信扫码登录
function login() {
  const appId = 'hotnews';
  const redirect = encodeURIComponent(location.pathname + location.search);
  location.href = `/auth/${appId}?redirect=${redirect}`;
}

// 2. 回调页面从 URL 参数提取 token
const params = new URLSearchParams(location.search);
const token = params.get('token');
const openid = params.get('openid');

if (token) {
  localStorage.setItem('token', token);
  localStorage.setItem('openid', openid);
  // 清理 URL 参数
  history.replaceState(null, '', location.pathname);
}

// 3. 后续请求带上 JWT
fetch('/api/user/me', {
  headers: { 'Authorization': `Bearer ${localStorage.getItem('token')}` }
});
```

## 与微信公众号 OAuth 的技术差异对比

| 项目 | 公众号 OAuth | 开放平台网站应用 |
|------|-------------|----------------|
| 授权 URL 域名 | `open.weixin.qq.com` | `open.weixin.qq.com` |
| 授权 URL 路径 | `/connect/oauth2/authorize` | `/connect/qrconnect` |
| code 换 token 域名 | `api.weixin.qq.com` | `api.weixin.qq.com` |
| code 换 token 路径 | `/sns/oauth2/access_token` | `/sns/oauth2/access_token` |
| scope | `snsapi_base` | `snsapi_login` |
| 返回 access_token | 否 (scope=base) | 是 |
| 返回 refresh_token | 否 | 是 (30天有效) |
| 返回 unionid | 仅绑定开放平台后 | 是 |
| 用户信息接口 | `/sns/userinfo` | `/sns/userinfo` |
| provider 标识 | `wx_oa` | `wx_web` |

## 安全注意事项

1. **state 防 CSRF** — 网关用 JWT 签名 state，回调时验签，保证回调是微信官方发起的（而非攻击者伪造）
2. **code 一次性** — 微信的 code 使用后会立即失效，重复使用返回 40163
3. **redirect 验证** — state 中包含的 redirect 会被提取用于回跳，确保用户被重定向到受信地址
4. **JWT 24h 过期** — 前端应在 JWT 过期前刷新，或重新走授权流程
5. **HTTPS 必须** — 微信要求回调地址必须是 HTTPS（开发环境 localhost 除外）

## 桌面扫码登录（服务号方案，本项目实际采用）

### 为什么需要

服务号网页授权（`open.weixin.qq.com/connect/oauth2/authorize`，`scope=snsapi_userinfo`）
**只在微信内置浏览器里正常渲染**；桌面 Chrome/Firefox 直接打开会显示
「请在微信客户端打开链接」。所以 PC 端登录要走「弹窗显示二维码、手机扫码」的方式。

### 原理（只需一个服务号，不需要开放平台）

二维码的内容就是**同一个服务号网页授权 URL**。用户用手机微信扫码 → 在手机上确认授权 →
微信把**手机**的浏览器 302 到回调地址 → 回调用 `state`（=ticket）把该 ticket 标记为已授权 →
PC 端轮询 ticket 状态 → 拿到用户信息后绑定到 PC 自己的 `ff_sid` 会话。全程不出现开放平台。

```
PC浏览器                     BFF                          手机微信
  │  GET /api/wechat/qrlogin │                               │
  │ ───────────────────────>│ 返回 {ticket, auth_url}        │
  │ <───────────────────────│                               │
  │ 弹窗渲染 auth_url 二维码 │                               │
  │                            ┌── 扫码（手机打开 auth_url）─┘
  │                            │ 手机确认授权
  │                            └─── 微信 302 手机浏览器到
  │                              /api/wechat/callback?code=&state=<ticket>
  │  GET /api/wechat/qrlogin/status?ticket=..（每 2s 轮询）
  │ ───────────────────────>│  回调已把 ticket 置为 authorized
  │ <───────────────────────│  → 把用户绑定到当前 PC 会话，返回 authorized+user
```

### 端点

| 端点 | 说明 |
|------|------|
| `GET /api/wechat/qrlogin` | 创建登录 ticket，返回 `{ticket, auth_url, expires_in:300}`。`ticket` 同时作为 OAuth 的 `state` |
| `GET /api/wechat/qrlogin/status?ticket=...` | 轮询状态：`pending \| authorized \| expired \| invalid`。首次观察到 `authorized` 即把用户绑定到**当前 PC 会话**并消费 ticket |
| `GET /api/wechat/callback?code=..&state=<ticket>` | 在**手机端微信浏览器**执行。若 `state` 是 pending 的 ticket → 换 code→userinfo、把 ticket 置为 authorized，然后渲染「授权成功，请返回电脑」页。否则走原服务号重定向流程（校验 `ff_wx_state` cookie 防 CSRF） |

ticket 有效期 5 分钟，过期后状态返回 `expired`，前端引导用户点「刷新」重新拉取。

### 前端二维码渲染（已踩过的坑，勿再踩）

- ❌ `https://cdn.jsdelivr.net/npm/qrcode@1.5.3/build/qrcode.min.js` → **404**。
  node-qrcode 这个 npm 包没有 `build/` 产物，script 标签直接引不到。这是本项目踩过的坑。
- ✅ `https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js` → 200，暴露浏览器全局 `QRCode`（davidshimjs 实现）。
- API 是 `new QRCode(containerEl, {text, width, height, correctLevel})`，
  **不是** node-qrcode 的 `QRCode.toCanvas(canvas, url, {margin})`。
- 目标元素必须是**容器 div**：qrcodejs 总是向目标元素内 `appendChild` 一个**新 canvas**。
  直接传 `<canvas>` 会在 canvas 里再嵌一个 canvas，渲染不出来。
- 刷新/重渲染前先 `container.innerHTML = ''` 清空，否则每次 `new QRCode` 都叠加一个 canvas。
- 链接较长，`correctLevel` 用 `QRCode.CorrectLevel.H`（高纠错更稳）。

```html
<!-- 容器用 div，不是 canvas -->
<div class="qr-canvas-wrap" id="qr-canvas-wrap"></div>
<script src="https://cdn.jsdelivr.net/npm/qrcodejs@1.0.0/qrcode.min.js"></script>
```

```js
wrap.innerHTML = '';
new QRCode(wrap, { text: authUrl, width: 220, height: 220, correctLevel: QRCode.CorrectLevel.H });
```

### 同套登录踩过的其他坑

- **godotenv 行内注释 bug**：`.env` 里**值为空**的行不能写行尾 `# 注释`
  （解析器会先 trim 行首空白，`#` 落到下标 0，被当成值。实测 `WECHAT_APP_ID=` 后面
  接 `#TODO ...` 会把 `#TODO ...` 整段当 appid，授权 URL 变成 `appid=%23TODO...`）。
  注释一律写**单独一行**。`.env` 与 `.env.example` 已按此修正。
- **回调域名必须 HTTPS**，且经 nginx 反代时需带 `X-Forwarded-Proto: https`
  （BFF 的 `scheme()` 据此拼回调地址），否则拼出 http 回调被微信拒绝。
- 服务号后台「网页授权域名」要填回调域名，并放好对应 `MP_verify_*.txt` 校验文件（本项目在 `bff/web/` 下，已验证可访问）。

## 相关文档

- [微信开放平台文档](https://developers.weixin.qq.com/doc/oplatform/Website_App/WeChat_Login/Wechat_Login.html)
- 本项目 `api-doc.md` — 完整 API 手册
- 本项目 `CLAUDE.md` — 架构说明
