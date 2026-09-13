#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Full chunked-upload + render chain test, mirroring the external BFF client.

Flow: initialize -> upload_asset_chunk start -> append (real PNG data) -> complete
      -> create_remotion_video_share -> poll get_render_status -> verify video.
"""
import base64, io, json, os, re, subprocess, sys, time, traceback
import requests
from PIL import Image, ImageDraw

TOKEN = os.environ.get("MCP_API_TOKEN", "").strip()
URL = f"http://192.168.20.173:8900/mcp"
PROJECT_ID = "chunk-chain-" + time.strftime("%Y%m%d-%H%M%S")
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chunk_chain.log")

def log(*a):
    line = " ".join(str(x) for x in a)
    print(line, flush=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(time.strftime("%H:%M:%S") + " " + line + "\n")

def make_png_bytes(i, w=540, h=960):
    img = Image.new("RGB", (w, h), ((i * 71) % 256, (i * 53 + 40) % 256, (i * 19 + 90) % 256))
    d = ImageDraw.Draw(img)
    d.text((30, h // 2), f"ChunkTest {i + 1}", fill=(255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

def parse(r):
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try:
                return json.loads(line[5:].strip())
            except Exception:
                pass
    try:
        return json.loads(r.text)
    except Exception:
        return {"raw": r.text[:300]}

def main():
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
        r = s.post(URL, headers=h, json=b, timeout=60)
        sid = r.headers.get("Mcp-Session-Id")
        if sid:
            h["Mcp-Session-Id"] = sid
        return parse(r), sid

    def tool_text(res):
        c = res.get("result", {}).get("content", [{}])
        return c[0].get("text", "") if c else json.dumps(res)[:200]

    t0 = time.time()
    log(f"== chunk chain start URL={URL} PROJECT={PROJECT_ID}")

    init, sid = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "chunk-chain-test", "version": "1.0"}})
    if not sid:
        log("FATAL no session"); return 2
    call("notifications/initialized", note=True)
    log(f"initialize OK sid={sid[:8]}")

    # upload 3 images via chunked protocol
    for i in range(3):
        data = make_png_bytes(i)
        b64 = base64.b64encode(data).decode()
        total = len(data)
        res, _ = call("tools/call", {"name": "upload_asset_chunk", "arguments": {
            "operation": "start", "project_id": PROJECT_ID, "filename": f"s{i}.png",
            "total_bytes": total, "mime_type": "image/png"}})
        txt = tool_text(res)
        st = json.loads(txt)
        if not st.get("success"):
            log(f"start s{i} FAIL: {txt[:200]}"); return 2
        up_id = st["upload_id"]
        chunk_size = st["chunk_limit_bytes"]
        off = 0
        # append in chunks
        for cstart in range(0, total, chunk_size):
            cpart = b64[cstart // 3 * 4:][: (min(cstart + chunk_size, total) - cstart) // 3 * 4]
            # recompute exact b64 slice for [cstart, cend)
            cend = min(cstart + chunk_size, total)
            raw = data[cstart:cend]
            cb64 = base64.b64encode(raw).decode()
            res, _ = call("tools/call", {"name": "upload_asset_chunk", "arguments": {
                "operation": "append", "upload_id": up_id, "offset": cstart, "chunk_base64": cb64}})
            t = tool_text(res)
            ast = json.loads(t)
            if not ast.get("success"):
                log(f"append s{i}@{cstart} FAIL: {t[:200]}"); return 2
            off = ast.get("next_offset")
        res, _ = call("tools/call", {"name": "upload_asset_chunk", "arguments": {
            "operation": "complete", "upload_id": up_id}})
        t = tool_text(res)
        cst = json.loads(t)
        log(f"upload s{i} ({total}B) complete -> success={cst.get('success')} dedup={cst.get('deduplicated')}")

    # create render job
    res, _ = call("tools/call", {"name": "create_remotion_video_share", "arguments": {
        "project_id": PROJECT_ID, "script_id": "photo-ken-burns",
        "duration_per_image": 3.0, "aspect_ratio": "9:16", "title": "chunk-chain"}})
    t = tool_text(res)
    log(f"create_remotion_video_share -> {t[:300]}")
    m = re.search(r'"render_job_id"\s*:\s*"([\w-]+)"', t)
    job = m.group(1) if m else None
    if not job:
        log("FATAL no job"); return 2
    log(f"job_id={job}")

    final = None
    for n in range(120):
        time.sleep(5)
        res, _ = call("tools/call", {"name": "get_render_status", "arguments": {"render_job_id": job}})
        t = tool_text(res)
        try:
            st = json.loads(t)
        except Exception:
            st = {"raw": t}
        log(f"[{n*5:>4}s] status={st.get('status')}")
        if st.get("status") in ("published", "failed"):
            final = st
            break
    if not final:
        log("TIMEOUT"); return 2

    # verify
    proj = os.path.abspath(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects", PROJECT_ID))
    vids = []
    if os.path.isdir(proj):
        for root, _, files in os.walk(proj):
            for f in files:
                if f.lower().endswith((".mp4", ".webm", ".mov")):
                    vids.append(os.path.join(root, f))
    log("=" * 50)
    log(f"FINAL status={final.get('status')} share={final.get('share_url')}")
    for v in sorted(vids):
        sz = os.path.getsize(v)
        log(f"video: {v} size={sz}")
        pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-select_streams", "v:0", "-show_entries", "stream=width,height,codec_name",
                             "-of", "json", v], capture_output=True, text=True)
        log(f"  ffprobe: {pr.stdout.strip()[:200]}")
    log(f"CHUNK CHAIN {'PASS' if final.get('status')=='published' and vids else 'FAIL'} elapsed={time.time()-t0:.0f}s")
    return 0 if (final.get("status") == "published" and vids) else 2

if __name__ == "__main__":
    open(LOG, "a", encoding="utf-8").write("\n===== RUN " + time.strftime("%Y-%m-%d %H:%M:%S") + " =====\n")
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        with open(LOG, "a", encoding="utf-8") as f:
            traceback.print_exc(file=f)
        sys.exit(2)
