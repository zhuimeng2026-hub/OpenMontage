# auth.aixifs.com API 调用说明

> 服务端口：**13080**，所有接口路径基于此。

## 已注册应用

| app_id | 认证方式 | 回调域名 |
|--------|---------|---------|
| `hotnews` | 微信服务号 | `https://hotnews.aixifs.com` |
| `dwqjk` | 企业微信 | `https://dwqjk.aixifs.com` |
| `babyphoto` | 微信服务号 | `https://babyphoto.aixifs.com` |
| `landscape` | 微信服务号 | `https://ocbot.aixifs.com` |
| `langbot` | 微信服务号 | `https://xchat.aixifs.com` |

---

## 一、OAuth 登录（浏览器跳转）

### 第 1 步：引导用户授权

```
GET /auth/{app_id}?redirect={回跳地址}
```

- `{app_id}` — 上表中的 app_id，如 `hotnews`
- `redirect` — 授权完成后的回跳地址，可以是相对路径 `/page` 或绝对 URL `https://hotnews.aixifs.com/page`

浏览器会被 302 到微信/企业微信授权页，用户扫码或确认授权。

### 第 2 步：回调自动回跳

微信回调 `/callback` 处理完毕后，浏览器 302 到：

```
{redirect}?token={JWT}&openid={openid}
```

- `token` — JWT，后续调用认证接口时用
- `openid` — 用户在该公众号/企业微信下的唯一标识

前端从 URL 参数取出 `token` 和 `openid` 保存（如 localStorage）。

---

## 二、静默登录（后端 / 小程序 code 换 token）

```
POST /api/auth/login
Content-Type: application/json

{
    "code": "微信 OAuth code",
    "app_id": "hotnews"
}
```

返回：
```json
{
    "code": 0,
    "token": "eyJhbG...",
    "openid": "oXXXX",
    "user_id": 1
}
```

---

## 三、开放 API（无需认证）

### 创建订单

```
POST /api/orders
Content-Type: application/json

{
    "openid": "oXXXX",
    "app_id": "hotnews",
    "plan_code": "vip_monthly",
    "extra": "{}"
}
```

返回：
```json
{
    "order": {
        "id": 1,
        "orderNo": "SP20260510213001abc12345",
        "outTradeNo": "SP20260510213001abc12345",
        "amountFen": 100,
        "status": "pending",
        "planCode": "vip_monthly",
        "planName": "月度会员",
        "appId": "hotnews"
    }
}
```

### 发起支付

```
POST /api/pay/checkout
Content-Type: application/json

{
    "app_id": "hotnews",
    "openid": "oXXXX",
    "orderNo": "SP20260510213001abc12345",
    "outTradeNo": "SP20260510213001abc12345",
    "description": "月度会员",
    "totalFee": 100,
    "planCode": "vip_monthly",
    "extra": "{}"
}
```

返回微信 JSAPI/H5 支付参数（调起微信支付）。

### 支付回调（微信调用，调用方无需关心）

```
POST /api/pay/notify
```

---

## 四、认证 API（需要 JWT）

所有接口请求头需携带：`Authorization: Bearer {token}`

### 获取用户信息

```
GET /api/user/me
```
返回：
```json
{
    "code": 0,
    "user": {
        "id": 1,
        "nickname": "",
        "avatar_url": "",
        "openid": "oXXXX",
        "app_id": "hotnews"
    }
}
```

### 更新用户信息

```
PUT /api/user/me
Content-Type: application/json

{
    "nickname": "新昵称",
    "avatar_url": "https://example.com/avatar.jpg"
}
```

### 获取用户订单

```
GET /api/user/orders
```

### 获取当前应用套餐列表

```
GET /api/plans
```
返回该 app 下所有可用套餐。

---

## 五、健康检查

```
GET /health
→ {"status": "ok"}
```

---

## 典型调用流程

```
1. POST /api/auth/login        → 拿到 token + openid
2. GET  /api/plans              → 拿到套餐列表（JW金认证）
3. POST /api/orders             → 创建订单（无需认证，传 openid + app_id）
4. POST /api/pay/checkout       → 拿支付参数
5. 前端调起微信支付
6. 微信异步回调 POST /api/pay/notify → 订单状态变为 paid
7. GET  /api/user/orders        → 确认订单已支付（JW金认证）
```
