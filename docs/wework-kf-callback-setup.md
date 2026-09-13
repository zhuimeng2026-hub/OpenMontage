# 企业微信客服回调配置指南

## 概述

wechat-auth-pay 新增企业微信客服回调功能，接收用户消息后调用 LLM 自动回复。

回调端点：`GET/POST /wework/kf/callback`

## 一、环境变量配置

在 `/opt/wechat-auth-pay/.env` 中添加：

```bash
# 企业微信客服回调
WXWORK_CORP_ID=wwf387d1b1d38bcae2              # 企业微信 corpID
WXWORK_AGENT_SECRET=xxx                          # 自建应用 Secret（用于获取 access_token）
WXWORK_KF_CALLBACK_TOKEN=xxx                     # 自建应用「接收消息」的 Token
WXWORK_KF_CALLBACK_AESKEY=xxx                    # 自建应用「接收消息」的 EncodingAESKey（43字符）
WXWORK_KF_ID=kfc811ddf5948b6771e                 # 客服账号的 open_kfid（从 https://work.weixin.qq.com/kfid/xxx 获取）

# LLM 自动回复
LLM_BASE_URL=https://aikey.aixifs.com/v1
LLM_API_KEY=xxx
LLM_MODEL=deepseek-v4-flash
LLM_SYSTEM_PROMPT=你是一个友好的客服助手，请用简洁清晰的中文回答用户的问题。
```

Token/AESKey 来源：`~/.hermes/config.yaml` 中 `platforms.wecom_callback.apps[].token` 和 `encoding_aes_key`。

## 二、Nginx 配置

在 `/etc/nginx/sites-available/auth.conf` 中添加：

```nginx
# 企业微信客服回调（GET 验证 + POST 消息）
location = /wework/kf/callback {
    proxy_pass http://127.0.0.1:13080/wework/kf/callback;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

```bash
nginx -t && systemctl reload nginx
```

## 三、企业微信后台配置

1. 登录 https://work.weixin.qq.com/wework_admin/frame
2. **应用管理** → **自建应用**（agentid=1000011）
3. 找到「**接收消息**」→ 点「**设置API接收**」
4. 填写：
   - URL: `https://auth.aixifs.com/wework/kf/callback`
   - Token: 与 `.env` 中 `WXWORK_KF_CALLBACK_TOKEN` 一致
   - EncodingAESKey: 与 `.env` 中 `WXWORK_KF_CALLBACK_AESKEY` 一致
5. 点击保存，企业微信发 GET 验证请求

## 四、踩坑记录

### 4.1 签名验证必须包含 echostr

企业微信 GET 验证的签名计算包含 `echostr`，而 POST 消息的签名不包含。

```
GET  签名 = SHA1(sort([token, timestamp, nonce, echostr]))
POST 签名 = SHA1(sort([token, timestamp, nonce]))
```

原始代码只用了 3 个参数，导致 GET 验证始终返回 403。

### 4.2 echostr 需要 AES 解密

验证通过后，不能直接返回 `echostr`，需要先用 EncodingAESKey 解密：

1. `AESKey = base64_decode(EncodingAESKey + "=")`（43字符 + "=" = 44字符 base64 → 32字节）
2. `IV = AESKey[0:16]`
3. AES-256-CBC 解密
4. 去除 PKCS7 padding
5. 明文格式：`random(16字节) + msg_len(4字节, big-endian) + msg + corp_id`

## 五、调试命令

```bash
# 查看服务日志（实时）
journalctl -u wechat-auth-pay -f | grep kf

# 手动测试 GET 验证（替换参数）
curl -s "https://auth.aixifs.com/wework/kf/callback?msg_signature=xxx&timestamp=xxx&nonce=xxx&echostr=xxx"

# 检查服务状态
systemctl status wechat-auth-pay --no-pager -l

# 重启服务（修改 .env 后）
systemctl restart wechat-auth-pay
```

## 六、关键文件

| 文件 | 说明 |
|---|---|
| `handler/wework_kf.go` | 客服回调处理（验签、解密、LLM 调用、消息回复） |
| `config.go` | 环境变量解析（WxWorkKFCfg / LLMCfg） |
| `main.go` | 路由注册：`GET/POST /wework/kf/callback` |
| `/opt/wechat-auth-pay/.env` | 运行时配置 |
| `/etc/nginx/sites-available/auth.conf` | Nginx 反向代理 |
| `~/.hermes/config.yaml` | Token/AESKey/CorpSecret 来源 |
