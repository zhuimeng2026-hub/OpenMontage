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
  instances      多实例健康检查 + 微信配置一致性（--bff 用逗号分隔多个实例地址）
  qr-cross-instance 多实例扫码票据可见性校验：A 建票、B 查状态（验证 qrTickets 跨实例共享）

BFF 日志检查（复现“上传卡第一张”类问题时，在 BFF 主机上跑）
------------------------------------------------------------
  log-check       解析 frameflow-bff 的运行日志，定位上传链路的可疑点：
                  会话冷启动(cold_init)耗时/失败、MCP 调用(done)耗时/错误、
                  图片批次创建耗时，以及“有 start 无 done”的疑似卡死请求。
                  默认读 /var/log/frameflow-bff.log；也可用 `--log-path -`
                  从管道读取（如 `journalctl -u frameflow-bff | ...`）。
                  需配合 BFF 侧新增的 [bff-session]/[bff-mcp] done/[image-batch]
                  结构化日志（见 frameflow/bff 的 log-check 相关提交）。

系统状态采集（双机部署：A=render/nginx/BFF，B=MCP/Remotion）
------------------------------------------------------------
  status          采集本机系统状态：CPU/内存/磁盘占用、监听端口存活、
                  关键进程存活、上游链路连通（A→B）。命中异常阈值时在报告中
                  打印 [WARN]/[ERROR]，并写 om_mcp_probe.log 强化异常留痕。
                  --role 预设各机关注点（bff/render/all）；--serve 暴露 HTTP 报告。

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
import contextlib
import hashlib
import io
import json
import logging
import os
import re
import socket
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


def cmd_bff_instances(bff_list):
    """多实例健康检查 + 微信配置一致性。

    逐个实例探测 /api/me（无 cookie 应 200 + authenticated:false）与
    /api/wechat/qrlogin（是否配置、APPID 是否一致），输出每实例状态与汇总。
    --bff 用逗号分隔多个实例地址，例如
      --bff "https://bff1,https://bff2,https://bff3"
    """
    results = []
    appids = set()
    for url in bff_list:
        c = BFFClient(url)
        row = {"url": url}
        try:
            me = c.get("/api/me")
            row["me_ok"] = isinstance(me, dict)
        except Exception as e:  # noqa: BLE001 - 探测工具需兜住网络错误
            row["me_ok"] = False
            row["error"] = str(e)
        try:
            cfg = c.get("/api/wechat/qrlogin")
            if isinstance(cfg, dict):
                if "auth_url" in cfg:
                    row["wechat"] = "configured"
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(cfg["auth_url"]).query)
                    row["appid"] = q.get("appid", [""])[0]
                    appids.add(row["appid"])
                elif "error" in cfg:
                    row["wechat"] = "not_configured"
                else:
                    row["wechat"] = "unknown"
            else:
                row["wechat"] = "unknown"
        except Exception as e:  # noqa: BLE001
            row["wechat"] = "error:" + str(e)
        results.append(row)
        print("INSTANCE %s => me_ok=%s wechat=%s appid=%s" % (
            url, row.get("me_ok"), row.get("wechat"), row.get("appid", "")))
    all_ok = all(r.get("me_ok") for r in results)
    consistent = len(appids) <= 1
    print("INSTANCES_TOTAL = %d" % len(results))
    print("INSTANCES_HEALTHY = %s" % all_ok)
    print("WECHAT_CONFIG_CONSISTENT = %s" % consistent)
    if not consistent:
        print("  [WARN] 各实例 APPID 不一致，可能是不同部署 / 配置漂移")
    if not all_ok:
        print("  [WARN] 存在不可达实例，请检查该实例进程 / 反向代理 / 健康检查")
    return 0 if (all_ok and consistent) else 1


def cmd_bff_qr_cross_instance(bff_a, bff_b, poll=False, timeout=120):
    """多实例扫码票据可见性校验（验证 qrTickets 跨实例共享修复是否生效）。

    在实例 A 创建扫码票据，立即去实例 B 查询其状态：
      - B 能看到 pending/authorized → 票据已跨实例共享，多实例扫码登录无缺口；
      - B 返回 invalid/expired（或 A 创建的票据在 B 上查无）→ 票据未共享，
        多实例部署下手机授权会落在一台、PC 轮询在另一台，导致扫码卡 pending。
    --wait 时额外轮询 B 直到手机授权（需要真实扫码）。
    """
    c = bff_a.get("/api/wechat/qrlogin")
    if not isinstance(c, dict) or "ticket" not in c:
        print("QR_CREATE_FAILED = %s" % json.dumps(c, ensure_ascii=False))
        return 1
    ticket = c["ticket"]
    print("TICKET = %s  (created on %s)" % (ticket, bff_a.base))
    s = bff_b.get("/api/wechat/qrlogin/status", params={"ticket": ticket})
    status = (s or {}).get("status")
    print("STATUS_ON_B = %s  (queried on %s)" % (status, bff_b.base))
    if status in ("pending", "authorized"):
        print("CROSS_INSTANCE_QR_OK = true  (票据在实例间可见，多实例扫码登录无缺口)")
        ok = True
    elif status in ("invalid", "expired"):
        print("CROSS_INSTANCE_QR_OK = false  (票据未跨实例共享，多实例扫码会卡 pending)")
        ok = False
    else:
        print("CROSS_INSTANCE_QR_OK = unknown  (status=%s)" % status)
        ok = False
    if poll and ok:
        print("== 等待手机扫码授权（轮询实例 B）==")
        rc = _poll_qr(bff_b, ticket, timeout)
        return 0 if rc == 0 else 1
    return 0 if ok else 1


# ---------------------------------------------------------------------------
# BFF 日志检查：复现“上传卡第一张”时，在 BFF 主机上解析运行日志定位可疑点
# ---------------------------------------------------------------------------

_LOG_TAG_RE = re.compile(r"\[(bff-mcp|bff-session|image-batch)\]")
_KV_RE = re.compile(r"(\w+)=(\S+)")


def _read_log_lines(path: str, tail: int):
    """读取日志文件的末尾 N 行；path 为 '-' 时从 stdin 读取（便于 journalctl 管道）。"""
    if path == "-":
        data = sys.stdin.read().splitlines()
    else:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = f.readlines()
        except OSError as e:
            print("LOG_OPEN_FAILED path=%s err=%s" % (path, e))
            return []
    return data[-tail:] if tail > 0 else data


def _parse_log_ts(head: str):
    # Go log 前缀形如 "2026/08/17 12:00:00"
    parts = head.strip().split()
    if len(parts) < 2:
        return None
    try:
        return time.strptime(" ".join(parts[:2]), "%Y/%m/%d %H:%M:%S")
    except Exception:
        return None


def cmd_log_check(args):
    """解析 frameflow-bff 日志，定位上传链路的可疑点。

    --serve 时作为 HTTP 服务运行：在部署机监听一个端口，远端（如本机）
    直接 `curl http://<主机>:<端口>/` 即可拿到最新日志检查报告，无需落盘或 SSH。
    """
    if args.serve:
        return _run_serve(args, _render_log_report)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _render_log_report(args)
    sys.stdout.write(buf.getvalue())
    return code


def _render_log_report(args):
    """核心分析逻辑：打印报告并返回退出码（0=无问题 / 1=发现问题）。"""
    from collections import defaultdict
    lines = _read_log_lines(args.log_path, args.tail)
    if not lines:
        return 1

    since = time.time() - args.since * 60
    events = []  # (ts_obj, tag, kind, fields, raw)
    for ln in lines:
        m = _LOG_TAG_RE.search(ln)
        if not m:
            continue
        tag = m.group(1)
        after = ln[m.end():].strip()
        kind = after.split()[0] if after.split() else ""
        fields = {k: v for k, v in _KV_RE.findall(ln)}
        # err 字段可能含空格（如 "err=dial tcp: i/o timeout"），用行尾完整内容覆盖
        merr = re.search(r"\berr=(.*)$", ln)
        if merr:
            fields["err"] = merr.group(1).strip()
        ts = _parse_log_ts(ln[:m.start()])
        if ts is not None and time.mktime(ts) < since:
            continue
        events.append((ts, tag, kind, fields, ln.rstrip("\n")))

    # ---- [1] 会话初始化：cold_init(联网) / resumed(命中持久化) / 失败 ----
    init_kinds = {"cold_init", "batch_cold_init", "batch_reinit"}
    resume_kinds = {"resumed", "batch_resumed"}
    init_elapsed = []          # 联网初始化的耗时列表
    resume_count = 0
    sess_errs = []
    for _, tag, kind, f, raw in events:
        if tag != "bff-session":
            continue
        em = f.get("elapsed_ms", "")
        if kind in init_kinds and em.isdigit():
            init_elapsed.append(int(em))
        elif kind in resume_kinds:
            resume_count += 1
        if kind.endswith("_failed") and f.get("err") and f["err"] not in ("<nil>", "", "nil"):
            sess_errs.append((ts_iso(ts), kind, f["err"]))

    # ---- [2] MCP 调用：done 耗时 + 错误 + start/done 配对(卡死检测) ----
    done_by_tool = defaultdict(list)
    done_errs = []
    pending = defaultdict(int)
    pending_ts = {}
    for ts, tag, kind, f, raw in events:
        if tag != "bff-mcp":
            continue
        if kind == "start":
            key = (f.get("scope_hash"), f.get("tool"), f.get("operation"))
            pending[key] += 1
            if key not in pending_ts:
                pending_ts[key] = ts
        elif kind == "done":
            key = (f.get("scope_hash"), f.get("tool"), f.get("operation"))
            pending[key] = max(0, pending[key] - 1)
            em = f.get("elapsed_ms", "")
            if em.isdigit():
                done_by_tool[f.get("tool")].append(int(em))
            err = f.get("err")
            if err and err not in ("<nil>", "", "nil"):
                done_errs.append((ts_iso(ts), f.get("tool"), f.get("operation"), err))

    # ---- [3] 图片批次创建耗时 + 错误 ----
    ib_elapsed = []
    ib_errs = []
    for ts, tag, kind, f, raw in events:
        if tag == "image-batch" and "create_session" in kind:
            em = f.get("elapsed_ms", "")
            if em.isdigit():
                ib_elapsed.append(int(em))
            if kind.endswith("_failed") and f.get("err") and f["err"] not in ("<nil>", "", "nil"):
                ib_errs.append((ts_iso(ts), f["err"]))

    # ---- [4] 疑似卡死：start 后超过 stall_sec 仍无 done ----
    now = time.time()
    stalls = []
    for key, cnt in pending.items():
        if cnt <= 0:
            continue
        age = (now - time.mktime(pending_ts[key])) if pending_ts.get(key) else 0
        if age >= args.stall_sec:
            stalls.append((key, cnt, age))

    # ---- 报告 ----
    print("=== frameflow-bff 上传链路日志检查 (log-check) ===")
    print("LOG = %s" % args.log_path)
    print("WINDOW = 最近 %d 分钟, tail=%d 行, 解析事件=%d" % (args.since, args.tail, len(events)))
    print("SLOW_THRESHOLD_MS = %d   STALL_SEC = %d" % (args.slow_ms, args.stall_sec))
    print()
    print("[1] 会话初始化 (bff-session)")
    if init_elapsed:
        mx, avg = max(init_elapsed), sum(init_elapsed) // len(init_elapsed)
        print("  %-14s count=%-4d max_ms=%-7d avg_ms=%-7d%s" % (
            "cold_init(联网)", len(init_elapsed), mx, avg, " [WARN 慢]" if mx >= args.slow_ms else ""))
    else:
        print("  cold_init(联网): 窗口内无记录（通常正常：首个身份初始化后走 resumed）")
    print("  resumed(命中持久化): %d  次" % resume_count)
    for t, kind, err in sess_errs:
        print("  [ERR] %s %s err=%s" % (t, kind, err))
    print()
    print("[2] MCP 调用耗时 (bff-mcp done)")
    if done_by_tool:
        for tool, vals in sorted(done_by_tool.items()):
            mx, avg = max(vals), sum(vals) // len(vals)
            print("  %-22s count=%-4d max_ms=%-7d avg_ms=%-7d%s" % (
                tool, len(vals), mx, avg, " [WARN 慢]" if mx >= args.slow_ms else ""))
    else:
        print("  (窗口内无 mcp done 记录)")
    for t, tool, op, err in done_errs:
        print("  [ERR] %s tool=%s operation=%s err=%s" % (t, tool, op, err))
    print()
    print("[3] 图片批次创建 (image-batch create_session)")
    if ib_elapsed:
        mx, avg = max(ib_elapsed), sum(ib_elapsed) // len(ib_elapsed)
        print("  count=%-4d max_ms=%-7d avg_ms=%-7d%s" % (
            len(ib_elapsed), mx, avg, " [WARN 慢]" if mx >= args.slow_ms else ""))
    else:
        print("  (窗口内无 create_session 记录)")
    for t, err in ib_errs:
        print("  [ERR] %s create_session_failed err=%s" % (t, err))
    print()
    print("[4] 疑似卡死 (start 后 %d 秒仍无 done)" % args.stall_sec)
    if not stalls:
        print("  OK: 所有 start 均有 done 配对")
    else:
        for (scope, tool, op), cnt, age in stalls:
            print("  [STALL] scope=%s tool=%s operation=%s pending=%d age=%.0fs" % (scope, tool, op, cnt, age))
    print()
    problems = bool(
        sess_errs or done_errs or ib_errs or stalls
        or (init_elapsed and max(init_elapsed) >= args.slow_ms)
        or any(max(v) >= args.slow_ms for v in done_by_tool.values())
        or (ib_elapsed and max(ib_elapsed) >= args.slow_ms)
    )
    print("VERDICT = %s" % ("PROBLEMS_FOUND" if problems else "NO_PROBLEMS_DETECTED"))
    return 1 if problems else 0


def _run_serve(args, render_fn):
    """以 HTTP 服务形式暴露报告（部署机侧，供 log-check / status 复用）。

    用法：om_mcp_probe.py <子命令> --serve 0.0.0.0:9099
    远端读取：curl http://<部署机>:<端口>/
    GET / 返回纯文本报告；GET /healthz 返回 OK，便于存活探活。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.split("?")[0] == "/healthz":
                body = b"OK\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                render_fn(args)
            body = buf.getvalue().encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):  # 静默访问日志，避免污染
            pass

    host, _, port = args.serve.partition(":")
    port = int(port or "9099")
    host = host or "0.0.0.0"
    httpd = ThreadingHTTPServer((host, port), _Handler)
    print("%s serving on http://%s:%d/  (Ctrl-C 停止)" % (render_fn.__name__, host, port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def ts_iso(ts):
    return time.strftime("%Y-%m-%dT%H:%M:%S", ts) if ts else "?"


# ---------------------------------------------------------------------------
# 系统状态采集（双机部署：A=render/nginx/BFF，B=MCP/Remotion）
# ---------------------------------------------------------------------------

_ROLE_PRESETS = {
    "bff": {
        "ports": [80, 443, 8080],
        "procs": ["nginx", "frameflow-bff", "frameflow"],
        "target": os.environ.get("OM_BFF_UPSTREAM", ""),
    },
    "render": {
        "ports": [8900],
        "procs": ["remotion", "chrome", "chromium", "node"],
        "target": os.environ.get("OM_BFF_URL", ""),
    },
    "all": {
        "ports": [80, 443, 8080, 8900],
        "procs": ["nginx", "frameflow", "remotion", "chrome", "chromium", "node"],
        "target": "",
    },
}


def _safe_hostname():
    try:
        return socket.gethostname()
    except Exception:
        return "?"


def _sample_cpu():
    """返回 CPU 使用率(%)；不可用返回 None。优先 psutil，回退 /proc/stat。"""
    try:
        import psutil
        return round(float(psutil.cpu_percent(interval=1)), 1)
    except Exception:
        pass
    try:
        def _read():
            parts = open("/proc/stat").readline().split()
            vals = list(map(int, parts[1:]))
            return sum(vals), vals[3]  # total, idle
        t1 = _read()
        time.sleep(0.5)
        t2 = _read()
        total = t2[0] - t1[0]
        idle = t2[1] - t1[1]
        if total <= 0:
            return 0.0
        return round((1 - idle / total) * 100, 1)
    except Exception:
        return None


def _sample_mem():
    """返回 (使用率%, 可用MB)；不可用返回 (None, None)。"""
    try:
        import psutil
        m = psutil.virtual_memory()
        return round(float(m.percent), 1), int(m.available / 1024 / 1024)
    except Exception:
        pass
    try:
        info = {}
        for line in open("/proc/meminfo"):
            k, v = line.split(":", 1)
            info[k.strip()] = int(v.split()[0])  # KB
        total = info["MemTotal"]
        avail = info.get("MemAvailable", info.get("MemFree", 0))
        return round((1 - avail / total) * 100, 1), int(avail / 1024)
    except Exception:
        return None, None


def _sample_disk(path):
    """返回 (使用率%, 可用GB)；不可用返回 (None, None)。"""
    try:
        import psutil
        d = psutil.disk_usage(path)
        return round(float(d.percent), 1), round(d.free / 1024 / 1024 / 1024, 1)
    except Exception:
        pass
    try:
        st = os.statvfs(path)
        total = st.f_blocks * st.f_frsize
        free = st.f_bavail * st.f_frsize
        if total <= 0:
            return None, None
        return round((1 - free / total) * 100, 1), round(free / 1024 / 1024 / 1024, 1)
    except Exception:
        return None, None


def _check_port(port, host="127.0.0.1", timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _check_procs(names):
    """返回 dict: 进程名子串 -> 命中进程数；psutil 不可用时返回 None。"""
    try:
        import psutil
        found = {}
        for p in psutil.process_iter(["name", "cmdline"]):
            try:
                nm = ((p.info.get("name") or "") + " " +
                      " ".join(p.info.get("cmdline") or []))
            except Exception:
                continue
            for n in names:
                if n.lower() in nm.lower():
                    found[n] = found.get(n, 0) + 1
        return found
    except Exception:
        return None


# chrome/chromium/chrome-headless-shell 只在 Remotion 渲染进行中才存在（渲染期进程），
# 闲置时缺省属正常，不作为关键进程缺失上报（见 _render_status_report 的按需处理）。
RENDER_ON_DEMAND_PROCS = {"chrome", "chromium", "chrome-headless-shell", "headless-shell", "headless_shell"}


def _render_active():
    """是否有 Remotion 渲染子进程在跑（headless 浏览器只在该时刻存在）。

    识别 "remotion render" 命令行（区别于常驻的 remotion studio）。"""
    try:
        import psutil
        for p in psutil.process_iter(["cmdline"]):
            try:
                cmd = " ".join(p.info.get("cmdline") or [])
            except Exception:
                continue
            if "remotion render" in cmd:
                return True
    except Exception:
        pass
    return False


def _http_probe(url, timeout=10, token=""):
    """纯连通性 + 耗时探测：返回 (ok, http_code, elapsed_ms, err)。

    提供 token 时附加 Bearer 头——BFF / MCP 均要求鉴权，否则一律 401 会误报
    为 upstream_down（健康系统被误判为故障）。
    """
    bf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".body")
    bf.close()
    cmd = ["curl", "-sS", "--max-time", str(timeout),
           "-o", bf.name, "-w", "%{http_code} %{time_total}"]
    if token:
        cmd += ["-H", "Authorization: Bearer %s" % token]
    cmd.append(url)
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        # -w 指标写入 stdout；body 已落到 -o 文件，不会混入 stdout。
        metrics = (r.stdout or "").strip().split()
        code = metrics[0] if metrics else "000"
        elapsed = int(float(metrics[1]) * 1000) if len(metrics) > 1 else 0
        ok = r.returncode == 0 and code[:1] in ("2", "3")
        err = "" if r.returncode == 0 else (r.stderr or "").strip()[:200]
        return ok, code, elapsed, err
    except subprocess.TimeoutExpired:
        return False, "000", timeout * 1000, "curl timeout"
    except Exception as e:  # noqa: BLE001
        return False, "000", 0, str(e)
    finally:
        try:
            os.remove(bf.name)
        except OSError:
            pass


def _mcp_probe(url, token, timeout=15):
    """对 MCP 端点做真实 initialize 握手探测（带 Bearer + Accept + 会话）。

    纯 GET /mcp 无鉴权/无 Accept 头会被 MCP 层拒绝（401/406），必须走协议握手
    才能证明「服务可正常接受工具调用」。返回 (ok, http_code, elapsed_ms, err)。
    """
    import time as _t
    t0 = _t.monotonic()
    hdrs = [
        "Content-Type: application/json",
        "Accept: application/json, text/event-stream",
        "Authorization: Bearer %s" % token,
    ]
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "om_probe", "version": "1.0.0"}},
    })
    hf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".hdr")
    bf = tempfile.NamedTemporaryFile("w+", delete=False, suffix=".body")
    hf.close()
    bf.close()
    cmd = ["curl", "-sS", "--max-time", str(timeout),
           "-D", hf.name, "-o", bf.name]
    for h in hdrs:
        cmd += ["-H", h]
    cmd += ["-d", body, url]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 10)
        code = "000"
        try:
            with open(hf.name, encoding="utf-8", errors="replace") as f:
                for line in f.read().splitlines():
                    if line.lower().startswith("http/"):
                        parts = line.split(" ", 2)
                        if len(parts) > 1:
                            code = parts[1]
        except OSError:
            pass
        with open(bf.name, encoding="utf-8", errors="replace") as f:
            raw = f.read()
        elapsed = int((_t.monotonic() - t0) * 1000)
        # streamable HTTP：initialize 成功返回 200（JSON）或 202（SSE 延迟响应）且含 result
        ok = r.returncode == 0 and code[:1] == "2" and '"result"' in raw
        if ok:
            err = ""
        elif r.returncode != 0:
            err = (r.stderr or "").strip()[:200]
        else:
            err = "code=%s body=%s" % (code, raw[:200])
        return ok, code, elapsed, err
    except subprocess.TimeoutExpired:
        return False, "000", timeout * 1000, "curl timeout"
    except Exception as e:  # noqa: BLE001
        return False, "000", 0, str(e)
    finally:
        for fp in (hf.name, bf.name):
            try:
                os.remove(fp)
            except OSError:
                pass


def cmd_status(args):
    """采集本机系统状态。--serve 时作为 HTTP 服务运行（部署机侧）。"""
    if args.serve:
        return _run_serve(args, _render_status_report)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = _render_status_report(args)
    sys.stdout.write(buf.getvalue())
    return code


def _render_status_report(args):
    """采集本机系统状态并打印报告。

    命中异常阈值 / 故障时，除报告中打印 [WARN]/[ERROR] 外，还会通过
    LOG.warning/LOG.error 写入 om_mcp_probe.log，强化异常留痕，便于事后复盘。
    """
    preset = _ROLE_PRESETS.get(args.role, _ROLE_PRESETS["all"])
    ports = [int(p) for p in args.ports.split(",") if p.strip()] or preset["ports"]
    proc_names = [p for p in args.procs.split(",") if p.strip()] or preset["procs"]
    target = args.target or preset.get("target", "")

    print("=== OpenMontage 系统状态采集 (status) ===")
    print("ROLE = %s" % args.role)
    print("HOSTNAME = %s" % _safe_hostname())
    print("COLLECT_AT = %s" % ts_iso(time.localtime(time.time())))
    print()

    problems = []

    # [1] CPU / 内存 / 磁盘
    print("[1] 资源占用")
    cpu = _sample_cpu()
    if cpu is None:
        print("  CPU: [WARN] 无法采样（无 psutil 且非 /proc 系统）")
        LOG.warning("status: CPU 采样失败")
        problems.append("cpu_unavailable")
    else:
        warn = cpu >= args.cpu_warn
        print("  CPU%% = %.1f%s" % (cpu, " [WARN 高负载]" if warn else ""))
        if warn:
            LOG.warning("status: CPU 使用率 %.1f%% >= 阈值 %.0f%%", cpu, args.cpu_warn)
            problems.append("cpu_high")

    mem_pct, mem_avail = _sample_mem()
    if mem_pct is None:
        print("  内存: [WARN] 无法采样")
        LOG.warning("status: 内存采样失败")
        problems.append("mem_unavailable")
    else:
        warn = mem_pct >= args.mem_warn
        print("  内存%% = %.1f (可用 %d MB)%s" % (mem_pct, mem_avail or 0,
                                                 " [WARN 高占用]" if warn else ""))
        if warn:
            LOG.warning("status: 内存使用率 %.1f%% >= 阈值 %.0f%%", mem_pct, args.mem_warn)
            problems.append("mem_high")

    disk_pct, disk_free = _sample_disk(args.disk_path)
    if disk_pct is None:
        print("  磁盘(%s): [WARN] 无法采样" % args.disk_path)
        LOG.warning("status: 磁盘采样失败 path=%s", args.disk_path)
        problems.append("disk_unavailable")
    else:
        warn = disk_pct >= args.disk_warn
        print("  磁盘(%s)%% = %.1f (可用 %.1f GB)%s" % (
            args.disk_path, disk_pct, disk_free or 0, " [WARN 空间不足]" if warn else ""))
        if warn:
            LOG.warning("status: 磁盘使用率 %.1f%% >= 阈值 %.0f%% path=%s",
                        disk_pct, args.disk_warn, args.disk_path)
            problems.append("disk_high")
    print()

    # [2] 监听端口
    print("[2] 监听端口存活 (127.0.0.1)")
    for port in ports:
        ok = _check_port(port)
        flag = "OK" if ok else "[ERROR 端口未监听]"
        if not ok:
            LOG.error("status: 端口 %d 未监听（预期服务不可达）", port)
            problems.append("port_down:%d" % port)
        print("  :%d -> %s" % (port, flag))
    print()

    # [3] 关键进程存活
    print("[3] 关键进程存活")
    proc_hits = _check_procs(proc_names)
    render_on = _render_active()
    on_demand = [n for n in proc_names if n in RENDER_ON_DEMAND_PROCS]
    on_demand_found = False
    if proc_hits is None:
        print("  [WARN] psutil 不可用，跳过进程检查；端口检查仍可覆盖服务可达性")
        LOG.warning("status: 进程检查不可用（无 psutil）")
    else:
        for name in proc_names:
            cnt = proc_hits.get(name, 0)
            if cnt > 0:
                print("  %-20s -> count=%d" % (name, cnt))
                if name in RENDER_ON_DEMAND_PROCS:
                    on_demand_found = True
            elif name in RENDER_ON_DEMAND_PROCS:
                # headless 浏览器只在 Remotion 渲染进行中才存在；闲置时缺省正常
                print("  %-20s -> count=0 (渲染时才需要)" % name)
            else:
                LOG.error("status: 未找到关键进程: %s", name)
                problems.append("proc_missing:%s" % name)
                print("  %-20s -> [ERROR 未找到进程]" % name)
    if render_on and on_demand and not on_demand_found:
        # 渲染在跑却一个 headless 浏览器都没有 → 渲染已卡死/浏览器崩溃
        LOG.error("status: 渲染进行中但缺少 headless 浏览器进程: %s", ",".join(on_demand))
        problems.append("proc_missing:" + "+".join(on_demand))
        print("  [ERROR] 渲染进行中但无任何 headless 浏览器进程 (%s)" % ",".join(on_demand))
    print()

    # [4] 上游链路连通
    print("[4] 上游链路连通")
    if not target:
        print("  (未配置 --target，跳过；可用 --target 指定 BFF→MCP 端点)")
    else:
        token = getattr(args, "token", None) or ""
        if "/mcp" in target.lower():
            ok, code, elapsed, err = _mcp_probe(target, token) if token else _http_probe(target)
        else:
            ok, code, elapsed, err = _http_probe(target, token=token)
        if ok:
            slow = elapsed >= 3000
            print("  %s -> HTTP %s, %d ms%s" % (target, code, elapsed,
                                               " [WARN 慢]" if slow else ""))
            if slow:
                LOG.warning("status: 上游探测慢 target=%s %dms", target, elapsed)
                problems.append("upstream_slow")
        else:
            print("  %s -> [ERROR] HTTP %s err=%s" % (target, code, err))
            LOG.error("status: 上游探测失败 target=%s code=%s err=%s", target, code, err)
            problems.append("upstream_down")
    print()

    print("VERDICT = %s" % ("PROBLEMS_FOUND" if problems else "NO_PROBLEMS_DETECTED"))
    print("PROBLEM_TAGS = %s" % (",".join(problems) if problems else "-"))
    return 1 if problems else 0


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
                    help="BFF 基地址（微信登录调试子命令使用）；instances 子命令可用逗号分隔多个实例地址")
    ap.add_argument("--bff-b", default=os.environ.get("OM_BFF_B_URL", ""),
                    help="第二实例基地址（qr-cross-instance 校验跨实例票据共享时使用）")
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
    p_inst = sub.add_parser("instances",
                            help="多实例健康检查 + 微信配置一致性（--bff 用逗号分隔多个实例地址）")
    p_inst.add_argument("--bff", default=os.environ.get("OM_BFF_URL", "https://render.mengxa.com"),
                        help="BFF 实例地址，逗号分隔多个；例如 --bff \"https://bff1,https://bff2\"")
    p_qx = sub.add_parser("qr-cross-instance",
                          help="多实例扫码票据可见性校验：A 建票、B 查状态（验证 qrTickets 跨实例共享）")
    p_qx.add_argument("--bff", default=os.environ.get("OM_BFF_URL", "https://render.mengxa.com"),
                      help="第一实例（创建票据）地址")
    p_qx.add_argument("--bff-b", default=os.environ.get("OM_BFF_B_URL", ""),
                      help="第二实例（查询票据状态）地址")
    p_qx.add_argument("--wait", action="store_true",
                      help="额外轮询第二实例 B 直到手机授权（需真实扫码）")
    p_qx.add_argument("--timeout", type=int, default=120)

    # ---- BFF 日志检查子命令 ----
    p_lc = sub.add_parser("log-check",
                          help="解析 frameflow-bff 日志，定位上传链路可疑点（卡第一张等）；默认读 /var/log/frameflow-bff.log，--log-path - 表示从 stdin 读取")
    p_lc.add_argument("--log-path", default="/var/log/frameflow-bff.log",
                     help="BFF 日志文件路径；'-' 表示从 stdin 读取（如 journalctl 管道）")
    p_lc.add_argument("--since", type=int, default=60, help="仅分析最近 N 分钟的日志（默认 60）")
    p_lc.add_argument("--tail", type=int, default=5000, help="最多读取日志末尾 N 行（默认 5000）")
    p_lc.add_argument("--slow-ms", type=int, default=3000, help="耗时超过该毫秒数标记为慢（默认 3000）")
    p_lc.add_argument("--stall-sec", type=int, default=10, help="start 后超过该秒数仍无 done 视为疑似卡死（默认 10）")
    p_lc.add_argument("--serve", default="",
                     help="以 HTTP 服务形式运行（部署机侧）：监听 host:port，远端 curl http://<host>:<port>/ 读取报告；如 --serve 0.0.0.0:9099")

    # ---- 系统状态采集子命令 ----
    p_st = sub.add_parser("status",
                          help="采集本机系统状态（CPU/内存/磁盘/端口/进程/上游连通）；异常状态强化记录；--serve 暴露 HTTP 报告")
    p_st.add_argument("--role", choices=["bff", "render", "all"], default="all",
                      help="本机角色：bff=nginx+BFF+前端, render=MCP+Remotion, all=全部（默认 all）")
    p_st.add_argument("--ports", default="",
                      help="额外需要检查监听的端口（逗号分隔）；为空时用 --role 预设")
    p_st.add_argument("--procs", default="",
                      help="需要检查存活的进程名子串（逗号分隔）；为空时用 --role 预设")
    p_st.add_argument("--target", default="",
                      help="上游链路探测 URL（如 BFF→MCP 端点）；为空时用 --role 预设/环境变量")
    p_st.add_argument("--disk-path", default="/", help="磁盘占用采样路径（默认 /）")
    p_st.add_argument("--cpu-warn", type=float, default=85.0, help="CPU 使用率阈值%%（默认 85）")
    p_st.add_argument("--mem-warn", type=float, default=85.0, help="内存使用率阈值%%（默认 85）")
    p_st.add_argument("--disk-warn", type=float, default=90.0, help="磁盘使用率阈值%%（默认 90）")
    p_st.add_argument("--serve", default="",
                      help="以 HTTP 服务形式运行（部署机侧）：监听 host:port，远端 curl http://<host>:<port>/ 读取报告；如 --serve 0.0.0.0:9099")

    args = ap.parse_args(argv)
    setup_logging(args.log, args.quiet)
    if args.cmd == "log-check":
        return cmd_log_check(args)
    if args.cmd == "status":
        return cmd_status(args)
    BFF_CMDS = {"wechat-config", "me", "qr-create", "qr-status", "qr-wait",
                "cookie-check", "login-flow", "instances", "qr-cross-instance"}
    if args.cmd in BFF_CMDS:
        if args.cmd == "instances":
            urls = [u.strip() for u in args.bff.split(",") if u.strip()]
            if not urls:
                ap.error("instances 需要 --bff 提供至少一个实例地址（逗号分隔）")
            return cmd_bff_instances(urls)
        if args.cmd == "qr-cross-instance":
            if not args.bff_b:
                ap.error("qr-cross-instance 需要 --bff-b 指定第二实例地址")
            bff_a = BFFClient(args.bff)
            bff_b = BFFClient(args.bff_b)
            return cmd_bff_qr_cross_instance(bff_a, bff_b, poll=args.wait, timeout=args.timeout)
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
                path = (info.get("asset") or {}).get("relative_path") or (info.get("asset") or {}).get("path") or info.get("relative_path") or info.get("path")
                if path:
                    print("SERVER_PATH =", path)
            return 0

        if args.cmd == "chunkupload":
            info = cli.chunk_upload(args.file, args.project, chunk=args.chunk)
            print(json.dumps(info, ensure_ascii=False, indent=2))
            if isinstance(info, dict):
                path = (info.get("asset") or {}).get("relative_path") or (info.get("asset") or {}).get("path") or info.get("relative_path") or info.get("path")
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
