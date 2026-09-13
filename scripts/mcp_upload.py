#!/usr/bin/env python3
"""Upload one or more local files into an OpenMontage project via MCP upload_asset.

Usage:
  python3 scripts/mcp_upload.py --project my-demo \\
      assets/images/zh_title_card.jpg assets/audio/zh_narration.mp3

Reads MCP_API_TOKEN from .env, session id from /tmp/mcp_session.txt
(initialized by `scripts/mcp_helper.py init`). Each file is uploaded
independently; failures don't stop the batch. After upload the script
copies / symlinks each file from the session-isolated staging directory
(`projects/<pid>/assets/_sessions/<hash>/`) into the canonical
`projects/<pid>/assets/{images,audio}/` tree so `video_compose` can
reference the project paths directly.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from pathlib import Path

TOKEN_FILE = Path(os.environ.get("MCP_TOKEN_FILE", ".env"))
SESSION_FILE = Path(os.environ.get("MCP_SESSION_FILE", "/tmp/mcp_session.txt"))
# Override the MCP endpoint with OPENMONTAGE_MCP_URL; useful for running this
# script against a remote deployment (e.g. http://192.168.20.173:8900/mcp)
# without having to edit the source. Defaults to the local dev port.
MCP_URL = os.environ.get("OPENMONTAGE_MCP_URL", "http://localhost:8900/mcp")


def load_token() -> str:
    if not TOKEN_FILE.exists():
        sys.exit(f"Token file {TOKEN_FILE} not found")
    for line in TOKEN_FILE.read_text().splitlines():
        if line.startswith("MCP_API_TOKEN="):
            return line.split("=", 1)[1].strip()
    sys.exit(f"MCP_API_TOKEN not found in {TOKEN_FILE}")


def mcp_call(name: str, arguments: dict, token: str, session: str):
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}}
    req = urllib.request.Request(
        MCP_URL,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {token}",
                 "mcp-session-id": session},
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            payload = r.read().decode()
    except urllib.error.HTTPError as e:
        payload = e.read().decode()
    return json.loads(payload)


def guess_mime(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif",
        ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
        ".mp4": "video/mp4", ".mov": "video/quicktime", ".webm": "video/webm",
        ".srt": "application/x-subrip", ".vtt": "text/vtt",
    }.get(ext, "application/octet-stream")


def category_for(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        return "images"
    if ext in {".mp3", ".wav", ".m4a"}:
        return "audio"
    if ext in {".mp4", ".mov", ".webm"}:
        return "video"
    if ext in {".srt", ".vtt"}:
        return "subtitles"
    return "misc"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", required=True, help="OpenMontage project id")
    p.add_argument("files", nargs="+", help="Files to upload")
    p.add_argument("--keep-session-path", action="store_true",
                   help="Don't copy files out of assets/_sessions/<hash>/")
    args = p.parse_args()

    token = load_token()
    session = (SESSION_FILE.read_text().strip()
               if SESSION_FILE.exists() else sys.exit(
                   f"No MCP session; run: python3 scripts/mcp_helper.py init"))

    for src in args.files:
        src_path = Path(src)
        if not src_path.exists():
            print(f"SKIP {src} (not found)")
            continue
        mime = guess_mime(src_path)
        b64 = base64.b64encode(src_path.read_bytes()).decode()
        print(f"Uploading {src_path} ({len(b64)} chars b64, {mime}) ...")
        resp = mcp_call("upload_asset", {
            "project_id": args.project,
            "filename": src_path.name,
            "content_base64": b64,
            "mime_type": mime,
        }, token, session)
        sc = resp.get("result", {}).get("structuredContent") or resp.get("result", {})
        ok = sc.get("success", False)
        rel = (sc.get("asset", {}) or {}).get("relative_path", "")
        print(f"  -> success={ok}  staged={rel}")
        if not ok:
            print(f"  !! upload failed: {sc.get('error')}")
            continue
        if args.keep_session_path:
            continue
        # Stage the canonical copy so video_compose can reference it.
        cat = category_for(src_path)
        canonical = Path(f"projects/{args.project}/assets/{cat}/{src_path.name}")
        canonical.parent.mkdir(parents=True, exist_ok=True)
        if rel:
            staged = Path(rel)
            if staged.exists():
                shutil.copy2(staged, canonical)
                print(f"  -> copied to {canonical}")
            else:
                # server may have written to absolute repo path; fall back to src
                shutil.copy2(src_path, canonical)
                print(f"  -> fallback copy from {src_path} to {canonical}")
        else:
            shutil.copy2(src_path, canonical)
            print(f"  -> copied from {src_path} to {canonical}")


if __name__ == "__main__":
    main()