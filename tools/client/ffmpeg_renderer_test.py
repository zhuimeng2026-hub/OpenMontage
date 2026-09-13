"""Smoke tests + worked example for the client-side FFmpeg renderer.

Run with:
    python tools/client/ffmpeg_renderer_test.py

This file is *not* a pytest suite (the project pins pytest targets per-area);
it's a runnable smoke test that builds a realistic ``edit_decisions`` +
``asset_manifest`` from in-memory dicts, renders the plan, and prints the
exact FFmpeg commands a GUI client would execute.

To verify against a real FFmpeg binary, run:
    ffmpeg -version          # ensure FFmpeg >= 5.0 is on PATH
    python tools/client/ffmpeg_renderer_test.py --execute
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

# Make the file runnable both as a module and a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tools.client.ffmpeg_renderer import FFmpegRenderer, RenderPlan


# --- Sample artifacts (representative of video-template-remix output) ----


SAMPLE_EDIT_DECISIONS = {
    "version": "1.0",
    "render_runtime": "ffmpeg",
    "composition_mode": "templated",
    "renderer_family": "explainer-data",
    "compose_target": {
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "fit": "cover",
    },
    "cuts": [
        {
            "id": "c01_intro",
            "source": "source_ref_video",          # → asset_manifest id
            "in_seconds": 0.0,
            "out_seconds": 3.2,
            "overlay": {"asset_id": "user_img_001"},
            "transform": {
                "scale": 1.0,
                "position": "center",
                "animation": "ken-burns-slow-zoom",
            },
            "transition_in": "fade",
            "transition_duration": 0.3,
        },
        {
            "id": "c02_demo",                       # preserve source slot
            "source": "source_ref_video",
            "in_seconds": 3.2,
            "out_seconds": 6.8,
            "transform": {"animation": "ken-burns-slow-zoom"},
        },
        {
            "id": "c03_feature",
            "source": "source_ref_video",
            "in_seconds": 6.8,
            "out_seconds": 10.4,
            "overlay": {"asset_id": "user_img_002"},
            "transform": {"position": "center"},
        },
    ],
    "audio": {
        # remix_rules.preserve includes "source_audio"; explicit null confirms.
        "narration": None,
        "music": {"asset_id": None, "volume": 0.0, "ducking": False},
        "sfx": [],
    },
    "subtitles": {
        "enabled": True,
        "style": "sentence",
        "source": "source_subs",
        "position": "bottom-center",
        "font_size": 48,
        "color": "&H00FFFFFF",                     # ASS BGR format
        "outline_color": "&H00000000",
        "max_words_per_line": 8,
    },
}


SAMPLE_ASSET_MANIFEST = {
    "version": "1.0",
    "assets": [
        {
            "id": "source_ref_video",
            "type": "video",
            "path": "assets/reference/source.mp4",
            "source_tool": "video_downloader",
            "scene_id": "all",
        },
        {
            "id": "source_subs",
            "type": "subtitle",
            "path": "assets/reference/source.srt",
            "source_tool": "transcriber",
            "scene_id": "all",
        },
        {
            "id": "user_img_001",
            "type": "image",
            "path": "assets/user/uploads/hero.png",
            "source_tool": "user_upload",
            "scene_id": "c01_intro",
        },
        {
            "id": "user_img_002",
            "type": "image",
            "path": "assets/user/uploads/feature.png",
            "source_tool": "user_upload",
            "scene_id": "c03_feature",
        },
    ],
}


# --- Demo paths (synthesized at runtime if --execute) -----------------------


def setup_demo_files(project_root: Path, fast: bool = False) -> None:
    """Create dummy input files so FFmpeg can actually run.

    Real GUI clients would receive the real reference video and user images.
    Here we synthesize tiny valid MP4 + PNG + SRT files so the test is
    self-contained.

    ``fast=True`` makes the source video 3 seconds at low resolution so the
    end-to-end smoke test completes in seconds rather than minutes. This is
    useful for CI smoke validation; production renders use full-resolution
    media and accept the longer encode time.
    """
    import subprocess

    (project_root / "assets" / "reference").mkdir(parents=True, exist_ok=True)
    (project_root / "assets" / "user" / "uploads").mkdir(parents=True, exist_ok=True)

    src_mp4 = project_root / "assets" / "reference" / "source.mp4"
    if not src_mp4.exists():
        # Fast mode: 3s 320x180; production mode: 12s 1280x720.
        if fast:
            args = [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=duration=3:size=320x180:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=3",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "64k", "-shortest",
                str(src_mp4),
            ]
        else:
            args = [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                "testsrc2=duration=12:size=1280x720:rate=30",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
                "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-b:a", "64k", "-shortest",
                str(src_mp4),
            ]
        subprocess.run(args, check=True, capture_output=True)

    # Hero image: red PNG. In fast mode use 270x480 to keep the smoke test
    # fast; production mode uses the full 1080x1920 compose_target size.
    hero = project_root / "assets" / "user" / "uploads" / "hero.png"
    if not hero.exists():
        hero_size = "270x480" if fast else "1080x1920"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=c=red:s={hero_size}:d=1",
                "-frames:v", "1", str(hero),
            ],
            check=True, capture_output=True,
        )

    # Feature image: blue PNG (same sizing logic).
    feature = project_root / "assets" / "user" / "uploads" / "feature.png"
    if not feature.exists():
        feature_size = "270x480" if fast else "1080x1920"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i",
                f"color=c=blue:s={feature_size}:d=1",
                "-frames:v", "1", str(feature),
            ],
            check=True, capture_output=True,
        )

    # Subtitle file with three cues aligned to the three cuts.
    subs = project_root / "assets" / "reference" / "source.srt"
    if not subs.exists():
        if fast:
            # 3s source → 3 short cues of 1s each.
            subs_text = (
                "1\n00:00:00,000 --> 00:00:01,000\nHi.\n\n"
                "2\n00:00:01,000 --> 00:00:02,000\nDemo.\n\n"
                "3\n00:00:02,000 --> 00:00:03,000\nDone.\n"
            )
        else:
            subs_text = (
                "1\n00:00:00,000 --> 00:00:03,200\nWelcome to the demo.\n\n"
                "2\n00:00:03,200 --> 00:00:06,800\nWatch this in action.\n\n"
                "3\n00:00:06,800 --> 00:00:10,400\nHere's the new feature.\n"
            )
        subs.write_text(subs_text, encoding="utf-8")


# --- The actual demo -------------------------------------------------------


def build_plan(project_root: Path, fast: bool = False) -> RenderPlan:
    """Build the render plan from the sample artifacts.

    ``fast=True`` strips out ken-burns animations, compresses cut times,
    and shrinks the compose target to 360x640 so the smoke test runs in
    seconds. Use it for CI validation; production renders keep the
    animation fields and the full 1080x1920 target.
    """
    ed = dict(SAMPLE_EDIT_DECISIONS)
    if fast:
        # Disable the expensive ken-burns zoompan in smoke mode.
        for cut in ed["cuts"]:
            t = cut.get("transform") or {}
            t.pop("animation", None)
            cut["transform"] = t
        # Rescale cut times into the 3-second source video.
        cuts = ed["cuts"]
        bounds = [(0.0, 1.0), (1.0, 2.0), (2.0, 3.0)]
        for cut, (t_in, t_out) in zip(cuts, bounds):
            cut["in_seconds"] = t_in
            cut["out_seconds"] = t_out
            cut["transition_duration"] = 0.0  # cuts are tiny; skip xfade time
        # Shrink the compose target to keep the encode fast on CPU.
        ed["compose_target"] = {"width": 360, "height": 640, "fps": 30, "fit": "cover"}

    # Write sample artifacts to disk so the renderer can read them as it
    # would in production.
    ed_path = project_root / "edit_decisions.json"
    am_path = project_root / "asset_manifest.json"
    ed_path.write_text(json.dumps(ed, indent=2), encoding="utf-8")
    am_path.write_text(json.dumps(SAMPLE_ASSET_MANIFEST, indent=2), encoding="utf-8")

    renderer = FFmpegRenderer.from_artifacts(
        edit_decisions_path=ed_path,
        asset_manifest_path=am_path,
        project_root=project_root,
    )
    return renderer.build_plan()


def print_plan(plan: RenderPlan) -> None:
    """Pretty-print the plan as a runnable script."""
    print(f"=== Render plan: {len(plan)} steps ===\n")
    for i, step in enumerate(plan, 1):
        print(f"--- Step {i}: {step.name} ---")
        print(step.shell_command())
        print()
    if plan.output_path:
        print(f"Final output: {plan.output_path}")


def execute_plan(plan: RenderPlan) -> None:
    """Run each step with subprocess. Failures raise."""
    import subprocess
    for step in plan:
        print(f">>> {step.name}")
        result = subprocess.run(step.argv, check=True, cwd=step.cwd)
        print(f"    exit={result.returncode}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--execute", action="store_true",
        help="Actually run the FFmpeg commands (requires ffmpeg on PATH).",
    )
    parser.add_argument(
        "--fast", action="store_true",
        help="Smoke-test mode: short source video, skip ken-burns, ultrafast preset.",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="ffmpeg_renderer_test_") as tmp:
        project_root = Path(tmp)
        setup_demo_files(project_root, fast=args.fast)
        plan = build_plan(project_root, fast=args.fast)

        # Force ultrafast when executing so the smoke test finishes quickly.
        if args.execute:
            for step in plan:
                if "-preset" in step.argv:
                    step.argv[step.argv.index("-preset") + 1] = "ultrafast"

        print_plan(plan)

        if args.execute:
            print("\n=== Executing plan ===\n")
            execute_plan(plan)
            print("\n=== Done ===")
            out = plan.output_path
            if out and out.exists():
                import subprocess as _sp
                probe = _sp.run(
                    ["ffprobe", "-v", "error", "-show_format", "-show_streams", str(out)],
                    capture_output=True, text=True,
                )
                print(f"\nFinal output: {out} ({out.stat().st_size:,} bytes)")
                for line in probe.stdout.splitlines():
                    if any(k in line for k in ("duration=", "bit_rate=", "width=", "height=", "codec_name=", "nb_frames=")):
                        print("  ", line)
        else:
            print("\n(Dry run. Pass --execute to actually run FFmpeg; --fast for smoke mode.)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
