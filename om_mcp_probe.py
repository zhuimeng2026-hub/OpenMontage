#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenMontage MCP 探测 / 复测工具
================================

用途
----
对 OpenMontage 的 Streamable-HTTP MCP 端点做黑盒探测与一键复测，专门处理两件事：
  1. 服务端每台响应会**轮换 Mcp-Session-Id**，必须在每次响应后重新读取并回带，
     否则后续调用报 "Streamable HTTP Mcp-Session-Id is required"。
  2. Windows schannel / 网关偶发 TLS 握手抖动（curl exit 35），已内置 curl 层重试。

子命令
------
  init            仅握手（initialize + notifications/initialized），打印最终 SID
  list            列出全部工具名与数量
  call NAME JSON  调用任意工具，JSON 为 arguments（默认 {}）
  upload FILE -p PROJECT
                  上传资产（upload_asset），打印返回的服务器路径 asset.path
  chunkupload FILE -p PROJECT
                  分块上传高清大图（upload_asset_chunk start/append/complete），
                  自动按二进制切片，绕过 nginx 1MiB 单请求限制，打印 asset.path
  share -d DIR | -f FILE
                  调用 weiyun_gen_share_link（file_list / dir_list），打印 short_url

微信登录调试（BFF，生产环境黑盒复现登录链路）
--------------------------------------------
  wechat-config   探测 /api/wechat/qrlogin，判断微信服务号是否已配置
  me              查询当前会话 /api/me（配合 --cookie-jar 携带 ff_sid）
  qr-create       创建扫码票据并打印 auth_url（手机微信扫码授权）
  qr-status       查询票据状态（--ticket）
  qr-wait         端到端：创建票据→轮询→手机授权→校验 me（--timeout 秒）
  cookie-check    检查 ff_sid 的 Set-Cookie 属性（--headers 抓包头，或读 qr-wait 产物）
  login-flow      完整链路：创建票据→扫码→授权→校验 me

环境变量
--------
  OM_MCP_URL     端点（默认 https://dw.aixifs.com/mcp）
  OM_MCP_TOKEN   Bearer token（优先级高于 MCP_API_TOKEN）

示例
----
  python om_mcp_probe.py list
  python om_mcp_probe.py upload "C:/path/45.jpg" -p mclaw-demo
  python om_mcp_probe.py share -d /opt/OpenMontage/renders
  python om_mcp_probe.py call weiyun_gen_share_link '{"dir_list":["/opt/OpenMontage/renders"]}'
"""

import argparse
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

DEFAULT_URL = "https://dw.aixifs.com/mcp"
DEFAULT_TOKEN = ""

LOG = logging.getLogger("om_mcp_probe")


def setup_logging(log_path: str = "om_mcp_probe.log", quiet: bool = False) -> None:
    """同时输出到文件（落日志便于复盘）与控制台。"""
    LOG.setLevel(logging.DEBUG)
    LOG.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    LOG.addHandler(fh)
    if not quiet:
        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(fmt)
        LOG.addHandler(sh)


class MCPClient:
    def __init__(self, url: str, token: str, max_retries: int = 6):
        self.url = url
        self.token = token
        self.sid = None
        self._id = 0
        self.max_retries = max_retries

    # ---- transport -------------------------------------------------------
    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _request(self, payload: dict):
        hdrs = [
            "Content-Type: application/json",
            "Accept: application/json, text/event-stream",
            f"Authorization: Bearer {self.token}",
        ]
        if self.sid:
            hdrs.append(f"Mcp-Session-Id: {self.sid}")

        hf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".hdr")
        bf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".body")
        pf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".json", encoding="utf-8")
        hf.close()
        bf.close()
        # 请求体写入临时文件，用 curl -d @file 读取：
        # 避免把大 base64（分块上传每片 ~533KB）作为命令行参数触发
        # Windows [WinError 206] 文件名或扩展名太长（命令行长度上限）。
        with open(pf.name, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        pf.close()
        cmd = [
            "curl", "-sS", "--max-time", "600",
            "-D", hf.name, "-o", bf.name,
        ]
        for h in hdrs:
            cmd += ["-H", h]
        cmd += ["-d", f"@{pf.name}", self.url]

        last_err = None
        try:
            for attempt in range(1, self.max_retries + 1):
                try:
                    subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=620)
                    # 1) 从响应头轮换/更新 SID
                    with open(hf.name, encoding="utf-8", errors="replace") as f:
                        htext = f.read()
                    for line in htext.splitlines():
                        if line.lower().startswith("mcp-session-id:"):
                            self.sid = line.split(":", 1)[1].strip()
                    # 2) 解析 body（兼容纯 JSON 与 SSE "data:" 行）
                    with open(bf.name, encoding="utf-8", errors="replace") as f:
                        raw = f.read()
                    return self._parse(raw)
                except subprocess.CalledProcessError as e:
                    # 超时（curl exit 28）不要重试：服务端长任务（如 Remotion 渲染）
                    # 可能仍在跑，重发会导致重复提交（例如二次 begin_render）。
                    if e.returncode == 28:
                        raise RuntimeError(
                            "MCP 请求超时（curl --max-time 已达上限）。服务端长任务可能仍在运行，请勿重发。"
                        )
                    last_err = f"curl exit {e.returncode}"
                except Exception as e:  # noqa: BLE001
                    last_err = str(e)
                time.sleep(1)
        finally:
            for fp in (hf.name, bf.name, pf.name):
                try:
                    os.remove(fp)
                except OSError:
                    pass
        raise RuntimeError(f"MCP 请求失败（已重试 {self.max_retries} 次）：{last_err}")

    @staticmethod
    def _parse(raw: str):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            pass
        data_lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
        if data_lines:
            try:
                return json.loads(data_lines[-1])
            except Exception:
                return {"_raw": data_lines[-1]}
        return {"_raw": raw[:500]}

    # ---- high level ------------------------------------------------------
    def initialize(self):
        self._request({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05", "capabilities": {},
                "clientInfo": {"name": "om_probe", "version": "1.0.0"},
            },
        })
        # notifications/initialized 可能触发 SID 轮换，必须发送并重新读取
        self._request({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return self.sid

    def list_tools(self) -> list:
        resp = self._request({"jsonrpc": "2.0", "id": self._next_id(), "method": "tools/list"})
        tools = (resp or {}).get("result", {}).get("tools", [])
        return tools

    def call(self, name: str, arguments: dict | None = None) -> dict:
        return self._request({
            "jsonrpc": "2.0", "id": self._next_id(), "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })

    def chunk_upload(self, path: str, project: str, chunk: int = 400_000) -> dict:
        """分块上传高清大图（绕过 nginx 1MiB 单请求限制）。

        upload_asset_chunk 协议：start -> append(若干) -> complete。
        关键点：按**二进制字节**切片后各自 base64，offset 用二进制偏移，
        否则服务端只收到第一片（实测 375000/1238828 即此坑）。
        """
        import base64
        with open(path, "rb") as f:
            data = f.read()
        n = len(data)
        sha = hashlib.sha256(data).hexdigest()
        mime = "image/png" if path.lower().endswith(".png") else "image/jpeg"
        safe = os.path.basename(path)
        safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in safe)
        if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            safe += ".png"

        LOG.info("chunk_upload start: %s (%d bytes, chunk=%d)", safe, n, chunk)
        r = self.call("upload_asset_chunk", {
            "operation": "start", "project_id": project, "filename": safe,
            "total_bytes": n, "mime_type": mime, "sha256": sha,
        })
        info = self.extract(r)
        uid = (info or {}).get("upload_id")
        if not uid:
            raise RuntimeError(f"chunk start 失败：{info}")
        LOG.info("  upload_id=%s", uid)

        offset = 0
        while offset < n:
            piece = data[offset:offset + chunk]
            cb64 = base64.b64encode(piece).decode()
            r = self.call("upload_asset_chunk", {
                "operation": "append", "project_id": project, "filename": safe,
                "upload_id": uid, "offset": offset, "chunk_base64": cb64,
            })
            info = self.extract(r)
            if not (info or {}).get("success"):
                raise RuntimeError(f"chunk append@{offset} 失败：{info}")
            offset += len(piece)
            LOG.debug("  appended %d/%d", offset, n)

        r = self.call("upload_asset_chunk", {
            "operation": "complete", "project_id": project,
            "filename": safe, "upload_id": uid,
        })
        info = self.extract(r)
        LOG.info("chunk_upload complete: %s", json.dumps(info, ensure_ascii=False)[:300])
        return info

    @staticmethod
    def extract(resp):
        """从 tools/call 的 result.content[].text 中取出结构化结果。"""
        if not resp:
            return None
        if "result" in resp and "content" in resp["result"]:
            txt = "".join(x.get("text", "") for x in resp["result"]["content"])
            try:
                return json.loads(txt)
            except Exception:
                return txt
        if "error" in resp:
            return resp["error"]
        return resp


def _b64_path(path: str) -> str:
    import base64
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()


# ---------------------------------------------------------------------------
# BFF 微信登录调试（生产环境黑盒复现整条登录链路）
# ---------------------------------------------------------------------------

class BFFClient:
    """薄封装 FrameFlow BFF 的登录相关 HTTP 接口，重点是 cookie jar 持久化：
    让 wechat-config / me / qr-* / cookie-check 等子命令能跨调用共享 ff_sid。
    用显式 Cookie 头回带，避免依赖 curl 的 jar 自动合并（localhost 下常见坑）。
    """

    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.cookies = {}            # name -> value（用于回带 Cookie 头）
        self.last_set_cookie = []    # 最近一次响应的 Set-Cookie 原始串

    def load_jar(self, path: str):
        """读取 Netscape 格式 cookie jar。"""
        if not path or not os.path.exists(path):
            return
        for line in open(path, encoding="utf-8"):
            line = line.rstrip("\n")
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7 and not parts[0].startswith("#"):
                self.cookies[parts[-2]] = parts[-1]
            elif "=" in line and "\t" not in line:
                k, v = line.split("=", 1)
                self.cookies[k.strip()] = v.strip()

    def save_jar(self, path: str):
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Netscape HTTP Cookie File\n")
            for k, v in self.cookies.items():
                f.write(f"render.mengxa.com\tFALSE\t/\tFALSE\t0\t{k}\t{v}\n")

    @staticmethod
    def _parse_set_cookie(htext: str):
        out = []
        for line in htext.splitlines():
            if line.lower().startswith("set-cookie:"):
                out.append(line.split(":", 1)[1].strip())
        return out

    def _cookie_header(self) -> str:
        return "; ".join(f"{k}={v}" for k, v in self.cookies.items())

    def get(self, path: str, params=None, timeout: int = 30):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        hf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".hdr")
        bf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".body")
        hf.close()
        bf.close()
        cmd = ["curl", "-sS", "--max-time", str(timeout), "-D", hf.name, "-o", bf.name]
        if self.cookies:
            cmd += ["-H", f"Cookie: {self._cookie_header()}"]
        cmd.append(url)
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=timeout + 20)
            with open(hf.name, encoding="utf-8", errors="replace") as f:
                htext = f.read()
            self.last_set_cookie = self._parse_set_cookie(htext)
            for sc in self.last_set_cookie:
                m = re.match(r"([^=]+)=([^;]*)", sc)
                if m:
                    self.cookies[m.group(1).strip()] = m.group(2).strip()
            with open(bf.name, encoding="utf-8", errors="replace") as f:
                raw = f.read()
            return MCPClient._parse(raw)
        finally:
            for fp in (hf.name, bf.name):
                try:
                    os.remove(fp)
                except OSError:
                    pass


def _write_set_cookie_file(set_cookie_lines):
    """把最近一次响应的 Set-Cookie 落盘，供 cookie-check 离线分析。"""
    try:
        with open("om_mcp_setcookie.txt", "w", encoding="utf-8") as f:
            for ln in (set_cookie_lines or []):
                f.write("Set-Cookie: " + ln + "\n")
    except OSError:
        pass


def _poll_qr(bff: BFFClient, ticket: str, timeout: int = 300):
    """轮询扫码票据状态，直到 authorized / invalid / expired / 超时。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = bff.get("/api/wechat/qrlogin/status", params={"ticket": ticket})
        status = (info or {}).get("status")
        elapsed = int(time.time() - (deadline - timeout))
        print("  poll status=%s elapsed=%ds" % (status, elapsed))
        if status == "authorized":
            _write_set_cookie_file(bff.last_set_cookie)
            return 0
        if status in ("invalid", "expired"):
            print("QR_TERMINATED status=%s" % status)
            return 1
        time.sleep(3)
    print("QR_WAIT_TIMEOUT")
    return 1


def cmd_bff_wechat_config(bff: BFFClient):
    info = bff.get("/api/wechat/qrlogin")
    if not isinstance(info, dict):
        print("BFF 无响应或返回非 JSON：%r" % (info,))
        return 1
    if "error" in info and "not configured" in str(info["error"]):
        print("WECHAT_CONFIGURED = false")
        print("ERROR = %s" % info["error"])
        return 0
    if "auth_url" in info:
        print("WECHAT_CONFIGURED = true")
        auth_url = info["auth_url"]
        print("AUTH_URL = %s" % auth_url)
        q = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
        appid = q.get("appid", [""])[0]
        redir = q.get("redirect_uri", [""])[0]
        print("APPID = %s" % appid)
        print("REDIRECT_URI = %s" % redir)
        host = urllib.parse.urlparse(redir).hostname
        print("REDIRECT_HOST_OK = %s" % (str(host == "render.mengxa.com").lower()))
        if host != "render.mengxa.com":
            print("  [WARN] redirect_uri 域名应为 render.mengxa.com")
        return 0
    print("WECHAT_CONFIGURED = unknown")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_bff_me(bff: BFFClient):
    info = bff.get("/api/me")
    print("ME = %s" % json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_bff_qr_create(bff: BFFClient):
    info = bff.get("/api/wechat/qrlogin")
    if not isinstance(info, dict) or "ticket" not in info:
        print("QR_CREATE_FAILED = %s" % json.dumps(info, ensure_ascii=False))
        return 1
    print("TICKET = %s" % info["ticket"])
    print("EXPIRES_IN = %s" % info.get("expires_in"))
    print("AUTH_URL = %s" % info.get("auth_url"))
    print("请使用微信扫描以下地址生成的二维码（手机微信内可直接打开 AUTH_URL 授权）：")
    print(info.get("auth_url"))
    return 0


def cmd_bff_qr_status(bff: BFFClient, ticket: str):
    info = bff.get("/api/wechat/qrlogin/status", params={"ticket": ticket})
    print("STATUS = %s" % json.dumps(info, ensure_ascii=False))
    return 0


def cmd_bff_qr_wait(bff: BFFClient, timeout: int = 300):
    c = bff.get("/api/wechat/qrlogin")
    if not isinstance(c, dict) or "ticket" not in c:
        print("QR_CREATE_FAILED = %s" % json.dumps(c, ensure_ascii=False))
        return 1
    print("TICKET = %s" % c["ticket"])
    print("请使用微信扫描以下地址生成的二维码：")
    print(c.get("auth_url"))
    rc = _poll_qr(bff, c["ticket"], timeout)
    if rc == 0:
        me = bff.get("/api/me")
        print("ME_AFTER_LOGIN = %s" % json.dumps(me, ensure_ascii=False))
    return rc


def cmd_bff_cookie_check(headers_path: str):
    lines = []
    src = headers_path or "om_mcp_setcookie.txt"
    if os.path.exists(src):
        lines = open(src, encoding="utf-8").read().splitlines()
    else:
        print("未找到 Set-Cookie 来源：请先用 --headers 指定 curl -D 抓包头文件，")
        print("或先运行 qr-wait 自动生成 om_mcp_setcookie.txt。")
        return 1
    target = None
    for ln in lines:
        body = ln.split(":", 1)[1].strip() if ln.lower().startswith("set-cookie:") else ln.strip()
        if body.startswith("ff_sid"):
            target = body
            break
    if not target:
        print("未在来源中找到 ff_sid 的 Set-Cookie。")
        return 1
    parts = target.split(";")
    nameval = parts[0]
    attrs = {}
    for a in parts[1:]:
        if "=" in a:
            k, v = a.split("=", 1)
            attrs[k.strip().lower()] = v.strip()
        else:
            attrs[a.strip().lower()] = ""
    print("COOKIE = %s" % nameval)
    checks = [
        ("Secure", attrs.get("secure", "") != ""),
        ("HttpOnly", attrs.get("httponly", "") != ""),
        ("SameSite∈{lax,strict,none}", attrs.get("samesite", "").lower() in ("lax", "strict", "none")),
        ("Path=/", attrs.get("path", "") == "/"),
    ]
    for k, ok in checks:
        print("  %s: %s" % (k, "OK" if ok else "WARN"))
        if not ok:
            print("    [WARN] 生产环境建议补齐该属性")
    print("Max-Age = %s" % attrs.get("max-age", "<none>"))
    print("建议：SESSION_SECURE=true 时 Set-Cookie 必须带 Secure，否则 HTTPS 下浏览器不下发 cookie。")
    return 0


def cmd_bff_login_flow(bff: BFFClient, timeout: int = 300):
    print("== 1/3 创建扫码票据 ==")
    c = bff.get("/api/wechat/qrlogin")
    if not isinstance(c, dict) or "ticket" not in c:
        print("FAILED create: %s" % json.dumps(c, ensure_ascii=False))
        return 1
    print("TICKET = %s" % c["ticket"])
    print("请使用微信扫描以下地址生成的二维码：")
    print(c.get("auth_url"))
    print("== 2/3 等待手机授权 ==")
    if _poll_qr(bff, c["ticket"], timeout) != 0:
        return 1
    print("== 3/3 校验 /api/me ==")
    me = bff.get("/api/me")
    ok = bool((me or {}).get("authenticated"))
    print("LOGIN_FLOW_OK = %s" % ok)
    print(json.dumps(me, ensure_ascii=False, indent=2))
    return 0 if ok else 1


def run_bff(args, bff: BFFClient):
    if args.cmd == "wechat-config":
        return cmd_bff_wechat_config(bff)
    if args.cmd == "me":
        return cmd_bff_me(bff)
    if args.cmd == "qr-create":
        return cmd_bff_qr_create(bff)
    if args.cmd == "qr-status":
        return cmd_bff_qr_status(bff, args.ticket)
    if args.cmd == "qr-wait":
        return cmd_bff_qr_wait(bff, args.timeout)
    if args.cmd == "cookie-check":
        return cmd_bff_cookie_check(args.headers)
    if args.cmd == "login-flow":
        return cmd_bff_login_flow(bff, args.timeout)
    return 2


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenMontage MCP 探测 / 复测工具")
    ap.add_argument("--url", default=os.environ.get("OM_MCP_URL", DEFAULT_URL))
    ap.add_argument("--token", default=os.environ.get("OM_MCP_TOKEN", os.environ.get("MCP_API_TOKEN", DEFAULT_TOKEN)))
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--log", default="om_mcp_probe.log", help="日志文件路径（默认 om_mcp_probe.log）")
    ap.add_argument("--quiet", action="store_true", help="仅写日志文件，不打印到控制台")
    ap.add_argument("--bff", default=os.environ.get("OM_BFF_URL", "https://render.mengxa.com"),
                    help="BFF 基地址（微信登录调试子命令使用）")
    ap.add_argument("--cookie-jar", default="",
                    help="cookie jar 文件（Netscape 格式），跨子命令共享 ff_sid / ff_wx_state")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="仅握手并打印最终 SID")

    sub.add_parser("list", help="列出全部工具名")

    p_call = sub.add_parser("call", help="调用任意工具")
    p_call.add_argument("name")
    p_call.add_argument("json", nargs="?", default="{}", help="arguments JSON")

    p_up = sub.add_parser("upload", help="上传资产（upload_asset，适合小图）")
    p_up.add_argument("file")
    p_up.add_argument("-p", "--project", default="mclaw-demo")

    p_cu = sub.add_parser("chunkupload", help="分块上传高清大图（upload_asset_chunk）")
    p_cu.add_argument("file")
    p_cu.add_argument("-p", "--project", default="mclaw-demo")
    p_cu.add_argument("--chunk", type=int, default=400_000, help="每片二进制字节数")

    p_sh = sub.add_parser("share", help="weiyun_gen_share_link")
    p_sh.add_argument("-d", "--dir")
    p_sh.add_argument("-f", "--file")
    p_sh.add_argument("-n", "--name", default="mclaw-share")
    p_sh.add_argument("--passwd", default="")

    # ---- BFF 微信登录调试子命令 ----
    sub.add_parser("wechat-config", help="检查微信服务号是否已配置（探测 /api/wechat/qrlogin）")
    sub.add_parser("me", help="查询当前会话 /api/me（需 --cookie-jar 携带 ff_sid）")
    sub.add_parser("qr-create", help="创建扫码票据并打印 auth_url（供微信扫码）")
    p_qs = sub.add_parser("qr-status", help="查询扫码票据状态")
    p_qs.add_argument("--ticket", required=True)
    p_qw = sub.add_parser("qr-wait", help="端到端：创建票据→轮询→手机授权→校验 me")
    p_qw.add_argument("--timeout", type=int, default=300)
    p_cc = sub.add_parser("cookie-check", help="检查 ff_sid 的 Set-Cookie 属性（Secure/HttpOnly/SameSite/Path/Max-Age）")
    p_cc.add_argument("--headers", default="", help="curl -D 抓包头文件；缺省读 qr-wait 生成的 om_mcp_setcookie.txt")
    p_lf = sub.add_parser("login-flow", help="完整链路：创建票据→扫码→授权→校验 me")
    p_lf.add_argument("--timeout", type=int, default=300)

    args = ap.parse_args(argv)
    setup_logging(args.log, args.quiet)
    if args.cmd in {"wechat-config", "me", "qr-create", "qr-status", "qr-wait", "cookie-check", "login-flow"}:
        bff = BFFClient(args.bff)
        if args.cookie_jar:
            bff.load_jar(args.cookie_jar)
        rc = run_bff(args, bff)
        if args.cookie_jar:
            bff.save_jar(args.cookie_jar)
        return rc
    if not args.token:
        ap.error("MCP token is required; set MCP_API_TOKEN (or OM_MCP_TOKEN) or pass --token")
    cli = MCPClient(args.url, args.token, max_retries=args.retries)

    try:
        if args.cmd == "init":
            sid = cli.initialize()
            print("SID =", sid)
            return 0

        # 其余子命令都需先握手
        cli.initialize()

        if args.cmd == "list":
            tools = cli.list_tools()
            print(f"TOOL COUNT = {len(tools)}")
            for t in sorted(tools, key=lambda x: x.get("name", "")):
                print(" -", t.get("name"))
            return 0

        if args.cmd == "call":
            try:
                arguments = json.loads(args.json)
            except Exception:
                LOG.error("json 参数无法解析 -> %s", args.json)
                print("ERROR: json 参数无法解析 ->", args.json, file=sys.stderr)
                return 2
            resp = cli.call(args.name, arguments)
            print(json.dumps(cli.extract(resp), ensure_ascii=False, indent=2))
            return 0

        if args.cmd == "upload":
            b64 = _b64_path(args.file)
            safe = os.path.basename(args.file)
            # 仅保留 ASCII 安全 basename（服务端拒绝中文/空格文件名）
            safe = "".join(c if (c.isalnum() or c in "._-") else "_" for c in safe)
            if not safe.lower().endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                safe += ".jpg"
            resp = cli.call("upload_asset", {
                "content_base64": b64,
                "filename": safe,
                "project_id": args.project,
            })
            info = cli.extract(resp)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            if isinstance(info, dict):
                path = (info.get("asset") or {}).get("path") or info.get("path")
                if path:
                    print("SERVER_PATH =", path)
            return 0

        if args.cmd == "chunkupload":
            info = cli.chunk_upload(args.file, args.project, chunk=args.chunk)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            if isinstance(info, dict):
                path = (info.get("asset") or {}).get("path") or info.get("path")
                if path:
                    print("SERVER_PATH =", path)
            return 0

        if args.cmd == "share":
            arguments = {"share_name": args.name, "passwd": args.passwd}
            if args.dir:
                arguments["dir_list"] = [args.dir]
            if args.file:
                arguments["file_list"] = [args.file]
            resp = cli.call("weiyun_gen_share_link", arguments)
            info = cli.extract(resp)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            if isinstance(info, dict):
                url = info.get("data", {}).get("short_url") if isinstance(info.get("data"), dict) else info.get("short_url")
                if url:
                    print("SHARE_URL =", url)
            return 0

        ap.error("unknown command")
        return 2
    except Exception as e:  # noqa: BLE001
        LOG.exception("执行失败：%s", e)
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
