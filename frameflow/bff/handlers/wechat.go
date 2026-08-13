package handlers

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"

	"github.com/gin-gonic/gin"
)

// WechatLogin redirects the browser to WeChat's OAuth page. The redirect_uri
// points back at this BFF's callback — the appSecret stays server-side and is
// never shipped to the browser. A short-lived state cookie is set for CSRF.
func (h *Handlers) WechatLogin(c *gin.Context) {
	if h.Cfg.WechatAppID == "" || h.Cfg.WechatAppSecret == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "wechat service account not configured on server"})
		return
	}
	redirectURI := h.Cfg.WechatRedirectURI
	if redirectURI == "" {
		redirectURI = fmt.Sprintf("%s://%s/api/wechat/callback", scheme(c), c.Request.Host)
	}
	state := randHex(16)
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie("ff_wx_state", state, 300, "/", "", h.Cfg.SessionSecure, true)

	authURL := fmt.Sprintf(
		"https://open.weixin.qq.com/connect/oauth2/authorize?appid=%s&redirect_uri=%s&response_type=code&scope=%s&state=%s#wechat_redirect",
		h.Cfg.WechatAppID,
		url.QueryEscape(redirectURI),
		h.Cfg.WechatScope,
		state,
	)
	c.Redirect(http.StatusFound, authURL)
}

// WechatCallback exchanges the code for an access_token + openid, fetches the
// user profile, stores it against the BFF session cookie, then bounces back to
// the SPA with ?login=wechat so the frontend can call /api/me.
func (h *Handlers) WechatCallback(c *gin.Context) {
	code := c.Query("code")
	state := c.Query("state")
	if code == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing code"})
		return
	}
	if expected, _ := c.Cookie("ff_wx_state"); expected == "" || expected != state {
		c.JSON(http.StatusBadRequest, gin.H{"error": "state mismatch"})
		return
	}
	if h.Cfg.WechatAppID == "" || h.Cfg.WechatAppSecret == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "wechat not configured"})
		return
	}

	tokenURL := fmt.Sprintf(
		"https://api.weixin.qq.com/sns/oauth2/access_token?appid=%s&secret=%s&code=%s&grant_type=authorization_code",
		h.Cfg.WechatAppID, h.Cfg.WechatAppSecret, code,
	)
	tok, err := httpGetJSON(tokenURL)
	if err != nil {
		c.JSON(http.StatusBadGateway, gin.H{"error": "exchange token failed: " + err.Error()})
		return
	}
	accessToken, _ := tok["access_token"].(string)
	openid, _ := tok["openid"].(string)
	if accessToken == "" || openid == "" {
		c.JSON(http.StatusBadGateway, gin.H{"error": "wechat token response missing fields", "detail": tok})
		return
	}

	userInfo := map[string]interface{}{"openid": openid}
	if h.Cfg.WechatScope == "snsapi_userinfo" {
		infoURL := fmt.Sprintf("https://api.weixin.qq.com/sns/userinfo?access_token=%s&openid=%s&lang=zh_CN", accessToken, openid)
		if info, err := httpGetJSON(infoURL); err == nil {
			userInfo = info
		}
	}

	sid := h.ensureSession(c)
	h.saveUser(sid, userInfo)

	// clear the one-time state cookie
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie("ff_wx_state", "", -1, "/", "", h.Cfg.SessionSecure, true)

	c.Redirect(http.StatusFound, "/?login=wechat")
}

// Me reports the current logged-in user (or authenticated:false).
func (h *Handlers) Me(c *gin.Context) {
	sid, err := c.Cookie(sessionCookieName)
	if err != nil || sid == "" {
		c.JSON(http.StatusOK, gin.H{"authenticated": false})
		return
	}
	if u := h.loadUser(sid); u != nil {
		c.JSON(http.StatusOK, gin.H{"authenticated": true, "user": u})
		return
	}
	c.JSON(http.StatusOK, gin.H{"authenticated": false})
}

// Logout clears the session.
func (h *Handlers) Logout(c *gin.Context) {
	if sid, err := c.Cookie(sessionCookieName); err == nil && sid != "" {
		h.dropUser(sid)
	}
	c.SetSameSite(http.SameSiteLaxMode)
	c.SetCookie(sessionCookieName, "", -1, "/", "", h.Cfg.SessionSecure, true)
	c.JSON(http.StatusOK, gin.H{"ok": true})
}

func scheme(c *gin.Context) string {
	if c.Request.TLS != nil {
		return "https"
	}
	return "http"
}

func httpGetJSON(u string) (map[string]interface{}, error) {
	resp, err := http.Get(u)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}
	var m map[string]interface{}
	if err := json.Unmarshal(body, &m); err != nil {
		return nil, err
	}
	return m, nil
}
