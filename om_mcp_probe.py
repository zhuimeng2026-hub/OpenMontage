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
import subprocess
import sys
import tempfile
import time

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


def main(argv=None):
    ap = argparse.ArgumentParser(description="OpenMontage MCP 探测 / 复测工具")
    ap.add_argument("--url", default=os.environ.get("OM_MCP_URL", DEFAULT_URL))
    ap.add_argument("--token", default=os.environ.get("OM_MCP_TOKEN", os.environ.get("MCP_API_TOKEN", DEFAULT_TOKEN)))
    ap.add_argument("--retries", type=int, default=6)
    ap.add_argument("--log", default="om_mcp_probe.log", help="日志文件路径（默认 om_mcp_probe.log）")
    ap.add_argument("--quiet", action="store_true", help="仅写日志文件，不打印到控制台")
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

    args = ap.parse_args(argv)
    setup_logging(args.log, args.quiet)
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
