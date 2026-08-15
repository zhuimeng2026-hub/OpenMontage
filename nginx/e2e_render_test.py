#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenMontage 端到端测试：图片上传 -> Remotion 短视频渲染。
走真实浏览器路径：https://render.mengxa.com/api/mcp -> nginx -> BFF -> MCP。
若 HTTPS 代理失败，自动回退到 localhost:8900 直连 MCP。
"""
import base64, io, json, os, sys, time, traceback
import requests
from PIL import Image, ImageDraw

TOKEN = "h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE"
TARGET_HTTPS = "https://render.mengxa.com/api/mcp"
TARGET_DIRECT = "http://localhost:8900/mcp"

PROJECT_ID = "e2e-test-" + time.strftime("%Y%m%d-%H%M%S")
N_IMAGES = 3

def log(*a):
    print("[e2e]", *a, flush=True)

def make_test_png(idx, w=540, h=960):
    img = Image.new("RGB", (w, h), (idx*60 % 256, (idx*97+30) % 256, (idx*130+80) % 256))
    d = ImageDraw.Draw(img)
    # 渐变条纹 + 文字，方便肉眼确认视频内容
    for y in range(0, h, 40):
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=2)
    d.text((40, h//2), f"OpenMontage\nTest {idx+1}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

class MCPClient:
    def __init__(self, base, verify=True):
        self.base = base
        self.verify = verify
        self.sid = None
        self.rid = 0
        self.session = requests.Session()

    def _headers(self, has_id=True):
        h = {
            "Authorization": f"Bearer {TOKEN}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.sid:
            h["Mcp-Session-Id"] = self.sid
        return h

    def _parse(self, resp):
        text = resp.text
        # 优先解析 SSE 的 data: 行
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                payload = line[5:].strip()
                try:
                    return json.loads(payload)
                except Exception:
                    continue
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text}

    def call(self, method, params=None, notification=False):
        self.rid += 1
        body = {"jsonrpc": "2.0", "method": method}
        if not notification:
            body["id"] = self.rid
        if params is not None:
            body["params"] = params
        resp = self.session.post(self.base, headers=self._headers(), json=body,
                                 timeout=120, verify=self.verify)
        resp.raise_for_status()
        if self.sid is None:
            self.sid = resp.headers.get("Mcp-Session-Id")
        return self._parse(resp)

    def initialize(self):
        r = self.call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "e2e-render-test", "version": "0.1"},
        })
        self.call("notifications/initialized", notification=True)
        return r

def main():
    # 1) 建立 MCP（优先走 HTTPS 浏览器路径）
    client = None
    for base, verify in [(TARGET_HTTPS, False), (TARGET_DIRECT, True)]:
        log(f"尝试连接 MCP: {base}")
        try:
            c = MCPClient(base, verify=verify)
            init = c.initialize()
            if "result" in init and "serverInfo" in init["result"]:
                log(f"握手成功 via {base} -> serverInfo={init['result']['serverInfo']}")
                client = c
                break
            else:
                log(f"握手异常: {init}")
        except Exception as e:
            log(f"连接失败: {type(e).__name__}: {e}")
    if client is None:
        log("无法连接 MCP，终止"); sys.exit(2)
    log(f"使用传输路径: {client.base}  session={client.sid}")

    # 2) 生成并上传测试图
    uploaded = []
    for i in range(N_IMAGES):
        png = make_test_png(i)
        b64 = base64.b64encode(png).decode()
        fn = f"scene_{i+1}.png"
        res = client.call("tools/call", {
            "name": "upload_asset",
            "arguments": {
                "project_id": PROJECT_ID,
                "filename": fn,
                "content_base64": b64,
                "mime_type": "image/png",
            },
        })
        content = res.get("result", {}).get("content", [{}])
        text = content[0].get("text", "") if content else ""
        ok = '"success": true' in text or '"success":true' in text
        log(f"上传 {fn}: {'OK' if ok else 'FAIL'} | {text[:160]}")
        uploaded.append((fn, ok, text))

    if not all(u[1] for u in uploaded):
        log("部分图片上传失败，终止渲染"); sys.exit(3)

    # 3) 触发 Remotion 渲染（非阻塞）
    log("触发 create_remotion_video_share ...")
    res = client.call("tools/call", {
        "name": "create_remotion_video_share",
        "arguments": {
            "project_id": PROJECT_ID,
            "script_id": "photo-ken-burns",
            "duration_per_image": 3.0,
            "aspect_ratio": "9:16",
            "title": "E2E 渲染测试",
        },
    })
    c0 = res.get("result", {}).get("content", [{}])
    txt0 = c0[0].get("text", "") if c0 else ""
    log(f"create_remotion_video_share 返回: {txt0[:300]}")
    try:
        job_id = json.loads(txt0).get("render_job_id")
    except Exception:
        job_id = None
    if not job_id:
        # 兼容文本里直接含 job id
        import re
        m = re.search(r"render_job_id['\"]?\s*[:=]\s*['\"]?([\w-]+)", txt0)
        job_id = m.group(1) if m else None
    log(f"render_job_id = {job_id}")
    if not job_id:
        log("未取得 render_job_id，终止"); sys.exit(4)

    # 4) 轮询状态
    final = None
    for t in range(0, 60):  # 最多 ~10 分钟
        res = client.call("tools/call", {
            "name": "get_render_status",
            "arguments": {"render_job_id": job_id},
        })
        c1 = res.get("result", {}).get("content", [{}])
        txt1 = c1[0].get("text", "") if c1 else ""
        try:
            st = json.loads(txt1)
        except Exception:
            st = {"raw": txt1}
        status = st.get("status")
        log(f"[{t*10:>3}s] status={status} | {txt1[:200]}")
        if status in ("published", "failed", "rendered"):
            final = st
            break
        time.sleep(10)

    # 5) 核实本地产物
    proj_dir = os.path.join(os.getcwd(), "..", "projects", PROJECT_ID)
    proj_dir = os.path.abspath(proj_dir)
    log(f"项目目录: {proj_dir}")
    mp4s = []
    if os.path.isdir(proj_dir):
        for root, _, files in os.walk(proj_dir):
            for f in files:
                if f.lower().endswith((".mp4", ".webm", ".mov")):
                    mp4s.append(os.path.join(root, f))
    for p in mp4s:
        sz = os.path.getsize(p)
        log(f"产物视频: {p} ({sz} bytes)")

    # 6) 结论
    log("="*60)
    log("E2E 结果:")
    log(f"  传输路径      : {client.base}")
    log(f"  上传图片      : {sum(1 for u in uploaded if u[1])}/{len(uploaded)} 成功")
    log(f"  render_job_id : {job_id}")
    log(f"  最终状态      : {final.get('status') if final else 'timeout'}")
    if final:
        log(f"  share_url     : {final.get('share_url')}")
        log(f"  stage         : {final.get('stage')}")
        log(f"  message       : {str(final.get('message'))[:200]}")
    log(f"  本地视频产物  : {mp4s if mp4s else '无'}")
    log("="*60)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        sys.exit(99)
