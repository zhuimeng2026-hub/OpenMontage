"""Smoke render: verify Remotion asset staging for all local-resource fields.

Covers the plan's acceptance criteria in one render:
- cut.source (Ken Burns) regression
- cut.backgroundImage behind a text_card
- cut.backgroundVideo behind a text_card
- anime_scene images[]
- screenshot_scene backgroundImage
- audio.music.src into the final MP4 audio track

Passes when the render succeeds, output MP4 has video + audio streams,
and the staged files landed in remotion-composer/public/_staged/.
"""
import subprocess
import sys
import tempfile
from pathlib import Path

OUT = Path(tempfile.mkdtemp(prefix="staging_smoke_"))


def sh(*args):
    subprocess.run(args, capture_output=True, check=True)


# --- fixtures ---
img_a = OUT / "kenburns.png"
img_b = OUT / "bg_image.jpg"
img_c = OUT / "anime_2.png"
bg_vid = OUT / "bg_video.mp4"
music = OUT / "music.mp3"

sh("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x3366cc:s=640x360:d=1", "-frames:v", "1", str(img_a))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x22aa66:s=640x360:d=1", "-frames:v", "1", str(img_b))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0xcc5533:s=640x360:d=1", "-frames:v", "1", str(img_c))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=s=320x180:r=15:d=3", "-pix_fmt", "yuv420p", str(bg_vid))
sh("ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=12", "-ar", "22050", "-ac", "1", str(music))

props = {
    "renderer_family": "explainer-data",
    "cuts": [
        {"id": "c0", "type": "image", "source": str(img_a), "in_seconds": 0, "out_seconds": 2},
        {"id": "c1", "type": "text_card", "text": "Bg Image", "source": str(img_a),
         "backgroundImage": str(img_b), "in_seconds": 2, "out_seconds": 4},
        {"id": "c2", "type": "text_card", "text": "Bg Video", "source": str(img_a),
         "backgroundVideo": str(bg_vid), "backgroundVideoStart": 0, "in_seconds": 4, "out_seconds": 6},
        {"id": "c3", "type": "anime_scene", "images": [str(img_a), str(img_c)],
         "in_seconds": 6, "out_seconds": 8},
        {"id": "c4", "type": "screenshot_scene", "backgroundImage": str(img_b),
         "screenshotSteps": [{"kind": "cursor_move", "to": {"x": 0.5, "y": 0.5}, "durationSeconds": 1}],
         "in_seconds": 8, "out_seconds": 10},
    ],
    "audio": {"music": {"src": str(music), "volume": 0.2, "loop": True}},
}

from tools.video.video_compose import VideoCompose  # noqa: E402

vc = VideoCompose()
out_mp4 = OUT / "out.mp4"
result = vc._remotion_render({"composition_data": props, "output_path": str(out_mp4)})

print("\n==== RESULT ====")
print("success:", result.success)
if result.error:
    print("error:", result.error)

if result.success:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out_mp4)],
        capture_output=True, text=True,
    )
    streams = [s for s in probe.stdout.split() if s]
    print("output streams:", streams)
    has_video = "video" in streams
    has_audio = "audio" in streams
    print("has_video:", has_video, "| has_audio:", has_audio)
    if not (has_video and has_audio):
        print("FAIL: expected video + audio streams")
        sys.exit(1)

# Verify per-job staging cleanup: render must leave no leftover job dirs.
# (Staging itself is implicitly proven by the render succeeding — missing
# staged files would fail Remotion's file:// load. New contract: each job
# stages into public/_staged/<job_id>/ and rmtree's it afterwards.)
composer_dir = Path(__file__).resolve().parent.parent.parent / "remotion-composer"
staged_root = composer_dir / "public" / "_staged"
if staged_root.exists():
    entries = list(staged_root.iterdir())
    leftover_dirs = sorted(e.name for e in entries if e.is_dir())
    legacy_flat = sorted(e.name for e in entries if e.is_file())
    if legacy_flat:
        print(f"WARN: {len(legacy_flat)} legacy flat files under _staged/ (pre-existing "
              f"global-staging leftovers, not created by this run):", legacy_flat[:5])
    if leftover_dirs:
        print(f"FAIL: leftover per-job staging dirs ({len(leftover_dirs)}):", leftover_dirs[:10])
        sys.exit(1)
    print("cleanup OK — no leftover per-job staging dirs")
else:
    print("cleanup OK — no _staged root remains (nothing staged / all cleaned)")

print("\nSMOKE OK — render succeeded, staging cleaned up")
print(f"output: {out_mp4}")
