#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Render chain monitor test — submit a real render task via MCP (192.168.20.173:8900/mcp)
and verify the system produces a normal video end-to-end.

Mimics an external client: initialize session -> upload images -> create_remotion_video_share
-> poll get_render_status -> verify the output video file.
"""
import base64, io, json, os, re, subprocess, sys, time, traceback
import requests
from PIL import Image, ImageDraw

TOKEN = os.environ.get("MCP_API_TOKEN", "").strip()
HOST = os.environ.get("MCP_HOST", "192.168.20.173")
PORT = os.environ.get("MCP_PORT", "8900")
URL = f"http://{HOST}:{PORT}/mcp"
PROJECT_ID = "monitor-render-" + time.strftime("%Y%m%d-%H%M%S")
LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chain_test.log")

def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S") + " " + line + "\n")

def png(i, w=540, h=960):
    img = Image.new("RGB", (w, h), ((i * 60) % 256, (i * 97 + 30) % 256, (i * 130 + 80) % 256))
    d = ImageDraw.Draw(img)
    d.text((40, h // 2), f"Monitor Test {i + 1}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()

def parse(r):
    text = r.text
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                pass
    try:
        return json.loads(text)
    except Exception:
        return {"raw": text[:500]}

def main():
    if not TOKEN:
        log("FATAL: MCP_API_TOKEN not set"); return 2
    s = requests.Session()
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    rid = [0]

    def call(method, params=None, note=False):
        rid[0] += 1
        b = {"jsonrpc": "2.0", "method": method}
        if not note:
            b["id"] = rid[0]
        if params is not None:
            b["params"] = params
        r = s.post(URL, headers=h, json=b, timeout=180)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            h["Mcp-Session-Id"] = sid
        if r.status_code != 200:
            log(f"  HTTP {r.status_code}: {r.text[:300]}")
        return parse(r), sid

    log(f"== chain test start  URL={URL} PROJECT={PROJECT_ID}")
    t0 = time.time()

    # 1. initialize
    init, sid = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "monitor-test", "version": "1.0"}})
    if not sid:
        log(f"FATAL: no session id -> {json.dumps(init)[:300]}")
        return 2
    log(f"initialize OK sid={sid[:8]}... serverInfo={init.get('result', {}).get('serverInfo')}")
    call("notifications/initialized", note=True)

    # 2. upload 3 images
    for i in range(3):
        res, _ = call("tools/call", {"name": "upload_asset", "arguments": {
            "project_id": PROJECT_ID, "filename": f"s{i}.png",
            "content_base64": png(i), "mime_type": "image/png"}})
        txt = res.get("result", {}).get("content", [{}])
        t = txt[0].get("text", "") if txt else json.dumps(res)[:200]
        ok = '"success": true' in t or '"success":true' in t
        log(f"upload s{i}.png -> {'OK' if ok else 'FAIL'} {t[:160]}")

    # 3. create remotion video share
    res, _ = call("tools/call", {"name": "create_remotion_video_share", "arguments": {
        "project_id": PROJECT_ID, "script_id": "photo-ken-burns",
        "duration_per_image": 3.0, "aspect_ratio": "9:16", "title": "monitor-test"}})
    txt = res.get("result", {}).get("content", [{}])
    t = txt[0].get("text", "") if txt else json.dumps(res)[:300]
    log(f"create_remotion_video_share -> {t[:400]}")
    m = re.search(r'"render_job_id"\s*:\s*"([\w-]+)"', t)
    job = m.group(1) if m else None
    if not job:
        log("FATAL: no render_job_id"); return 2
    log(f"job_id={job}  elapsed={time.time()-t0:.0f}s")

    # 4. poll status
    final = None
    for n in range(120):
        time.sleep(5)
        res, _ = call("tools/call", {"name": "get_render_status", "arguments": {"render_job_id": job}})
        txt = res.get("result", {}).get("content", [{}])
        t = txt[0].get("text", "") if txt else json.dumps(res)[:300]
        try:
            st = json.loads(t)
        except Exception:
            st = {"raw": t}
        log(f"[{n * 5:>4}s] status={st.get('status')} stage={st.get('stage')} | {t[:220]}")
        if st.get("status") in ("published", "failed"):
            final = st
            break
    if not final:
        log("TIMEOUT: no terminal status after 600s")
        return 2

    # 5. verify local video files
    proj = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects", PROJECT_ID))
    vids = []
    if os.path.isdir(proj):
        for root, _, files in os.walk(proj):
            for f in files:
                if f.lower().endswith((".mp4", ".webm", ".mov")):
                    vids.append(os.path.join(root, f))
    log("=" * 56)
    log(f"FINAL status={final.get('status')} share_url={final.get('share_url')}")
    log(f"project dir: {proj}")
    if not vids:
        log("NO VIDEO FILES FOUND — FAIL")
        return 2
    for v in sorted(vids):
        sz = os.path.getsize(v)
        log(f"video: {v}  size={sz}")
        if sz < 1000:
            log(f"  -> WARN tiny file (likely broken)")
        else:
            # ffprobe validity
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration,size",
                                 "-show_entries", "stream=codec_type,codec_name,width,height",
                                 "-of", "json", v], capture_output=True, text=True)
            log(f"  ffprobe: {pr.stdout.strip()[:300]}" + ("  ERR: " + pr.stderr.strip()[:200] if pr.stderr else ""))
    log(f"CHAIN TEST {'PASS' if final.get('status') == 'published' else 'CHECK'} — elapsed={time.time()-t0:.0f}s")
    return 0 if final.get("status") == "published" else 2

if __name__ == "__main__":
    open(LOG_PATH, "a", encoding="utf-8").write("\n===== RUN " + time.strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        sys.exit(2)
