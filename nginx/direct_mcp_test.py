#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""直连 MCP(8900)，维持同一 Mcp-Session-Id，验证 上传->渲染->视频 全链路。
用于证明：后端管线本身正常，问题仅在 BFF 未保持 MCP 会话亲和。"""
import base64, io, json, os, re, time, traceback
import requests
from PIL import Image, ImageDraw

TOKEN = os.environ.get("MCP_API_TOKEN", "").strip()
URL = "http://localhost:8900/mcp"
PROJECT_ID = "e2e-direct-" + time.strftime("%Y%m%d-%H%M%S")

def log(*a): print("[direct-mcp]", *a, flush=True)

def png(i, w=540, h=960):
    img = Image.new("RGB", (w, h), ((i*60)%256, (i*97+30)%256, (i*130+80)%256))
    d = ImageDraw.Draw(img); d.text((40, h//2), f"Test {i+1}", fill=(255,255,255))
    buf = io.BytesIO(); img.save(buf, "PNG"); return base64.b64encode(buf.getvalue()).decode()

def parse(r):
    """从 SSE 或纯 JSON 响应里抽取 JSON 对象。"""
    text = r.text
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try: return json.loads(line[5:].strip())
            except Exception: pass
    try: return json.loads(text)
    except Exception: return {"raw": text}

def main():
    if not TOKEN:
        raise SystemExit("MCP_API_TOKEN is required for direct MCP tests")
    s = requests.Session()
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    rid = [0]
    def call(method, params=None, note=False):
        rid[0]+=1
        b = {"jsonrpc":"2.0","method":method}
        if not note: b["id"]=rid[0]
        if params is not None: b["params"]=params
        r = s.post(URL, headers=h, json=b, timeout=120)
        r.raise_for_status()
        sid = r.headers.get("Mcp-Session-Id")
        if sid: h["Mcp-Session-Id"]=sid
        return parse(r), sid
    init, sid = call("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"dm","version":"0.1"}})
    log(f"initialize sid={sid} serverInfo={init.get('result',{}).get('serverInfo')}")
    call("notifications/initialized", note=True)
    for i in range(3):
        res, _ = call("tools/call", {"name":"upload_asset","arguments":{"project_id":PROJECT_ID,"filename":f"s{i}.png","content_base64":png(i),"mime_type":"image/png"}})
        txt = res.get("result",{}).get("content",[{}])
        t = txt[0].get("text","") if txt else json.dumps(res)[:200]
        log(f"upload s{i}.png -> {t[:140]}")
    res, _ = call("tools/call", {"name":"create_remotion_video_share","arguments":{"project_id":PROJECT_ID,"script_id":"photo-ken-burns","duration_per_image":3.0,"aspect_ratio":"9:16","title":"direct"}})
    txt = res.get("result",{}).get("content",[{}])
    t = txt[0].get("text","") if txt else json.dumps(res)[:300]
    log(f"create_remotion_video_share -> {t[:320]}")
    m = re.search(r'"render_job_id"\s*:\s*"([\w-]+)"', t)
    job = m.group(1) if m else None
    log(f"job_id={job}")
    if not job:
        log("无 job_id，终止"); return
    final=None
    for n in range(60):
        res, _ = call("tools/call", {"name":"get_render_status","arguments":{"render_job_id":job}})
        txt = res.get("result",{}).get("content",[{}])
        t = txt[0].get("text","") if txt else json.dumps(res)[:300]
        try: st=json.loads(t)
        except: st={"raw":t}
        log(f"[{n*10:>3}s] {st.get('status')} | {t[:200]}")
        if st.get("status") in ("published","failed","rendered"):
            final=st; break
        time.sleep(10)
    proj = os.path.abspath(os.path.join(os.getcwd(),"..","projects",PROJECT_ID))
    vids=[]
    if os.path.isdir(proj):
        for root,_,files in os.walk(proj):
            for f in files:
                if f.lower().endswith((".mp4",".webm",".mov")):
                    vids.append((os.path.join(root,f),os.path.getsize(os.path.join(root,f))))
    log("="*50)
    log(f"最终状态: {final.get('status') if final else 'timeout'}")
    log(f"share_url: {final.get('share_url') if final else None}")
    log(f"本地视频: {[ (p,sz) for p,sz in vids ]}")

if __name__=="__main__":
    try: main()
    except Exception: traceback.print_exc()
