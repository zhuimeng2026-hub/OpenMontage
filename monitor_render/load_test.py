#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""MCP chunk-upload load test — mimics BFF script-mode traffic at high concurrency.
Goal: force the asyncio.to_thread wedge and reveal it via tool.sync logs.
"""
import base64, io, json, os, sys, threading, time, traceback
import requests
from PIL import Image

TOKEN = os.environ.get("MCP_API_TOKEN", "").strip()
URL = "http://127.0.0.1:8900/mcp"
DURATION = float(os.environ.get("LOAD_DURATION", "120"))

def make_png(seed):
    img = Image.new("RGB", (64, 64), ((seed * 40) % 256, (seed * 70 + 30) % 256, (seed * 20 + 80) % 256))
    b = io.BytesIO(); img.save(b, "PNG"); return b.getvalue()

def parse(r):
    for line in r.text.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            try: return json.loads(line[5:].strip())
            except Exception: pass
    try: return json.loads(r.text)
    except Exception: return {"raw": r.text[:300]}

def tool_text(res):
    c = res.get("result", {}).get("content", [{}])
    return c[0].get("text", "") if c else json.dumps(res)[:200]

def run_session(idx):
    s = requests.Session()
    h = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json",
         "Accept": "application/json, text/event-stream"}
    rid = [idx * 10000]
    def call(method, params=None, note=False, timeout=20):
        rid[0] += 1
        b = {"jsonrpc": "2.0", "method": method}
        if not note: b["id"] = rid[0]
        if params is not None: b["params"] = params
        r = s.post(URL, headers=h, json=b, timeout=timeout)
        sid = r.headers.get("Mcp-Session-Id")
        if sid: h["Mcp-Session-Id"] = sid
        return r
    # init
    try:
        r = call("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                "clientInfo": {"name": f"load-{idx}", "version": "1"}})
        call("notifications/initialized", note=True)
    except Exception as e:
        print(f"[s{idx}] init FAIL {e}"); return
    started = time.time()
    ops = 0
    while time.time() - started < DURATION:
        try:
            data = make_png(idx)
            total = len(data)
            r = call("tools/call", {"name": "upload_asset_chunk", "arguments": {
                "operation": "start", "project_id": "frameflow-default",
                "filename": f"load-{idx}-{ops}.png", "total_bytes": total,
                "mime_type": "image/png"}}, timeout=15)
            if r.status_code != 200:
                print(f"[s{idx}] start HTTP {r.status_code}"); break
            res = parse(r)
            t = tool_text(res)
            if '"success": true' not in t:
                print(f"[s{idx}] start result NOT success: {t[:150]}"); break
            st = json.loads(t)
            uid = st["upload_id"]
            cb64 = base64.b64encode(data).decode()
            r = call("tools/call", {"name": "upload_asset_chunk", "arguments": {
                "operation": "append", "upload_id": uid, "offset": 0, "chunk_base64": cb64}}, timeout=15)
            r = call("tools/call", {"name": "upload_asset_chunk", "arguments": {
                "operation": "complete", "upload_id": uid}}, timeout=15)
            ops += 1
            if ops % 10 == 0:
                print(f"[s{idx}] {ops} uploads OK elapsed={time.time()-started:.0f}s")
        except Exception as e:
            print(f"[s{idx}] op FAIL after {ops}: {type(e).__name__} {e}")
            break
    print(f"[s{idx}] done ops={ops}")

def main():
    n = int(os.environ.get("LOAD_SESSIONS", "6"))
    print(f"load test: {n} sessions x {DURATION}s, URL={URL}")
    threads = [threading.Thread(target=run_session, args=(i,), daemon=True) for i in range(n)]
    for t in threads: t.start()
    for t in threads: t.join(timeout=DURATION + 60)
    print("load test finished")

if __name__ == "__main__":
    try: main()
    except Exception: traceback.print_exc()
