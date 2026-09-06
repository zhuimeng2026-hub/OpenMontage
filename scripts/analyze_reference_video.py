#!/usr/bin/env python3
"""End-to-end reference video analysis: one command closes the loop.

Pipeline (per CLAUDE.md §"Reference Video Entry Point"):

  URL | <local.mp4>
        │
        ▼  downloader (only if URL — falls back to direct yt-dlp for sources
        │  whose format IDs are pre-muxed mp4, e.g. Weibo/Douyin/Xiaohongshu;
        │  video_downloader silently degrades to a JPG cover in that case
        │  — see docs/bugs/video-analyzer-keyframe-silent-failure-2026-09-05-fix.md)
        ▼  transcriber (Whisper faster-whisper, language=zh by default)
        ▼  video-understand (scene frames + 16 kHz mono audio)
        ▼  video_analyzer (structural skeleton — scenes, pacing, motion,
        │                 audio_energy, no LLM fill)
        ▼  video_brief_synthesizer (VLM fills content/style/replication fields)
        ▼  research_brief.json  (canonical artifact for downstream idea-director)

Usage:

    python scripts/analyze_reference_video.py <URL_OR_PATH>
    python scripts/analyze_reference_video.py <URL_OR_PATH> --project-id myproj \\
        --userid local-dev --frames 24 --language zh --max-tokens 4096

Outputs land at:

    projects/users/<userid>/<project-id>/source.mp4
    projects/users/<userid>/<project-id>/source_frames/  (keyframe jpgs + wav)
    projects/users/<userid>/<project-id>/transcript.txt / .srt
    projects/users/<userid>/<project-id>/analysis_<ts>/video_analysis_brief.json
    projects/users/<userid>/<project-id>/analysis_<ts>/research_brief.json

Set OPENMONTAGE_* env vars before invoking. ANTHROPIC_BASE_URL +
ANTHROPIC_AUTH_TOKEN must be present for the synthesizer step (the rest of
the pipeline works without them).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


def _log(msg: str) -> None:
    print(f"[analyze-reference] {msg}", flush=True)


def _download(url: str, ws_dir: Path, userid: str, project_id: str) -> Path:
    """Download a reference URL into the workspace.

    Uses the project's video_downloader first; if it returns
    video_path=None (known bug for Weibo / Douyin / Xiaohongshu — see
    docs/bugs/video-analyzer-keyframe-silent-failure-2026-09-05-fix.md for the
    matching format-selector bug), falls back to direct yt-dlp with the
    pre-muxed mp4_* format IDs those platforms expose.
    """
    from tools.tool_registry import registry

    registry.discover()
    dl = registry._tools["video_downloader"]
    res = dl.execute({
        "url": url,
        "userid": userid,
        "project_id": project_id,
        "format": "video",
        "max_resolution": "720p",
        "max_duration_seconds": 600,
    })
    if res.success and res.data.get("video_path"):
        return Path(res.data["video_path"])

    # Fallback — list formats and pick the best pre-muxed mp4_*
    _log(f"video_downloader returned video_path=None; falling back to direct yt-dlp (likely pre-muxed mp4 source: {url[:60]}...)")
    out = ws_dir / "source.mp4"
    list_proc = subprocess.run(
        [sys.executable, "-m", "yt_dlp", "-F", url],
        capture_output=True, text=True, timeout=60,
    )
    fmt_ids = re.findall(r"^(mp4_\w+)\s", list_proc.stdout, flags=re.MULTILINE)
    fmt_selector = "/".join(fmt_ids) if fmt_ids else "best"
    subprocess.run([
        sys.executable, "-m", "yt_dlp",
        "-f", fmt_selector,
        "--merge-output-format", "mp4",
        "-o", str(out),
        url,
    ], check=True, timeout=600)
    return out


def _extract_audio_and_frames(video_path: Path, frames_dir: Path, max_frames: int) -> None:
    """Run ffmpeg to extract audio + keyframes. No LLM in this step."""
    frames_dir.mkdir(parents=True, exist_ok=True)

    # Audio
    subprocess.run([
        "ffmpeg", "-y", "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        str(frames_dir / "_audio.wav"),
    ], check=True, capture_output=True, timeout=120)

    # Scene-detect frames
    cmd = [
        "ffmpeg", "-y", "-i", str(video_path),
        "-vf", "select='gt(scene,0.3)',showinfo",
        "-vsync", "vfr", "-q:v", "2",
        str(frames_dir / "frame_%04d.jpg"),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg scene detect failed: {proc.stderr[-400:]}")

    # Downsample if we got more than max_frames
    extracted = sorted(frames_dir.glob("frame_*.jpg"))
    if len(extracted) > max_frames:
        step = len(extracted) / max_frames
        keep = {extracted[int(i * step)] for i in range(max_frames)}
        for p in extracted:
            if p not in keep:
                p.unlink()


def _transcribe(audio_path: Path, ws_dir: Path, language: str) -> tuple[Path, Path]:
    """Run faster-whisper via the transcriber tool, then flatten to .txt + .srt."""
    from tools.tool_registry import registry

    registry.discover()
    tr = registry._tools["transcriber"]
    res = tr.execute({
        "input_path": str(audio_path),
        "language": language,
        "model_size": "base",
        "output_dir": str(ws_dir),
    })
    if not res.success:
        raise RuntimeError(f"transcriber failed: {res.error}")

    # Find the produced transcript
    candidates = [ws_dir / f"{audio_path.stem}_transcript.json"]
    if not candidates[0].exists():
        candidates = list(ws_dir.glob("*_transcript.json"))
    if not candidates:
        raise RuntimeError("transcriber returned no JSON")
    src = candidates[0]
    data = json.loads(src.read_text(encoding="utf-8"))
    segs = data.get("segments", [])

    def to_srt_ts(x):
        m, sec = divmod(float(x), 60); h, m = divmod(m, 60)
        return f"{int(h):02d}:{int(m):02d}:{sec:06.3f}".replace(".", ",")

    srt_lines = []
    flat = []
    for i, s in enumerate(segs, start=1):
        t = s.get("text", "").strip()
        if not t: continue
        flat.append(t)
        srt_lines += [str(i), f"{to_srt_ts(s['start'])} --> {to_srt_ts(s['end'])}", t, ""]
    joined = "".join(t if t[-1] in "。！？.!?？" else t + "，" for t in flat).rstrip("，")
    (ws_dir / "transcript.txt").write_text(joined, encoding="utf-8")
    (ws_dir / "transcript.srt").write_text("\n".join(srt_lines), encoding="utf-8")
    return src, ws_dir / "transcript.txt"


def _analyze(video_path: Path, ws_dir: Path, userid: str, project_id: str) -> Path:
    from tools.tool_registry import registry
    registry.discover()
    va = registry._tools["video_analyzer"]
    res = va.execute({
        "source": str(video_path),
        "userid": userid,
        "project_id": project_id,
        "analysis_depth": "deep",
        "max_keyframes": 24,
        "max_duration_seconds": 600,
    })
    if not res.success:
        raise RuntimeError(f"video_analyzer failed: {res.error}")
    return Path(res.data["_analysis_meta"].get("output_path") or res.artifacts[0])


def _synthesize(brief_path: Path, frames_dir: Path, transcript_path: Path,
                userid: str, project_id: str, max_frames: int, max_tokens: int) -> Path:
    from tools.tool_registry import registry
    registry.discover()
    syn = registry._tools["video_brief_synthesizer"]
    res = syn.execute({
        "brief_path": str(brief_path),
        "frames_dir": str(frames_dir),
        "transcript_path": str(transcript_path),
        "userid": userid,
        "project_id": project_id,
        "max_frames": max_frames,
        "max_tokens": max_tokens,
    })
    if not res.success:
        raise RuntimeError(f"video_brief_synthesizer failed: {res.error}")
    if (res.data or {}).get("synthesis", {}).get("status") != "ok":
        _log(f"synthesizer status={res.data.get('synthesis', {}).get('status')} reason={res.data.get('synthesis', {}).get('skip_reason')}")
        return None  # type: ignore
    return Path(res.data["output_path"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("source", help="URL or local .mp4 path")
    ap.add_argument("--userid", default=os.environ.get("OPENMONTAGE_USERID", "local-dev"))
    ap.add_argument("--project-id", default="references")
    ap.add_argument("--frames", type=int, default=24)
    ap.add_argument("--max-frames-vlm", type=int, default=16)
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--language", default="zh")
    ap.add_argument("--skip-download", action="store_true",
                    help="Source is already a local file")
    args = ap.parse_args()

    # 1. Workspace
    from lib.project_workspace import ProjectWorkspace, Principal
    principal = Principal(kind="user", principal_id=args.userid)
    ws = ProjectWorkspace.for_principal(principal, args.project_id)
    ws.root.mkdir(parents=True, exist_ok=True)
    _log(f"workspace: {ws.root}")

    t0 = time.time()

    # 2. Source video
    if args.skip_download or not args.source.startswith(("http://", "https://", "www.")):
        video_path = Path(args.source)
        if not video_path.exists():
            print(f"local file not found: {video_path}", file=sys.stderr)
            return 2
        _log(f"using local source: {video_path}")
    else:
        video_path = _download(args.source, ws.root, args.userid, args.project_id)
        _log(f"downloaded: {video_path}")

    # 3. Audio + frames
    frames_dir = ws.root / "source_frames"
    _extract_audio_and_frames(video_path, frames_dir, args.frames)
    _log(f"frames + audio extracted to {frames_dir}")

    # 4. Transcribe
    transcript_json, transcript_txt = _transcribe(
        frames_dir / "_audio.wav", ws.root, args.language,
    )
    _log(f"transcript: {transcript_txt}")

    # 5. Structural analysis
    brief_path = _analyze(video_path, ws.root, args.userid, args.project_id)
    _log(f"video_analyzer skeleton: {brief_path}")

    # 6. VLM synthesis (closes the loop)
    research_brief = _synthesize(
        brief_path, frames_dir, transcript_json,
        args.userid, args.project_id, args.max_frames_vlm, args.max_tokens,
    )

    elapsed = round(time.time() - t0, 2)
    print()
    print("=" * 70)
    if research_brief and research_brief.exists():
        b = json.loads(research_brief.read_text(encoding="utf-8"))
        syn = b.get("_analysis_meta", {}).get("synthesis", {})
        print(f"LOOP CLOSED in {elapsed}s")
        print(f"  output:    {research_brief}")
        print(f"  model:     {syn.get('model', '?')}")
        print(f"  frames:    {syn.get('frames_used', '?')}")
        print(f"  fields:    {len(syn.get('fields_filled', []))}")
        print(f"  summary:   {b['content_analysis']['summary'][:140]}...")
        print(f"  topics:    {len(b['content_analysis']['topics'])}")
        print(f"  playbook:  {b['replication_guidance']['suggested_playbook']}")
    else:
        print(f"PARTIAL LOOP (VLM step skipped/failed) in {elapsed}s")
        print(f"  skeleton:  {brief_path}")
        print("  next:      run video_brief_synthesizer manually after setting")
        print("             ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    sys.exit(main())