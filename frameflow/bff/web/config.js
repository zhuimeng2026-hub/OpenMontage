/**
 * 帧流 FrameFlow — 前端运行配置
 * ------------------------------------------------------------
 * 请把下方「微信服务号」相关参数补充完整后，再启用正式登录。
 * 全部留空时，登录页会自动降级为「演示模式」，方便评审与开发。
 *
 * 注意：
 *  - appSecret / token / encodingAESKey 属于敏感凭证，正式环境应只存在于
 *    你的后端（BFF / 网关），切勿直接下发到浏览器前端。
 *  - 微信网页授权（OAuth2）的完整流程：前端带 appId 跳转微信 → 微信回调
 *    redirectUri?code=xxx → 后端用 code + appSecret 换 access_token/openid。
 */
window.FF_CONFIG = {
  // —— 微信服务号（公众号）网页授权登录 ——
  wechat: {
    appId: "",                     // 公众号 AppID，例如 wx1234567890abcdef
    appSecret: "",                 // 公众号 AppSecret（仅服务端使用，切勿暴露到前端）
    redirectUri: "",               // 回调地址，需与公众号后台「网页授权域名」一致（需 URL encode）
    scope: "snsapi_userinfo",      // snsapi_base（静默授权）或 snsapi_userinfo（获取昵称头像）
    state: "frameflow",            // 防 CSRF 随机串，正式环境应由后端生成并校验

    // 以下为公众号后台「基本配置」项，纯服务端使用（消息签名校验 / 安全模式加解密）
    token: "",                     // 服务器配置 Token
    encodingAESKey: ""             // 消息加解密密钥（开启安全模式时必填）
  },

  // —— Remotion 渲染服务（MCP）——
  // 前端【不直接】持有 MCP_API_TOKEN，必须经自建 BFF / 网关转发（见接入说明）。
  remotion: {
    // MCP and SSE URLs are server-side implementation details. The browser
    // always talks to the BFF, which is same-origin in production and local
    // hosts-based development.
    mcpUrl: "",
    progressUrl: "",
    // 你的 BFF 网关地址；前端统一走这里，由它持有 MCP_API_TOKEN。
    // 填了即切出「演示骨架」进入真实调用。建议与前端同源部署（即 BFF 自己托管 SPA），
    // 此时填 BFF 自身域名即可，例如 http://localhost:8080 或 https://bff.example.com。
    bffBaseUrl: window.location.origin
  }
};
