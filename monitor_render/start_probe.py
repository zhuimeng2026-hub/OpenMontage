#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Minimal upload_asset_chunk start probe — decide whether server-side start hangs."""
import json, os, subprocess, sys, tempfile, time, urllib.request

TOKEN = os.environ.get("MCP_API_TOKEN", "").strip()
URL = "http://192.168.20.173:8900/mcp"

def curl(payload, timeout=20):
    hf = tempfile.NamedTemporaryFile("w+", delete=False); bf = tempfile.NamedTemporaryFile("w+", delete=False)
    hf.close(); bf.close()
    cmd = ["curl", "-sS", "--max-time", str(timeout), "-D", hf.name, "-o", bf.name,
           "-H", "Content-Type: application/json", "-H", "Accept: application/json, text/event-stream",
           "-H", "Authorization: Bearer " + TOKEN, "-d", json.dumps(payload), URL]
    t0 = time.time()
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    sid = None
    try:
        for line in open(hf.name, encoding="utf-8", errors="replace"):
            if line.lower().startswith("mcp-session-id:"):
                sid = line.split(":", 1)[1].strip()
    except OSError:
        pass
    body = ""
    try:
        body = open(bf.name, encoding="utf-8", errors="replace").read()[:400]
    except OSError:
        pass
    for fp in (hf.name, bf.name):
        try: os.remove(fp)
        except OSError: pass
    return r.returncode, sid, body, round((time.time() - t0) * 1000)

def main():
    rc, sid, body, ms = curl({"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "start-probe", "version": "1"}}})
    print(f"initialize rc={rc} sid={'yes' if sid else 'NO'} ms={ms} body={body[:120]}")
    if not sid:
        print("FAIL: no session"); return 2
    # start upload chunk
    payload = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
               "params": {"name": "upload_asset_chunk", "arguments": {
                   "operation": "start", "project_id": "start-probe-" + str(int(time.time())),
                   "filename": "test.png", "total_bytes": 100, "mime_type": "image/png"}}}
    hdr = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "Authorization": "Bearer " + TOKEN, "Mcp-Session-Id": sid}
    # use requests-like via curl with session header
    hf = tempfile.NamedTemporaryFile("w+", delete=False); bf = tempfile.NamedTemporaryFile("w+", delete=False)
    hf.close(); bf.close()
    pf = tempfile.NamedTemporaryFile("w+", delete=False, encoding="utf-8")
    json.dump(payload, pf); pf.close()
    t0 = time.time()
    cmd = ["curl", "-sS", "--max-time", "20", "-D", hf.name, "-o", bf.name,
           "-H", "Content-Type: application/json", "-H", "Accept: application/json, text/event-stream",
           "-H", "Authorization: Bearer " + TOKEN, "-H", "Mcp-Session-Id: " + sid,
           "-d", "@" + pf.name, URL]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    ms = round((time.time() - t0) * 1000)
    try:
        body = open(bf.name, encoding="utf-8", errors="replace").read()[:500]
    except OSError:
        body = ""
    code = ""
    try:
        for line in open(hf.name, encoding="utf-8", errors="replace"):
            if line.lower().startswith("http/"):
                parts = line.split(" ", 2)
                if len(parts) > 1: code = parts[1]
    except OSError:
        pass
    for fp in (hf.name, bf.name, pf.name):
        try: os.remove(fp)
        except OSError: pass
    print(f"start rc={r.returncode} http={code} ms={ms} body={body}")
    print("HANG" if ms >= 20000 or r.returncode == 28 else "OK" if "upload_id" in body else "CHECK")
    return 0

if __name__ == "__main__":
    sys.exit(main())
