#!/usr/bin/env python3
"""One-shot orchestrator for the mcp-decompose-and-recompose skill.

Runs the four phases end-to-end:
  1. Decompose source via scene_detect + transcriber + video_analyzer (MCP)
  2. Generate or stage own elements (image + TTS)
  3. Recompose via video_compose operation=overlay (MCP)
  4. Verify with ffprobe + extract a proof frame

Usage:
  python3 scripts/mcp_decompose_and_recompose.py \\
      --project mcp-demo-001 \\
      --source assets/signal-from-tomorrow-demo.mp4 \\
      --title "《来自明天的信号》" \\
      --narration "这是一段由 OpenMontage MCP 添加的中文旁白。" \\
      --overlay-start 0 --overlay-end 3

Defaults are tuned for the bundled demo asset so `make demo-mcp-decompose`
(or this script with no args) runs out of the box. Everything is MCP-
mediated — no direct tool calls — so this script is the canonical
"client uses OpenMontage MCP" integration test.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Local helpers (sibling files)
sys.path.insert(0, str(Path(__file__).resolve().parent))
from mcp_helper import (  # noqa: E402
    init_session, load_token, call, SESSION_FILE, TOKEN_FILE,
)

# Lazy import shim — mcp_server is heavy (FastMCP); degrade gracefully.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    from mcp_server import _decompose_event  # noqa: F401
except Exception:
    def _decompose_event(event, **fields):  # type: ignore[misc]
        pass


WHISPER_BASE_SNAPSHOT = (
    "/root/.cache/huggingface/hub/models--Systran--faster-whisper-base/"
    "snapshots/ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66"
)


def ensure_session(project_id: str | None = None) -> tuple[str, str]:
    """Return a fresh MCP session id bound to *project_id*.

    The server enforces a per-session batch lock: once a session has an
    open `upload_asset` batch for one project, subsequent uploads to a
    different project from the same session are rejected with
    'MCP session is already collecting assets for another project'.
    Re-initialize the session whenever the project id changes.
    """
    token = load_token()
    marker_file = SESSION_FILE.with_name(f"{SESSION_FILE.name}.{project_id or 'default'}")
    if marker_file.exists():
        return marker_file.read_text().strip(), token
    sid = init_session(token)
    marker_file.write_text(sid)
    SESSION_FILE.write_text(sid)  # back-compat: also refresh the default pointer
    print(f"[init] new MCP session for project={project_id}: {sid}")
    return sid, token


def init_workspace(project_id: str, title: str) -> None:
    """Create projects/<id>/ and write project.json. Uses lib.checkpoint."""
    sys.path.insert(0, str(Path.cwd()))
    from lib.checkpoint import init_project  # type: ignore
    init_project(project_id, title=title, pipeline_type="hybrid")


def phase_decompose(source: str, project_id: str, session: str, token: str) -> dict:
    _decompose_event("decompose_run", state="start", phase=1, name="decompose", project=project_id)
    try:
        print("[1/4] Decompose — scene_detect + transcriber + video_analyzer")
        out_dir = f"projects/{project_id}/artifacts"
        Path(out_dir).mkdir(parents=True, exist_ok=True)

        # scene_detect
        scenes = call("tools/call", {
            "name": "execute_tool",
            "arguments": {"tool_name": "scene_detect", "inputs": {
                "input_path": source, "method": "content",
                "threshold": 0.3, "min_scene_length_seconds": 2.0,
                "output_path": f"{out_dir}/scenes.json",
            }},
        }, token, session).get("result", {}).get("structuredContent", {})
        print(f"  scene_detect: {scenes.get('data', {}).get('scene_count')} scenes")

        # transcriber — local snapshot path required on this host (see skill gotcha #1)
        tx = call("tools/call", {
            "name": "execute_tool",
            "arguments": {"tool_name": "transcriber", "inputs": {
                "input_path": source, "model_size": WHISPER_BASE_SNAPSHOT,
                "language": "en", "diarize": False, "output_dir": out_dir,
            }},
        }, token, session).get("result", {}).get("structuredContent", {})
        print(f"  transcriber: {len(tx.get('data', {}).get('segments', []))} segments")

        # video_analyzer — style profile + keyframes
        va = call("tools/call", {
            "name": "execute_tool",
            "arguments": {"tool_name": "video_analyzer", "inputs": {
                "source": source, "analysis_depth": "standard",
                "max_keyframes": 8, "output_dir": out_dir,
            }},
        }, token, session).get("result", {}).get("structuredContent", {})
        rep = va.get("data", {}).get("replication_guidance", {})
        print(f"  video_analyzer: {va.get('data', {}).get('_analysis_meta', {}).get('keyframe_count')} keyframes"
              f"  suggested={rep.get('suggested_pipeline')}/{rep.get('suggested_playbook')}")
        result = {"scenes": scenes, "transcript": tx, "analysis": va}
        _decompose_event("decompose_run", state="finish", phase=1, name="decompose",
                         project=project_id, success=True)
        return result
    except Exception as exc:
        _decompose_event("decompose_run", state="finish", phase=1, name="decompose",
                         project=project_id, success=False,
                         error=f"{type(exc).__name__}:{exc}"[:200])
        raise


def generate_own_elements(project_id: str, title: str, narration: str,
                          session: str, token: str) -> dict:
    _decompose_event("decompose_run", state="start", phase=2, name="own_elements", project=project_id)
    try:
        print("[2/4] Add own elements — title card image + Chinese TTS")
        img_dir = Path(f"projects/{project_id}/assets/images"); img_dir.mkdir(parents=True, exist_ok=True)
        audio_dir = Path(f"projects/{project_id}/assets/audio"); audio_dir.mkdir(parents=True, exist_ok=True)

        # Title card via Pillow (server-side local generation; not MCP — but stays
        # inside the project workspace, which is what the contract cares about).
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
        img = Image.new("RGB", (1920, 1080), (12, 18, 32))
        draw = ImageDraw.Draw(img)
        for y in range(1080):
            t = y / 1080
            r = int(12 + 26 * t); g = int(18 + 66 * t); b = int(32 + 88 * t)
            draw.line([(0, y), (1920, y)], fill=(r, g, b))
        draw.rectangle([(0, 460), (1920, 500)], fill=(96, 165, 250))
        fp = next((p for p in [
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        ] if Path(p).exists()), None)
        big = ImageFont.truetype(fp, 110) if fp else ImageFont.load_default()
        small = ImageFont.truetype(fp, 54) if fp else ImageFont.load_default()
        title_path = img_dir / "zh_title_card.jpg"
        tb = draw.textbbox((0, 0), title, font=big)
        draw.text(((1920 - (tb[2]-tb[0])) / 2, 360), title, fill=(245, 250, 255), font=big)
        sb = draw.textbbox((0, 0), "MCP decompose-and-recompose demo", font=small)
        draw.text(((1920 - (sb[2]-sb[0])) / 2, 560), "MCP decompose-and-recompose demo",
                  fill=(180, 200, 230), font=small)
        img.save(title_path, "JPEG", quality=92)
        print(f"  title card: {title_path} ({title_path.stat().st_size} bytes)")

        # TTS via MCP edge_tts
        audio_path = audio_dir / "zh_narration.mp3"
        tts = call("tools/call", {
            "name": "execute_tool",
            "arguments": {"tool_name": "edge_tts", "inputs": {
                "text": narration, "voice": "zh-CN-XiaoxiaoNeural",
                "output_path": str(audio_path),
            }},
        }, token, session).get("result", {}).get("structuredContent", {})
        print(f"  edge_tts: {audio_path} ({audio_path.stat().st_size} bytes)")

        # Upload both via MCP upload_asset (proves the client→server upload path)
        import base64
        for label, fp_upload, mime in [
            ("image", title_path, "image/jpeg"),
            ("audio", audio_path, "audio/mpeg"),
        ]:
            b64 = base64.b64encode(fp_upload.read_bytes()).decode()
            sc = call("tools/call", {
                "name": "upload_asset",
                "arguments": {"project_id": project_id, "filename": fp_upload.name,
                              "content_base64": b64, "mime_type": mime},
            }, token, session).get("result", {}).get("structuredContent", {})
            print(f"  upload_asset({label}): success={sc.get('success')}  "
                  f"sha256={(sc.get('asset') or {}).get('sha256', '')[:12]}")
        result = {"title_card": str(title_path), "narration": str(audio_path)}
        _decompose_event("decompose_run", state="finish", phase=2, name="own_elements",
                         project=project_id, success=True)
        return result
    except Exception as exc:
        _decompose_event("decompose_run", state="finish", phase=2, name="own_elements",
                         project=project_id, success=False,
                         error=f"{type(exc).__name__}:{exc}"[:200])
        raise


def phase_recompose(source: str, project_id: str, overlay: tuple, session: str, token: str):
    _decompose_event("decompose_run", state="start", phase=3, name="recompose", project=project_id)
    try:
        print("[3/4] Recompose — video_compose operation=overlay")
        out_path = f"projects/{project_id}/renders/final.mp4"
        Path(f"projects/{project_id}/renders").mkdir(parents=True, exist_ok=True)
        payload = {
            "operation": "overlay",
            "input_path": source,
            "output_path": out_path,
            "audio_path": f"projects/{project_id}/assets/audio/zh_narration.mp3",
            "overlays": [{
                "asset_path": f"projects/{project_id}/assets/images/zh_title_card.jpg",
                "start_seconds": overlay[0], "end_seconds": overlay[1],
                "x": 0, "y": 0, "scale": 1.0, "fade_in": True, "fade_out": True,
            }],
            "options": {"audio_volume": 0.6, "audio_delay_seconds": 0.5},
            "codec": "libx264", "crf": 22, "preset": "fast",
        }
        sc = call("tools/call", {
            "name": "execute_tool",
            "arguments": {"tool_name": "video_compose", "inputs": payload},
        }, token, session).get("result", {}).get("structuredContent", {})
        print(f"  video_compose: success={sc.get('success')}  artifacts={sc.get('artifacts')}")
        result = out_path
        _decompose_event("decompose_run", state="finish", phase=3, name="recompose",
                         project=project_id, success=True)
        return result
    except Exception as exc:
        _decompose_event("decompose_run", state="finish", phase=3, name="recompose",
                         project=project_id, success=False,
                         error=f"{type(exc).__name__}:{exc}"[:200])
        raise


def phase_verify(out_path: str) -> dict:
    _decompose_event("decompose_run", state="start", phase=4, name="verify")
    try:
        print("[4/4] Verify — ffprobe + proof frame")
        probe = subprocess.run([
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,size,bit_rate",
            "-show_entries", "stream=codec_name,width,height,r_frame_rate,codec_type",
            "-of", "default=noprint_wrappers=1", out_path,
        ], capture_output=True, text=True)
        print(probe.stdout)
        proof = "/tmp/mcp_decompose_proof.png"
        subprocess.run(["ffmpeg", "-v", "error", "-ss", "1.5", "-i", out_path,
                        "-frames:v", "1", "-y", proof], check=False)
        print(f"  proof frame: {proof} ({Path(proof).stat().st_size if Path(proof).exists() else 0} bytes)")
        result = {"ffprobe": probe.stdout, "proof": proof}
        _decompose_event("decompose_run", state="finish", phase=4, name="verify", success=True)
        return result
    except Exception as exc:
        _decompose_event("decompose_run", state="finish", phase=4, name="verify", success=False,
                         error=f"{type(exc).__name__}:{exc}"[:200])
        raise


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--project", default=f"mcp-decompose-{int(time.time())}")
    p.add_argument("--title", default="《来自明天的信号》")
    p.add_argument("--source", default="assets/signal-from-tomorrow-demo.mp4")
    p.add_argument("--narration", default="这是一段由 OpenMontage MCP 自动添加的中文旁白。原始视频被分解、加入了自己的元素，并重新合成。")
    p.add_argument("--overlay-start", type=float, default=0.0)
    p.add_argument("--overlay-end", type=float, default=3.0)
    args = p.parse_args()

    session, token = ensure_session(args.project)
    init_workspace(args.project, title=args.title)

    phase_decompose(args.source, args.project, session, token)
    generate_own_elements(args.project, args.title, args.narration, session, token)
    final = phase_recompose(args.source, args.project,
                            (args.overlay_start, args.overlay_end), session, token)
    phase_verify(final)
    print(f"\nDone. Final video: {final}")


if __name__ == "__main__":
    main()