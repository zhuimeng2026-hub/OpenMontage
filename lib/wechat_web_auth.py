"""WeChat Official Account webpage OAuth client (snsapi_base/userinfo)."""

from __future__ import annotations

import os
from urllib.parse import urlencode

import requests


class WeChatOAuthError(RuntimeError):
    pass


class WeChatOfficialAccount:
    def __init__(self) -> None:
        self.app_id = os.environ.get("WECHAT_MP_APP_ID", "").strip()
        self.app_secret = os.environ.get("WECHAT_MP_APP_SECRET", "").strip()
        self.redirect_uri = os.environ.get("WECHAT_MP_REDIRECT_URI", "").strip()
        self.scope = os.environ.get("WECHAT_MP_SCOPE", "snsapi_userinfo").strip()
        self.timeout = float(os.environ.get("WECHAT_MP_HTTP_TIMEOUT", "10"))

    @property
    def configured(self) -> bool:
        return bool(self.app_id and self.app_secret and self.redirect_uri)

    def authorization_url(self, state: str) -> str:
        if not self.configured:
            raise WeChatOAuthError("WeChat webpage OAuth is not configured")
        query = urlencode({"appid": self.app_id, "redirect_uri": self.redirect_uri, "response_type": "code", "scope": self.scope, "state": state})
        return f"https://open.weixin.qq.com/connect/oauth2/authorize?{query}#wechat_redirect"

    def exchange_code(self, code: str) -> dict:
        if not self.configured:
            raise WeChatOAuthError("WeChat webpage OAuth is not configured")
        response = requests.get(
            "https://api.weixin.qq.com/sns/oauth2/access_token",
            params={"appid": self.app_id, "secret": self.app_secret, "code": code, "grant_type": "authorization_code"},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errcode"):
            raise WeChatOAuthError(f"WeChat token exchange failed: {data.get('errmsg', data['errcode'])}")
        return data

    def profile(self, access_token: str, openid: str, lang: str = "zh_CN") -> dict:
        response = requests.get(
            "https://api.weixin.qq.com/sns/userinfo",
            params={"access_token": access_token, "openid": openid, "lang": lang},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("errcode"):
            raise WeChatOAuthError(f"WeChat profile lookup failed: {data.get('errmsg', data['errcode'])}")
        return data
