#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenMontage 端到端测试（真实浏览器路径）。
协议：POST https://render.mengxa.com/api/mcp  {tool, arguments} -> {_text}
即 nginx(443) -> BFF(8080) -> MCP(8900) 的完整代理链路。
"""
import base64, io, json, os, sys, time, traceback
import requests
from PIL import Image, ImageDraw

TOKEN = "h6LQUTVPA5vBmqXijUydpockVrPx2ruUqPaVQRT6WJE"
BFF = "https://render.mengxa.com/api/mcp"
PROJECT_ID = "e2e-browser-" + time.strftime("%Y%m%d-%H%M%S")
N_IMAGES = 3

def log(*a):
    print("[e2e-browser]", *a, flush=True)

def make_test_png(idx, w=540, h=960):
    img = Image.new("RGB", (w, h), ((idx*60)%256, (idx*97+30)%256, (idx*130+80)%256))
    d = ImageDraw.Draw(img)
    for y in range(0, h, 40):
        d.line([(0, y), (w, y)], fill=(255, 255, 255), width=2)
    d.text((40, h//2), f"OpenMontage\nTest {idx+1}", fill=(255, 255, 255))
    buf = io.BytesIO(); img.save(buf, format="PNG"); return buf.getvalue()

_session = requests.Session()
_session.headers.update({"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})

def bff_call(tool, arguments):
    """真实浏览器协议：POST /api/mcp {tool, args}（BFF 用 args 承载参数）。
    用 Session 保持 ff_sid cookie，使上传与渲染落在同一 MCP 会话。"""
    r = _session.post(BFF, json={"tool": tool, "args": arguments}, timeout=120, verify=False)
    r.raise_for_status()
    body = r.json()
    # BFF 错误时包裹 {"_text": "..."}；成功时直接返回工具结果对象
    if "_text" in body:
        text = body["_text"]
        try:
            return json.loads(text), r.status_code
        except Exception:
            return {"_raw": text, "success": False}, r.status_code
    return body, r.status_code

def main():
    log(f"BFF 路径: {BFF}")
    # 1) 上传测试图
    uploaded = []
    for i in range(N_IMAGES):
        png = make_test_png(i)
        b64 = base64.b64encode(png).decode()
        res, code = bff_call("upload_asset", {
            "project_id": PROJECT_ID,
            "filename": f"scene_{i+1}.png",
            "content_base64": b64,
            "mime_type": "image/png",
        })
        ok = res.get("success") is True
        log(f"上传 scene_{i+1}.png -> HTTP {code} success={ok} | {json.dumps(res, ensure_ascii=False)[:160]}")
        uploaded.append(ok)

    if not all(uploaded):
        log("部分图片上传失败，终止渲染"); sys.exit(3)

    # 2) 触发渲染
    res, code = bff_call("create_remotion_video_share", {
        "project_id": PROJECT_ID,
        "script_id": "photo-ken-burns",
        "duration_per_image": 3.0,
        "aspect_ratio": "9:16",
        "title": "E2E 浏览器路径测试",
    })
    log(f"create_remotion_video_share -> HTTP {code} | {json.dumps(res, ensure_ascii=False)[:300]}")
    job_id = res.get("render_job_id")
    if not job_id:
        log("未取得 render_job_id，终止"); sys.exit(4)
    log(f"render_job_id = {job_id}")

    # 3) 轮询
    final = None
    for t in range(0, 60):
        res, code = bff_call("get_render_status", {"render_job_id": job_id})
        status = res.get("status")
        log(f"[{t*10:>3}s] HTTP {code} status={status} | {json.dumps(res, ensure_ascii=False)[:220]}")
        if status in ("published", "failed", "rendered"):
            final = res; break
        time.sleep(10)

    # 4) 核实本地产物
    proj_dir = os.path.abspath(os.path.join(os.getcwd(), "..", "projects", PROJECT_ID))
    log(f"项目目录: {proj_dir}")
    vids = []
    if os.path.isdir(proj_dir):
        for root, _, files in os.walk(proj_dir):
            for f in files:
                if f.lower().endswith((".mp4", ".webm", ".mov")):
                    vids.append((os.path.join(root, f), os.path.getsize(os.path.join(root, f))))
    for p, s in vids:
        log(f"产物视频: {p} ({s} bytes)")

    log("="*60)
    log("E2E(浏览器路径) 结果:")
    log(f"  上传图片      : {sum(uploaded)}/{len(uploaded)} 成功")
    log(f"  render_job_id : {job_id}")
    log(f"  最终状态      : {final.get('status') if final else 'timeout'}")
    if final:
        log(f"  share_url     : {final.get('share_url')}")
        log(f"  stage         : {final.get('stage')}")
        log(f"  message       : {str(final.get('message'))[:200]}")
    log(f"  本地视频产物  : {[p for p,_ in vids] if vids else '无'}")
    log("="*60)

if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc(); sys.exit(99)
