"""Browser login and user-scoped project routes mounted beside MCP."""

from __future__ import annotations

import os
import time
from urllib.parse import urlparse

import requests
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

from .user_auth import UserAuthStore
from .wechat_web_auth import WeChatOfficialAccount, WeChatOAuthError


def _safe_return_to(value: str | None) -> str:
    value = value or "/web/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/web"):
        return "/web/"
    return value


def build_web_routes(store: UserAuthStore):
    wechat = WeChatOfficialAccount()
    cookie_name = os.environ.get("OPENMONTAGE_WEB_SESSION_COOKIE", "openmontage_session")
    secure = os.environ.get("OPENMONTAGE_WEB_COOKIE_SECURE", "true").lower() not in {"0", "false", "no"}

    def current_user(request: Request):
        return store.user_for_session(request.cookies.get(cookie_name))

    def public_user(user):
        return {key: user.get(key) for key in ("id", "provider", "display_name", "created_at")}

    async def home(request: Request):
        user = current_user(request)
        if user:
            return JSONResponse({"authenticated": True, "user": public_user(user), "projects_url": "/web/api/projects"})
        return HTMLResponse("""<!doctype html><meta charset='utf-8'><title>OpenMontage 登录</title>
        <h1>OpenMontage</h1><p>登录后，项目和素材只会显示在你的用户空间内。</p>
        <a href='/web/login/wechat'>使用微信登录</a>""")

    async def login_wechat(request: Request):
        return_to = _safe_return_to(request.query_params.get("return_to"))
        if not wechat.configured:
            return JSONResponse({"success": False, "error": "WeChat login is not configured", "required_env": ["WECHAT_MP_APP_ID", "WECHAT_MP_APP_SECRET", "WECHAT_MP_REDIRECT_URI"]}, status_code=503)
        state = store.create_oauth_state("wechat", return_to)
        return RedirectResponse(wechat.authorization_url(state), status_code=302)

    async def callback_wechat(request: Request):
        state = request.query_params.get("state", "")
        code = request.query_params.get("code", "")
        return_to = store.consume_oauth_state(state, "wechat")
        if not return_to or not code:
            return JSONResponse({"success": False, "error": "Invalid or expired WeChat login state"}, status_code=400)
        try:
            token = wechat.exchange_code(code)
            profile = {} if wechat.scope == "snsapi_base" else wechat.profile(token["access_token"], token["openid"])
            subject = profile.get("unionid") or token.get("unionid") or token["openid"]
            display_name = profile.get("nickname") or "微信用户"
            user = store.upsert_user("wechat", subject, display_name, {"openid": token["openid"], "unionid": profile.get("unionid") or token.get("unionid"), "headimgurl": profile.get("headimgurl")})
            session, expires = store.create_session(user["id"])
        except (KeyError, WeChatOAuthError, requests.RequestException, OSError, ValueError) as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=502)
        response = RedirectResponse(return_to, status_code=302)
        response.set_cookie(cookie_name, session, max_age=max(0, expires - int(time.time())), httponly=True, secure=secure, samesite="lax", path="/web")
        return response

    async def logout(request: Request):
        token = request.cookies.get(cookie_name)
        store.delete_session(token)
        response = RedirectResponse("/web/", status_code=302)
        response.delete_cookie(cookie_name, path="/web")
        return response

    async def me(request: Request):
        user = current_user(request)
        if not user:
            return JSONResponse({"authenticated": False}, status_code=401)
        return JSONResponse({"authenticated": True, "user": public_user(user)})

    async def projects(request: Request):
        user = current_user(request)
        if not user:
            return JSONResponse({"success": False, "error": "login required"}, status_code=401)
        root = store.user_projects_root(user["id"])
        if request.method == "POST":
            body = await request.json()
            project = store.ensure_project(user["id"], str(body.get("project_id", "")))
            return JSONResponse({"success": True, "project_id": project.name, "path": str(project.relative_to(store.projects_root))}, status_code=201)
        return JSONResponse({"success": True, "user_id": user["id"], "projects": store.list_projects(user["id"])})

    async def project_detail(request: Request):
        user = current_user(request)
        if not user:
            return JSONResponse({"success": False, "error": "login required"}, status_code=401)
        try:
            project = store.project_path(user["id"], request.path_params["project_id"])
        except ValueError as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)
        if not project.is_dir():
            return JSONResponse({"success": False, "error": "project not found"}, status_code=404)
        assets = []
        asset_root = project / "assets"
        if asset_root.exists():
            assets = [{"filename": item.name, "bytes": item.stat().st_size} for item in sorted(asset_root.iterdir()) if item.is_file()]
        renders = []
        render_root = project / "renders"
        if render_root.exists():
            renders = [{"filename": item.name, "bytes": item.stat().st_size} for item in sorted(render_root.iterdir()) if item.is_file()]
        return JSONResponse({"success": True, "project_id": project.name, "assets": assets, "renders": renders})

    async def upload_asset(request: Request):
        user = current_user(request)
        if not user:
            return JSONResponse({"success": False, "error": "login required"}, status_code=401)
        try:
            body = await request.json()
            asset = store.save_asset(user["id"], str(body.get("project_id", "")), str(body.get("filename", "")), str(body.get("content_base64", "")))
            return JSONResponse({"success": True, "asset": asset}, status_code=201)
        except (ValueError, TypeError, KeyError) as exc:
            return JSONResponse({"success": False, "error": str(exc)}, status_code=400)

    return [
        Route("/", home, methods=["GET"]),
        Route("/login/wechat", login_wechat, methods=["GET"]),
        Route("/callback/wechat", callback_wechat, methods=["GET"]),
        Route("/logout", logout, methods=["GET", "POST"]),
        Route("/api/me", me, methods=["GET"]),
        Route("/api/projects", projects, methods=["GET", "POST"]),
        Route("/api/projects/{project_id}", project_detail, methods=["GET"]),
        Route("/api/assets", upload_asset, methods=["POST"]),
    ]


def build_web_mount(store: UserAuthStore):
    return Mount("/web", routes=build_web_routes(store))
