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
  share -d DIR | -f FILE
                  调用 weiyun_gen_share_link（file_list / dir_list），打印 short_url

环境变量
--------
  OM_MCP_URL     端点（默认 https://dw.aixifs.com/mcp）
  OM_MCP_TOKEN   Bearer token（默认内置 h6LQ...RT6WJE）

示例
----
  python om_mcp_probe.py list
  python om_mcp_probe.py upload "C:/path/45.jpg" -p mclaw-demo
  python om_mcp_probe.py share -d /opt/OpenMontage/renders
  python om_mcp_probe.py call weiyun_gen_share_link '{"dir_list":["/opt/OpenMontage/renders"]}'
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

DEFAULT_URL = "https://dw.aixifs.com/mcp"
DEFAULT_TOKEN = "h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE"


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
        hf.close()
        bf.close()
        cmd = [
            "curl", "-sS", "--max-time", "40",
            "--retry", "4", "--retry-delay", "1", "--retry-all-errors",
            "-D", hf.name, "-o", bf.name,
        ]
        for h in hdrs:
            cmd += ["-H", h]
        cmd += ["--data-binary", json.dumps(payload, ensure_ascii=False), self.url]

        last_err = None
        for attempt in range(1, self.max_retries + 1):
            try:
                subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=70)
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
                last_err = f"curl exit {e.returncode}"
            except Exception as e:  # noqa: BLE001
                last_err = str(e)
            time.sleep(1)
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
    ap.add_argument("--token", default=os.environ.get("OM_MCP_TOKEN", DEFAULT_TOKEN))
    ap.add_argument("--retries", type=int, default=6)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="仅握手并打印最终 SID")

    sub.add_parser("list", help="列出全部工具名")

    p_call = sub.add_parser("call", help="调用任意工具")
    p_call.add_argument("name")
    p_call.add_argument("json", nargs="?", default="{}", help="arguments JSON")

    p_up = sub.add_parser("upload", help="上传资产")
    p_up.add_argument("file")
    p_up.add_argument("-p", "--project", default="mclaw-demo")

    p_sh = sub.add_parser("share", help="weiyun_gen_share_link")
    p_sh.add_argument("-d", "--dir")
    p_sh.add_argument("-f", "--file")
    p_sh.add_argument("-n", "--name", default="mclaw-share")
    p_sh.add_argument("--passwd", default="")

    args = ap.parse_args(argv)
    cli = MCPClient(args.url, args.token, max_retries=args.retries)

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


if __name__ == "__main__":
    sys.exit(main())
