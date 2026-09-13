package handlers

import (
	"database/sql"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"sync"
	"time"

	"github.com/gin-gonic/gin"
)

// WechatLogin redirects the browser to WeChat's OAuth page (official-account
// flow). It only renders inside the WeChat client — desktop browsers must use
// the QR-login flow instead (see QrLoginCreate). The redirect_uri points back
// at this BFF's callback — the appSecret stays server-side and is never
// shipped to the browser. A short-lived state cookie is set for CSRF.
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

// ---- QR-login (desktop popup) ----
//
// The desktop flow shows a QR code whose content is the official-account OAuth
// URL. The user scans it with WeChat, authorizes on the phone, WeChat 302s the
// phone's WeChat browser to /api/wechat/callback?code=..&state=<ticket>, and the
// callback marks the ticket authorized. The desktop then polls the ticket
// status and, once authorized, binds the user to its own ff_sid session.
// No Open Platform account is required — a 服务号 with 网页授权 suffices.

type qrTicket struct {
	Status  string // "pending" | "authorized"
	User    map[string]interface{}
	Expires time.Time
}

// qrTickets is an in-memory hot cache for QR-login tickets. The cross-instance
// source of truth is the wechat_qr_tickets table (see internal/state/db.go);
// ticketGet/Set/Delete/MarkAuthorized write through to it when userDB is set
// (production / multi-instance). When userDB is nil (dev without a DB) the map
// alone is used so a local run needs no SQLite file.
var qrTickets = struct {
	sync.RWMutex
	m map[string]*qrTicket
}{m: make(map[string]*qrTicket)}

func qrDB() *sql.DB { return userDB }

func ticketGet(id string) (*qrTicket, bool) {
	qrTickets.RLock()
	t, ok := qrTickets.m[id]
	qrTickets.RUnlock()
	if ok {
		return t, true
	}
	if db := qrDB(); db != nil {
		row := db.QueryRow(`SELECT status, profile_json, expires_at FROM wechat_qr_tickets WHERE ticket_id=? LIMIT 1`, id)
		var status, profile, expires string
		if err := row.Scan(&status, &profile, &expires); err == nil {
			exp, perr := time.Parse(time.RFC3339, expires)
			if perr != nil {
				exp = time.Time{}
			}
			if time.Now().After(exp) {
				ticketDelete(id) // lazy expiry sweep, keeps the table small
				return nil, false
			}
			t = &qrTicket{Status: status, Expires: exp}
			if profile != "" {
				_ = json.Unmarshal([]byte(profile), &t.User)
			}
			qrTickets.Lock()
			qrTickets.m[id] = t
			qrTickets.Unlock()
			return t, true
		}
	}
	return nil, false
}

func ticketSet(id string, t *qrTicket) {
	qrTickets.Lock()
	qrTickets.m[id] = t
	qrTickets.Unlock()
	if db := qrDB(); db != nil {
		exp := t.Expires.Format(time.RFC3339)
		_, _ = db.Exec(`INSERT INTO wechat_qr_tickets(ticket_id,status,profile_json,created_at,expires_at)
			VALUES(?,?,?,?,?)
			ON CONFLICT(ticket_id) DO UPDATE SET status=excluded.status, profile_json=excluded.profile_json, expires_at=excluded.expires_at`,
			id, t.Status, "", time.Now().Format(time.RFC3339), exp)
	}
}

func ticketDelete(id string) {
	qrTickets.Lock()
	delete(qrTickets.m, id)
	qrTickets.Unlock()
	if db := qrDB(); db != nil {
		_, _ = db.Exec(`DELETE FROM wechat_qr_tickets WHERE ticket_id=?`, id)
	}
}

// ticketMarkAuthorized records a phone-side authorization (the callback runs in
// the phone's WeChat browser, possibly on a different BFF instance than the PC
// poll) so the desktop poll can observe the authorized state across instances.
func ticketMarkAuthorized(id string, user map[string]interface{}) {
	profile, _ := json.Marshal(user)
	qrTickets.Lock()
	if t, ok := qrTickets.m[id]; ok {
		t.Status = "authorized"
		t.User = user
	}
	qrTickets.Unlock()
	if db := qrDB(); db != nil {
		_, _ = db.Exec(`UPDATE wechat_qr_tickets SET status='authorized', profile_json=? WHERE ticket_id=?`, string(profile), id)
	}
}

// QrLoginCreate issues a login ticket and returns the OAuth URL to render as a
// QR code. The ticket id doubles as the OAuth `state`, so the callback can map
// a scanned authorization back to this PC session.
func (h *Handlers) QrLoginCreate(c *gin.Context) {
	if h.Cfg.WechatAppID == "" || h.Cfg.WechatAppSecret == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "wechat service account not configured on server"})
		return
	}
	redirectURI := h.Cfg.WechatRedirectURI
	if redirectURI == "" {
		redirectURI = fmt.Sprintf("%s://%s/api/wechat/callback", scheme(c), c.Request.Host)
	}
	ticket := randHex(16)
	ticketSet(ticket, &qrTicket{Status: "pending", Expires: time.Now().Add(5 * time.Minute)})

	authURL := fmt.Sprintf(
		"https://open.weixin.qq.com/connect/oauth2/authorize?appid=%s&redirect_uri=%s&response_type=code&scope=%s&state=%s#wechat_redirect",
		h.Cfg.WechatAppID,
		url.QueryEscape(redirectURI),
		h.Cfg.WechatScope,
		ticket,
	)
	c.JSON(http.StatusOK, gin.H{"ticket": ticket, "auth_url": authURL, "expires_in": 300})
}

// QrLoginStatus polls a QR-login ticket. Once the phone-side callback marks it
// authorized, the first poll that observes it binds the user to this browser's
// ff_sid session and returns the user profile.
func (h *Handlers) QrLoginStatus(c *gin.Context) {
	ticket := c.Query("ticket")
	if ticket == "" {
		c.JSON(http.StatusBadRequest, gin.H{"status": "invalid"})
		return
	}
	t, ok := ticketGet(ticket)
	if !ok {
		c.JSON(http.StatusOK, gin.H{"status": "invalid"})
		return
	}
	if time.Now().After(t.Expires) {
		ticketDelete(ticket)
		c.JSON(http.StatusOK, gin.H{"status": "expired"})
		return
	}
	if t.Status != "authorized" || t.User == nil {
		c.JSON(http.StatusOK, gin.H{"status": "pending"})
		return
	}
	// Bind the authorized user to this PC session and consume the ticket.
	sid := h.ensureSession(c)
	h.saveUser(sid, t.User)
	ticketDelete(ticket)
	c.JSON(http.StatusOK, gin.H{"status": "authorized", "user": t.User})
}

// WechatCallback exchanges the code for an access_token + openid, fetches the
// user profile, stores it against the BFF session cookie, then bounces back to
// the SPA with ?login=wechat so the frontend can call /api/me.
//
// It serves two flows, distinguished by the `state`:
//   - state == a pending QR-login ticket → desktop QR flow. The callback runs
//     in the phone's WeChat browser (no session cookie), so it marks the ticket
//     authorized and renders a "back to your computer" page.
//   - otherwise → in-WeChat redirect flow, guarded by the ff_wx_state cookie.
func (h *Handlers) WechatCallback(c *gin.Context) {
	code := c.Query("code")
	state := c.Query("state")
	if code == "" {
		c.JSON(http.StatusBadRequest, gin.H{"error": "missing code"})
		return
	}

	// Desktop QR-login path
	if t, ok := ticketGet(state); ok && t.Status == "pending" {
		h.exchangeAndAuthorize(c, code, func(user map[string]interface{}) {
			ticketMarkAuthorized(state, user)
		})
		if c.Writer.Written() {
			return
		}
		c.Header("Content-Type", "text/html; charset=utf-8")
		c.String(http.StatusOK, qrLoginSuccessHTML)
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

// exchangeAndAuthorize exchanges the code for a user profile and passes it to
// done. On any failure it writes the error response itself (so callers should
// check c.Writer.Written()).
func (h *Handlers) exchangeAndAuthorize(c *gin.Context, code string, done func(map[string]interface{})) {
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
	done(userInfo)
}

const qrLoginSuccessHTML = `<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>授权成功</title></head>
<body style="font-family:-apple-system,'PingFang SC',sans-serif;text-align:center;padding-top:20vh;background:#f7f7f7;margin:0">
<div style="width:88px;height:88px;margin:0 auto 20px;border-radius:50%;background:#07c160;color:#fff;font-size:48px;line-height:88px;">✓</div>
<h2 style="color:#07c160;margin:0 0 12px">授权成功</h2>
<p style="color:#666;font-size:15px">请返回电脑网页，稍候会自动进入。</p>
</body></html>`

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
	// Behind a TLS-terminating reverse proxy (nginx), c.Request.TLS is nil —
	// trust X-Forwarded-Proto so auto-derived callback URLs stay HTTPS.
	if proto := c.GetHeader("X-Forwarded-Proto"); proto == "https" {
		return "https"
	}
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
