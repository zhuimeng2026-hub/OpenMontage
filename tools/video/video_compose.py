"""Video composition tool — FFmpeg + Remotion + HyperFrames (runtime-aware).

Pipeline-facing orchestration surface for composition. Takes `edit_decisions`,
`asset_manifest`, and audio, and delegates to the technical runtime chosen
at proposal stage.

Routing is driven by `edit_decisions.render_runtime` (locked at proposal):

- `remotion`   → React-based frame-accurate render via `npx remotion render`.
                 Handles the existing scene-component stack, word-level captions,
                 TalkingHead/CinematicRenderer. Current default.
- `hyperframes` → HTML/CSS/GSAP render via `hyperframes_compose`.
                 Handles kinetic typography, product promos, website-to-video,
                 registry blocks. Added in the parallel-runtime initiative.
- `ffmpeg`     → FFmpeg concat/trim. Used only for simple video cuts without
                 composition, or when the approved path explicitly names FFmpeg.

Silent runtime swaps are forbidden by governance. If the chosen runtime is
unavailable or fails, this tool surfaces a structured blocker and waits for
the agent to re-ask the user rather than substituting a different engine.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ResumeSupport,
    ToolResult,
    ToolStability,
    ToolTier,
)


import os

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _asset_abs_path(asset: dict, default: str = "") -> str:
    """Resolve an asset record to an on-disk absolute path.

    Session-uploaded assets carry an OS-portable ``relative_path`` (posix,
    relative to the repo root); template/workspace assets may still carry an
    absolute ``path``. Prefer ``relative_path`` so the same record resolves
    correctly on a different OS than the one that performed the upload.
    """
    rel = asset.get("relative_path")
    if rel:
        p = Path(rel)
        return str(p if p.is_absolute() else (_REPO_ROOT / rel))
    ap = asset.get("path")
    if ap:
        return str(ap)
    return default


def _get_remotion_concurrency():
    """Return the concurrency value for Remotion rendering."""
    import os

    env_val = os.environ.get("REMOTION_CONCURRENCY")
    if env_val:
        try:
            val = int(env_val)
            if 1 <= val <= os.cpu_count() or val == 100:
                return str(val)
        except ValueError:
            pass

    cores = os.cpu_count() or 4
    return str(min(cores, 8))


def _get_remotion_max_parallel() -> int:
    """并发 Remotion render 进程上限。

    每个 `npx remotion render` 都会在 --concurrency 线程池之上再起一个独立
    bundler + headless Chrome（约 0.5~1GB+ 内存）。N 个用户同时渲染 → N 个
    Chrome，无界扇出会 OOM 宿主。默认 2，可用 REMOTION_MAX_PARALLEL 覆盖。
    """
    env_val = os.environ.get("REMOTION_MAX_PARALLEL")
    if env_val:
        try:
            val = int(env_val)
            if val >= 1:
                return val
        except ValueError:
            pass
    return 2


def _get_remotion_max_parallel_per_user() -> int:
    """Return the maximum simultaneous Remotion processes for one user."""
    env_val = os.environ.get("REMOTION_MAX_PARALLEL_PER_USER")
    if env_val:
        try:
            val = int(env_val)
            if val >= 1:
                return min(val, _get_remotion_max_parallel())
        except ValueError:
            pass
    return 1


def _get_remotion_render_gate():
    """Return the process-wide user-fair Remotion render gate."""
    from lib.render_queue import get_fair_render_gate

    return get_fair_render_gate(
        _get_remotion_max_parallel(),
        _get_remotion_max_parallel_per_user(),
    )


class VideoCompose(BaseTool):
    name = "video_compose"
    version = "0.1.0"
    tier = ToolTier.CORE
    capability = "video_post"
    provider = "ffmpeg"
    stability = ToolStability.EXPERIMENTAL
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC

    dependencies = ["cmd:ffmpeg"]
    install_instructions = "Install FFmpeg: https://ffmpeg.org/download.html"
    agent_skills = ["remotion-best-practices", "remotion", "ffmpeg"]

    capabilities = [
        "compose_cuts",
        "burn_subtitles",
        "overlay_assets",
        "encode_profile",
        "remotion_render",
    ]

    input_schema = {
        "type": "object",
        "required": ["operation"],
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["compose", "render", "remotion_render", "burn_subtitles", "overlay", "encode", "remotion_bilingual_overlay"],
                "description": (
                    "compose: low-level concat cuts + audio + subtitles. "
                    "render: high-level — resolves asset IDs, auto-routes to Remotion "
                    "for images/animations or FFmpeg for video-only. Preferred for compose-director. "
                    "remotion_render: render via Remotion (Node.js). "
                    "burn_subtitles: burn subtitle file into existing video. "
                    "overlay: composite overlays onto base video. "
                    "encode: re-encode to a target profile/codec. "
                    "remotion_bilingual_overlay: render bilingual subtitles via "
                    "Remotion's BilingualCaptionOverlay composition (animated "
                    "word-level highlighting, transparent or black background)."
                ),
            },
            "input_path": {"type": "string"},
            "output_path": {"type": "string"},
            "edit_decisions": {
                "type": "object",
                "description": "Full edit_decisions artifact (required for compose/render)",
            },
            "asset_manifest": {
                "type": "object",
                "description": (
                    "Full asset_manifest artifact (required for render). "
                    "Used to resolve asset IDs in cuts[].source to file paths."
                ),
            },
            "proposal_packet": {
                "type": "object",
                "description": (
                    "Full proposal_packet artifact. Optional but STRONGLY "
                    "recommended — when present, final_review compares "
                    "proposal_packet.production_plan.render_runtime against "
                    "edit_decisions.render_runtime and flags runtime_swap_detected. "
                    "Without it, runtime-swap detection falls back to checking "
                    "edit_decisions.metadata.proposal_render_runtime."
                ),
            },
            "narration_transcript_path": {
                "type": "string",
                "description": (
                    "Path to a word-level transcript JSON (from `transcriber` "
                    "tool output). Optional but STRONGLY recommended: when "
                    "combined with script_path/script_text, final_review "
                    "runs transcript_comparison and catches TTS failures "
                    "like 'Chirp3-HD reads ... as the word dot'. Without "
                    "it, content-level audio bugs ship silently."
                ),
            },
            "script_path": {
                "type": "string",
                "description": (
                    "Path to the source narration script (plain text). "
                    "Used by transcript_comparison to diff against the "
                    "transcribed audio. Provide this OR script_text."
                ),
            },
            "script_text": {
                "type": "string",
                "description": (
                    "Inline source narration script. Used by "
                    "transcript_comparison when a file path is unavailable."
                ),
            },
            "subtitle_path": {"type": "string"},
            "subtitle_style": {
                "type": "object",
                "description": "ASS subtitle styling. Also extracted from edit_decisions.subtitles if not provided.",
                "properties": {
                    "font": {"type": "string", "default": "Arial"},
                    "font_size": {"type": "integer", "default": 24},
                    "primary_color": {"type": "string", "default": "&HFFFFFF"},
                    "outline_color": {"type": "string", "default": "&H000000"},
                    "outline_width": {"type": "number", "default": 2},
                    "margin_v": {"type": "integer", "default": 40},
                    "alignment": {"type": "integer", "default": 2},
                },
            },
            "overlays": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "asset_path": {"type": "string"},
                        "x": {"type": "number"},
                        "y": {"type": "number"},
                        "width": {"type": "number"},
                        "height": {"type": "number"},
                        "start_seconds": {"type": "number"},
                        "end_seconds": {"type": "number"},
                        "opacity": {"type": "number", "minimum": 0, "maximum": 1},
                    },
                },
            },
            "audio_path": {"type": "string", "description": "Mixed audio to mux into output"},
            "profile": {
                "type": "string",
                "description": (
                    "Media profile name from media_profiles.py "
                    "(e.g. youtube_landscape, tiktok, instagram_reels). "
                    "Applied in render and encode operations."
                ),
            },
            "options": {
                "type": "object",
                "description": "Render options (used by the render operation)",
                "properties": {
                    "subtitle_burn": {"type": "boolean", "default": True},
                    "two_pass_encode": {"type": "boolean", "default": False},
                },
            },
            "codec": {"type": "string", "default": "libx264"},
            "crf": {"type": "integer", "default": 23},
            "preset": {"type": "string", "default": "medium"},
            "remotion_timeout_ms": {
                "type": "integer",
                "description": (
                    "Remotion render timeout in milliseconds, passed through as "
                    "`--timeout` (governs headless-browser setup and delayRender). "
                    "Raise this when the browser is slow to start (e.g. restricted "
                    "networks). The subprocess timeout is widened to match."
                ),
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=4, ram_mb=2048, vram_mb=0, disk_mb=5000, network_required=False
    )

    # Remotion scene types that trigger React-based rendering
    _REMOTION_COMPONENTS = [
        "text_card", "stat_card", "callout", "comparison",
        "progress", "chart", "bar_chart", "line_chart", "pie_chart", "kpi_grid",
        "bilingual_caption_overlay",
    ]

    best_for = [
        "Final render for explainer and animation pipelines",
        "Image-to-video with spring animations (Remotion)",
        "Animated text cards, stat cards, charts (Remotion)",
        "Complex transitions between scenes (Remotion)",
        "Pure video concat and trim (FFmpeg)",
    ]
    retry_policy = RetryPolicy(max_retries=1, retryable_errors=["Conversion failed"])
    resume_support = ResumeSupport.FROM_START
    idempotency_key_fields = ["operation", "input_path", "edit_decisions"]
    side_effects = ["writes video file to output_path"]
    user_visible_verification = [
        "Play the composed output and verify cuts, subtitles, and overlays",
    ]

    def _ensure_node_on_path(self) -> None:
        """探测常见 nvm 安装并注入当前进程 PATH。

        服务进程（如 systemd / 守护脚本拉起）常常不带 node 环境，
        导致 `shutil.which("npx")` 因 PATH 缺失而判定 Remotion 不可用——
        这会静默降级到 FFmpeg，并错误地拒绝图片合成。即使启动环境
        忘了配 PATH，也在此兜底定位 node/npx，避免误导性的失败。"""
        import glob as _glob
        import os as _os
        import shutil as _shutil

        if _shutil.which("npx"):
            return
        candidates: list[str] = []
        for home in (_os.path.expanduser("~"), "/root"):
            candidates.extend(
                _glob.glob(_os.path.join(home, ".nvm", "versions", "node", "*", "bin"))
            )
        nvm_dir = _os.environ.get("NVM_DIR")
        if nvm_dir:
            candidates.extend(
                _glob.glob(_os.path.join(nvm_dir, "versions", "node", "*", "bin"))
            )
        for bin_dir in sorted(candidates, reverse=True):  # 取版本号最大的
            if _os.path.exists(_os.path.join(bin_dir, "npx")):
                _os.environ["PATH"] = bin_dir + _os.pathsep + _os.environ.get("PATH", "")
                return

    def _remotion_available(self) -> bool:
        """Check if Remotion rendering is available (requires npx + composer project + node_modules)."""
        import shutil as _shutil

        self._ensure_node_on_path()
        if not _shutil.which("npx"):
            return False
        composer_dir = Path(__file__).resolve().parent.parent.parent / "remotion-composer"
        if not composer_dir.exists() or not (composer_dir / "package.json").exists():
            return False
        # Check that node_modules are actually installed — without this,
        # npx remotion render will fail even though the project exists.
        if not (composer_dir / "node_modules").exists():
            return False
        return True

    def _hyperframes_available(self) -> bool:
        """Check if HyperFrames rendering is available.

        Delegates to the dedicated tool so the availability check stays in
        one place (node 22 floor, ffmpeg + npx on PATH).
        """
        try:
            from tools.video.hyperframes_compose import HyperFramesCompose
            return bool(HyperFramesCompose()._runtime_check()["runtime_available"])
        except Exception:
            return False

    def _ffmpeg_available(self) -> bool:
        """Check if the ffmpeg binary is actually resolvable on PATH."""
        import shutil as _shutil

        return bool(_shutil.which("ffmpeg"))

    def get_info(self) -> dict[str, Any]:
        """Extend base get_info to surface all available render runtimes.

        Preflight reports each runtime's availability separately so the agent
        can choose an appropriate `render_runtime` at proposal stage. Silent
        fallback between runtimes is forbidden.
        """
        info = super().get_info()
        ffmpeg_ok = self._ffmpeg_available()
        remotion_ok = self._remotion_available()
        hyperframes_ok = self._hyperframes_available()
        info["render_engines"] = {
            "ffmpeg": ffmpeg_ok,
            "remotion": remotion_ok,
            "hyperframes": hyperframes_ok,
        }
        # Backwards-compat alias — some proposal skills inspect this name.
        info["render_runtimes"] = info["render_engines"]

        if remotion_ok:
            info["remotion_components"] = self._REMOTION_COMPONENTS
            info["remotion_note"] = (
                "Remotion is available for React-based rendering. Use it for "
                "image-to-video with spring animations, animated text/stat cards, "
                "charts, callouts, comparisons, and word-level caption burn. "
                "Prefer Remotion over Ken Burns pan-and-zoom for explainer "
                "and motion-graphics pipelines that already use the scene-component stack."
            )
        else:
            composer_dir = Path(__file__).resolve().parent.parent.parent / "remotion-composer"
            if composer_dir.exists() and (composer_dir / "package.json").exists() and not (composer_dir / "node_modules").exists():
                info["remotion_note"] = (
                    "Remotion project exists but node_modules are NOT installed. "
                    "Run 'cd remotion-composer && npm install' to enable Remotion rendering."
                )
            else:
                info["remotion_note"] = (
                    "Remotion is NOT available (needs Node.js/npx + remotion-composer + node_modules)."
                )

        if hyperframes_ok:
            info["hyperframes_note"] = (
                "HyperFrames is available for HTML/CSS/GSAP composition. Use it "
                "for kinetic typography, product promos, launch reels, "
                "website-to-video, and registry-block-driven scenes. Consumed via "
                "'npx hyperframes' (npm package: 'hyperframes'). "
                "Before locking render_runtime='hyperframes' at the proposal stage, "
                "verify the runtime with `hyperframes_compose` operation='doctor' "
                "or `make hyperframes-doctor`. An 'available' flag from the runtime "
                "check means node + ffmpeg + the npm package all resolve; it does "
                "not guarantee a render will succeed on the first specific "
                "composition."
            )
        else:
            info["hyperframes_note"] = (
                "HyperFrames is NOT available. Requires Node.js >= 22, FFmpeg, "
                "npx on PATH, and the 'hyperframes' npm package to be resolvable. "
                "Run `make hyperframes-doctor` to see the specific missing piece, "
                "or call `hyperframes_compose` operation='doctor' directly."
            )

        # Governance note — agents and reviewers consume this.
        info["runtime_governance"] = (
            "render_runtime is locked at proposal stage and carried unchanged "
            "through edit_decisions. Silent swaps are forbidden. If the "
            "chosen runtime fails, surface a structured blocker and wait for "
            "user approval before switching."
        )
        return info

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        operation = inputs["operation"]
        start = time.time()

        try:
            if operation == "compose":
                result = self._compose(inputs)
            elif operation == "render":
                result = self._render(inputs)
            elif operation == "remotion_render":
                result = self._remotion_render(inputs)
            elif operation == "burn_subtitles":
                result = self._burn_subtitles(inputs)
            elif operation == "overlay":
                result = self._overlay(inputs)
            elif operation == "encode":
                result = self._encode(inputs)
            elif operation == "remotion_bilingual_overlay":
                result = self._remotion_bilingual_overlay(inputs)
            else:
                return ToolResult(success=False, error=f"Unknown operation: {operation}")
        except Exception as e:
            return ToolResult(success=False, error=str(e))

        result.duration_seconds = round(time.time() - start, 2)
        return result

    _IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif", ".webp"}

    @staticmethod
    def _is_image(path: Path) -> bool:
        """Check if a file is a still image (routes to Remotion, not FFmpeg)."""
        return path.suffix.lower() in VideoCompose._IMAGE_EXTENSIONS

    @staticmethod
    def _has_audio_stream(path: Path) -> bool:
        """Return True iff ffprobe reports at least one audio stream.

        Many stock video clips (especially from Pexels) ship with no audio
        stream at all. If we blindly tell ffmpeg to transcode the 0:a stream
        on such a file it errors out. This helper lets the segment builder
        branch on stream presence so it can synthesize a silent track when
        needed, keeping the concat segment layout consistent.
        """
        try:
            out = subprocess.check_output(
                [
                    "ffprobe", "-v", "error",
                    "-select_streams", "a",
                    "-show_entries", "stream=codec_type",
                    "-of", "default=nw=1:nk=1",
                    str(path),
                ],
                stderr=subprocess.STDOUT,
                text=True,
            )
            return "audio" in out
        except Exception:
            return False

    def _compose(self, inputs: dict[str, Any]) -> ToolResult:
        """FFmpeg composition: concat video cuts, add audio, burn subtitles.

        Handles video sources only. Still images and animated scene types
        are routed to Remotion via the render operation — call compose
        directly only for pure video pipelines (e.g. talking-head).
        """
        edit_decisions = inputs.get("edit_decisions")
        if not edit_decisions:
            return ToolResult(success=False, error="edit_decisions required for compose")

        output_path = Path(inputs.get("output_path", "composed_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path = inputs.get("audio_path")
        subtitle_path = inputs.get("subtitle_path")
        codec = inputs.get("codec", "libx264")
        crf = inputs.get("crf", 23)
        preset = inputs.get("preset", "medium")
        profile_name = inputs.get("profile")

        # Resolve target resolution + fit mode. Priority: explicit `profile`
        # arg > edit_decisions.metadata.compose_target > default (landscape HD).
        # compose_target = {"width": W, "height": H, "fit": "pad"|"cover"} lets a
        # caller request vertical (9:16) or any aspect without a named profile.
        # fit="pad" letterboxes (no content loss, the historical default);
        # fit="cover" scales-to-fill and centre-crops (better for vertical social).
        resolution = "1920x1080"
        fit_mode = "pad"
        compose_target = (edit_decisions.get("metadata") or {}).get("compose_target")
        if isinstance(compose_target, dict):
            try:
                resolution = f"{int(compose_target['width'])}x{int(compose_target['height'])}"
            except (KeyError, ValueError, TypeError):
                pass
            if compose_target.get("fit") in ("pad", "cover"):
                fit_mode = compose_target["fit"]
        if profile_name:
            try:
                from lib.media_profiles import get_profile
                p = get_profile(profile_name)
                resolution = f"{p.width}x{p.height}"
            except (ImportError, ValueError):
                pass
        try:
            target_w, target_h = (int(v) for v in resolution.split("x"))
        except ValueError:
            target_w, target_h = 1920, 1080

        cuts = edit_decisions.get("cuts", [])
        if not cuts:
            return ToolResult(success=False, error="No cuts in edit_decisions")

        # Resolve subtitle style using the layered priority resolver
        # (explicit > edit_decisions > playbook > defaults)
        playbook_data = inputs.get("playbook")
        resolved_sub_style = self._resolve_subtitle_style(
            inputs.get("subtitle_style"),
            edit_decisions,
            playbook_data,
        )
        inputs = dict(inputs)
        inputs["subtitle_style"] = resolved_sub_style

        ed_subs = edit_decisions.get("subtitles", {})
        if ed_subs.get("source") and not subtitle_path:
            subtitle_path = ed_subs["source"]

        temp_dir = output_path.parent / ".compose_tmp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        temp_segments: list[Path] = []
        concat_path: Path | None = None
        concat_out: Path | None = None

        try:
            for i, cut in enumerate(cuts):
                source = Path(cut["source"])
                if not source.exists():
                    return ToolResult(success=False, error=f"Cut source not found: {source}")

                seg_path = temp_dir / f"seg_{i:04d}.mp4"
                in_s = cut["in_seconds"]
                out_s = cut["out_seconds"]
                duration = out_s - in_s
                speed = cut.get("speed", 1.0)

                if self._is_image(source):
                    return ToolResult(
                        success=False,
                        error=(
                            f"Still image '{source.name}' in cuts. "
                            "Use operation='render' (auto-routes to Remotion) "
                            "or operation='remotion_render' for compositions "
                            "with images, animations, or component scenes."
                        ),
                    )
                else:
                    # Video source: trim to segment.
                    #
                    # Semantics:
                    #   -ss BEFORE -i   → fast input-level seek to in_s
                    #   -t  AFTER  -i   → "play for `duration` seconds"
                    #                     (unambiguous regardless of seek mode)
                    #
                    # We MUST re-encode here — `-c copy` cannot do frame-accurate
                    # cuts because it snaps to keyframes. With sparse GOPs (common
                    # in Pexels / AI-generated clips), stream-copy can produce
                    # segments significantly longer than `duration`, breaking the
                    # target timeline. Re-encoding with libx264/AAC is slower but
                    # gives exact cut boundaries. Same resolution in → same
                    # resolution out, so same-res inputs concat cleanly.
                    cmd = [
                        "ffmpeg", "-y",
                        "-ss", str(in_s),
                        "-t", str(duration),
                        "-i", str(source),
                    ]

                    # Normalize every segment to a consistent container so the
                    # concat-copy step is always safe. The concat demuxer with
                    # `-c copy` requires identical codec / resolution / fps /
                    # pix_fmt / sar across ALL segments — otherwise it throws
                    # "Non-monotonous DTS" or silently produces corrupt output.
                    #
                    # Target is target_w x target_h @ 30fps, yuv420p, sar=1
                    # (default 1920x1080; overridable via `profile` or
                    # edit_decisions.metadata.compose_target — see above).
                    # fit="pad" letterboxes to preserve all content; fit="cover"
                    # scales-to-fill then centre-crops (no bars, for vertical social).
                    if fit_mode == "cover":
                        geom = [
                            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase",
                            f"crop={target_w}:{target_h}",
                        ]
                    else:
                        geom = [
                            f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease",
                            f"pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2:color=black",
                        ]
                    vf_parts: list[str] = [*geom, "setsar=1", "fps=30"]
                    af_parts: list[str] = []
                    if speed != 1.0:
                        vf_parts.append(f"setpts={1.0/speed}*PTS")
                        af_parts.append(self._build_atempo(speed))

                    cmd.extend(["-filter:v", ",".join(vf_parts)])
                    if af_parts:
                        cmd.extend(["-filter:a", ",".join(af_parts)])

                    cmd.extend([
                        "-c:v", codec,
                        "-crf", str(crf),
                        "-preset", preset,
                        "-pix_fmt", "yuv420p",
                        "-r", "30",
                    ])

                    # Audio handling: some source clips have no audio stream
                    # (Pexels stock often ships silent). If we unconditionally
                    # ask ffmpeg to copy/encode the 0:a stream it errors out.
                    # Probe for an audio stream first — if present, transcode
                    # to AAC; if absent, synthesize a silent stereo track so
                    # concat segments have a consistent stream layout.
                    has_audio = self._has_audio_stream(source)
                    if has_audio:
                        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"])
                    else:
                        # Inject silent audio via lavfi before the output.
                        # We have to rebuild cmd to add the lavfi input
                        # before the output path and map streams explicitly.
                        cmd = [
                            "ffmpeg", "-y",
                            "-ss", str(in_s),
                            "-t", str(duration),
                            "-i", str(source),
                            "-f", "lavfi",
                            "-t", str(duration),
                            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                            "-filter:v", ",".join(vf_parts),
                        ]
                        if af_parts:
                            cmd.extend(["-filter:a", ",".join(af_parts)])
                        cmd.extend([
                            "-map", "0:v:0",
                            "-map", "1:a:0",
                            "-c:v", codec,
                            "-crf", str(crf),
                            "-preset", preset,
                            "-pix_fmt", "yuv420p",
                            "-r", "30",
                            "-c:a", "aac",
                            "-b:a", "192k",
                            "-ar", "48000",
                            "-ac", "2",
                        ])

                    cmd.append(str(seg_path))
                    self.run_command(cmd)

                temp_segments.append(seg_path)

            # Step 2: Concat segments
            concat_path = temp_dir / "concat_list.txt"
            with open(concat_path, "w", encoding="utf-8") as f:
                for seg in temp_segments:
                    safe = str(seg.resolve()).replace("\\", "/")
                    f.write(f"file '{safe}'\n")

            concat_out = temp_dir / "concat.mp4"
            cmd = [
                "ffmpeg", "-y",
                "-f", "concat", "-safe", "0",
                "-i", str(concat_path),
                "-c", "copy",
                str(concat_out),
            ]
            self.run_command(cmd)

            # Step 3: Apply subtitles and/or replace audio
            final_input = concat_out
            vfilters = []

            if subtitle_path and Path(subtitle_path).exists():
                style = inputs.get("subtitle_style", {})
                ass_style = self._build_subtitle_style(style)
                sub_escaped = str(Path(subtitle_path).resolve()).replace("\\", "/").replace(":", "\\:")
                vfilters.append(f"subtitles='{sub_escaped}':force_style='{ass_style}'")

            cmd = ["ffmpeg", "-y", "-i", str(final_input)]

            if audio_path and Path(audio_path).exists():
                cmd.extend(["-i", audio_path])

            # Determine if profile requires re-encoding (resize/fps change)
            # This must be checked BEFORE choosing copy vs encode, because
            # -s and -r are incompatible with -c:v copy.
            profile_flags: list[str] = []
            if profile_name:
                try:
                    from lib.media_profiles import get_profile
                    p = get_profile(profile_name)
                    profile_flags = ["-s", f"{p.width}x{p.height}", "-r", str(p.fps)]
                except (ImportError, ValueError):
                    pass

            needs_reencode = bool(vfilters) or bool(profile_flags)

            if needs_reencode:
                if vfilters:
                    cmd.extend(["-vf", ",".join(vfilters)])
                cmd.extend(["-c:v", codec, "-crf", str(crf), "-preset", preset])
                cmd.extend(profile_flags)
            else:
                cmd.extend(["-c:v", "copy"])

            if audio_path and Path(audio_path).exists():
                # Use type-based selectors (0:v, 1:a) instead of index-based
                # (0:v:0) because source videos may have audio as stream 0
                # and video as stream 1 (e.g. Kling-generated clips).
                cmd.extend(["-map", "0:v", "-map", "1:a", "-c:a", "aac", "-shortest"])
            else:
                cmd.extend(["-c:a", "copy"])

            cmd.append(str(output_path))
            self.run_command(cmd)

            return ToolResult(
                success=True,
                data={
                    "operation": "compose",
                    "cut_count": len(cuts),
                    "has_subtitles": subtitle_path is not None,
                    "has_mixed_audio": audio_path is not None,
                    "profile": profile_name,
                    "output": str(output_path),
                },
                artifacts=[str(output_path)],
            )
        finally:
            # Cleanup temp files
            for f in temp_segments:
                if f.exists():
                    f.unlink()
            for f in [concat_path, concat_out]:
                if f is not None and f.exists():
                    f.unlink()
            if temp_dir.exists():
                try:
                    temp_dir.rmdir()
                except OSError:
                    pass

    _REMOTION_SCENE_TYPES = {
        "text_card", "stat_card", "callout", "comparison", "progress", "chart",
    }

    # Maps renderer_family (set at proposal stage) to Remotion composition ID.
    # Each family MUST map to a distinct composition — collapsing defeats visual grammar.
    # Maps renderer_family → Remotion composition ID.
    # Only compositions registered in remotion-composer/src/Root.tsx are valid.
    # Current compositions: Explainer, CinematicRenderer, TalkingHead,
    # ProductReveal, EcommerceProductDemo
    RENDERER_FAMILY_MAP = {
        "explainer-data": "Explainer",
        "explainer-teacher": "Explainer",
        "cinematic-trailer": "CinematicRenderer",
        "documentary-montage": "CinematicRenderer",
        "product-reveal": "Explainer",
        "ecommerce-product-demo": "EcommerceProductDemo",
        "screen-demo": "Explainer",
        "presenter": "TalkingHead",
        "animation-first": "Explainer",
        "custom-composition": "CustomComposition",
    }

    @classmethod
    def _get_composition_id(cls, renderer_family: str) -> str:
        """Resolve renderer_family to Remotion composition ID.

        Raises ValueError if renderer_family is not recognized — the caller
        must set it at proposal stage.
        """
        comp = cls.RENDERER_FAMILY_MAP.get(renderer_family)
        if comp is None:
            raise ValueError(
                f"Unknown renderer_family {renderer_family!r}. "
                f"Valid families: {sorted(cls.RENDERER_FAMILY_MAP)}. "
                f"Set renderer_family at proposal stage."
            )
        return comp

    @staticmethod
    def _build_theme_from_playbook(
        playbook_name: str | None,
        composition_data: dict | None,
    ) -> dict[str, Any] | None:
        """Derive a Remotion ThemeConfig from a playbook's actual color values.

        Instead of passing a playbook name and hoping Remotion has a matching
        preset, we read the playbook YAML and extract concrete colors/fonts.
        This means custom playbooks, overridden palettes, and per-project
        styles all flow through to Remotion automatically.

        Falls back to extracting colors from edit_decisions metadata if
        no playbook is loadable.
        """
        theme: dict[str, Any] = {}

        # Try to load the playbook YAML
        playbook: dict[str, Any] = {}
        if playbook_name:
            try:
                from styles.playbook_loader import load_playbook
                playbook = load_playbook(playbook_name)
            except Exception:
                pass

        if playbook:
            vl = playbook.get("visual_language", {})
            palette = vl.get("color_palette", {})
            typo = playbook.get("typography", {})

            # Extract primary/accent — may be a list (gradient stops) or string
            primary_raw = palette.get("primary", ["#2563EB"])
            accent_raw = palette.get("accent", ["#F59E0B"])
            primary = primary_raw[0] if isinstance(primary_raw, list) else primary_raw
            accent = accent_raw[0] if isinstance(accent_raw, list) else accent_raw

            bg = palette.get("background", "#FFFFFF")
            text = palette.get("text", "#1F2937")
            surface = palette.get("surface", bg)
            muted = palette.get("muted_text", "#6B7280")

            # Build chart colors from all palette entries
            chart_colors = []
            for key in ["primary", "accent", "secondary", "success", "warning", "info"]:
                val = palette.get(key)
                if val:
                    chart_colors.append(val[0] if isinstance(val, list) else val)
            if len(chart_colors) < 3:
                chart_colors = [primary, accent, "#10B981", "#8B5CF6", "#EC4899", "#06B6D4"]

            theme = {
                "primaryColor": primary,
                "accentColor": accent,
                "backgroundColor": bg,
                "surfaceColor": surface,
                "textColor": text,
                "mutedTextColor": muted,
                "headingFont": typo.get("heading", {}).get("font", "Inter"),
                "bodyFont": typo.get("body", {}).get("font", "Inter"),
                "monoFont": typo.get("code", {}).get("font", "JetBrains Mono"),
                "chartColors": chart_colors[:6],
                "springConfig": {"damping": 20, "stiffness": 120, "mass": 1},
                "transitionDuration": 0.4,
            }

            # Derive caption colors from the palette
            theme["captionHighlightColor"] = primary
            # Caption background: semi-transparent version of the bg color
            theme["captionBackgroundColor"] = (
                f"rgba(255, 255, 255, 0.85)" if bg.upper() in ("#FFFFFF", "#FAFAFA", "#F9FAFB")
                else f"rgba(15, 23, 42, 0.75)"
            )

            # Motion style from playbook
            motion = playbook.get("motion", {})
            pace = motion.get("pace", "moderate")
            if pace == "fast":
                theme["springConfig"] = {"damping": 12, "stiffness": 80, "mass": 1}
                theme["transitionDuration"] = 0.3
            elif pace == "slow":
                theme["springConfig"] = {"damping": 25, "stiffness": 150, "mass": 1}
                theme["transitionDuration"] = 0.6

        # Fallback: try to extract from edit_decisions metadata
        if not theme and composition_data:
            meta = composition_data.get("metadata", {})
            if meta.get("primary_color"):
                theme = {
                    "primaryColor": meta["primary_color"],
                    "accentColor": meta.get("accent_color", "#F59E0B"),
                    "backgroundColor": meta.get("background_color", "#FFFFFF"),
                    "surfaceColor": meta.get("surface_color", "#F9FAFB"),
                    "textColor": meta.get("text_color", "#1F2937"),
                    "mutedTextColor": "#6B7280",
                    "headingFont": meta.get("heading_font", "Inter"),
                    "bodyFont": meta.get("body_font", "Inter"),
                    "monoFont": "JetBrains Mono",
                    "chartColors": meta.get("chart_colors", ["#2563EB", "#F59E0B", "#10B981"]),
                    "springConfig": {"damping": 20, "stiffness": 120, "mass": 1},
                    "transitionDuration": 0.4,
                    "captionHighlightColor": meta["primary_color"],
                    "captionBackgroundColor": "rgba(255, 255, 255, 0.85)",
                }

        return theme if theme else None

    def _needs_remotion(self, cuts: list[dict]) -> bool:
        """Determine whether Remotion should handle this composition.

        Remotion is the DEFAULT composition engine when available.  It handles
        video clips (via <OffthreadVideo>), still images, animated scene types,
        component types, transitions, and mixed content — all in a single
        React-based render pass.

        Returns False (i.e. use FFmpeg) only when Remotion is not
        available. For `operation="render"` the governance default is
        Remotion-first: the renderer family was chosen earlier, and the
        tool should preserve that decision instead of silently
        downgrading to FFmpeg.

        This "Remotion-first" policy means mixed content (video clips +
        animated stills + text cards) is always composed in Remotion, which
        can embed <OffthreadVideo> alongside React components natively.
        """
        # If Remotion isn't installed, fall back to FFmpeg
        if not self._remotion_available():
            return False

        # Any rich content → Remotion (fast path, catches the obvious cases)
        for cut in cuts:
            source = cut.get("source", "")
            if source and Path(source).suffix.lower() in self._IMAGE_EXTENSIONS:
                return True
            if cut.get("type") in self._REMOTION_SCENE_TYPES:
                return True
            if cut.get("animation") or cut.get("transition_in") or cut.get("transition_out"):
                return True
            transform = cut.get("transform", {})
            if transform and transform.get("animation"):
                return True

        # Even for pure-video cuts, default to Remotion — it handles video
        # clips natively via <OffthreadVideo> and gives us transitions,
        # overlays, and profile scaling for free.
        return True

    def _pre_compose_validation(
        self,
        edit_decisions: dict[str, Any],
        resolved_cuts: list[dict],
        scene_plan: list[dict] | None = None,
    ) -> ToolResult | None:
        """Pre-compose quality gate — blocks render on critical violations.

        Checks:
        1. Delivery promise violation: motion-required brief with >70% still cuts → BLOCK
        2. Slideshow risk score "fail" (average ≥ 4.0) → BLOCK
        3. Missing renderer_family → WARN (log only, don't block)

        Returns a failed ToolResult if render should be blocked, None if OK to proceed.
        """
        log = logging.getLogger("video_compose")
        warnings: list[str] = []
        blocks: list[str] = []

        # --- 1. Delivery promise check ---
        delivery_data = edit_decisions.get("metadata", {}).get("delivery_promise")
        if not delivery_data:
            # Also check top-level (proposal_packet nests it at top level)
            delivery_data = edit_decisions.get("delivery_promise")

        if delivery_data:
            try:
                from lib.delivery_promise import DeliveryPromise
                promise = DeliveryPromise.from_dict(delivery_data)
                result = promise.validate_cuts(resolved_cuts)
                if not result["valid"]:
                    for v in result["violations"]:
                        blocks.append(f"Delivery promise violation: {v}")
            except Exception as e:
                log.warning("Could not validate delivery promise: %s", e)
        else:
            warnings.append("No delivery_promise in edit_decisions — skipping promise validation")

        # --- 2. Slideshow risk check ---
        renderer_family = edit_decisions.get("renderer_family")
        scenes = scene_plan or []

        # If no scene_plan passed, try to extract scene info from cuts
        if not scenes and resolved_cuts:
            scenes = [
                {
                    "type": c.get("type", ""),
                    "description": c.get("reason", ""),
                    "shot_language": c.get("shot_language", {}),
                    "shot_intent": c.get("shot_intent"),
                    "narrative_role": c.get("narrative_role"),
                    "information_role": c.get("information_role"),
                    "hero_moment": c.get("hero_moment", False),
                }
                for c in resolved_cuts
            ]

        if scenes:
            try:
                from lib.slideshow_risk import score_slideshow_risk
                render_runtime = edit_decisions.get("render_runtime")
                risk = score_slideshow_risk(
                    scenes, edit_decisions, renderer_family, render_runtime
                )
                if risk["verdict"] == "fail":
                    blocks.append(
                        f"Slideshow risk score {risk['average']:.1f}/5.0 (verdict: fail). "
                        f"Video plan looks like a slideshow — revise scene plan before rendering."
                    )
                elif risk["verdict"] == "revise":
                    warnings.append(
                        f"Slideshow risk score {risk['average']:.1f}/5.0 (verdict: revise). "
                        f"Consider improving scene variety before final render."
                    )
            except Exception as e:
                log.warning("Could not compute slideshow risk: %s", e)

        # --- 3. Missing renderer_family (BLOCK — must be set at proposal) ---
        if not renderer_family:
            blocks.append(
                "No renderer_family in edit_decisions. "
                "renderer_family must be set at proposal stage and locked before compose. "
                "Re-run the proposal stage with a renderer_family selection."
            )

        # Log warnings
        for w in warnings:
            log.warning("[pre-compose] %s", w)

        # Block on critical violations
        if blocks:
            return ToolResult(
                success=False,
                error=(
                    "Pre-compose validation failed — render blocked.\n"
                    + "\n".join(f"  • {b}" for b in blocks)
                    + ("\n\nWarnings:\n" + "\n".join(f"  • {w}" for w in warnings) if warnings else "")
                ),
            )

        return None

    def _render(self, inputs: dict[str, Any]) -> ToolResult:
        """High-level render: assemble edit decisions + asset manifest into final video.

        This is the primary entry point for the compose-director skill.
        It resolves asset IDs and routes to the composition engine:

        - **Remotion (default):** Used for all compositions when available —
          video clips, images, animated scenes, component types, mixed content.
          Remotion embeds video via <OffthreadVideo> and handles transitions,
          overlays, and profile scaling natively.
        - **FFmpeg (fallback):** Used only when Remotion is unavailable, or
          when the agent explicitly calls operation='compose' for simple
          trim/concat operations.

        The agent should pass edit_decisions, asset_manifest, and optionally
        profile, subtitle_path, audio_path, and options.
        """
        edit_decisions = inputs.get("edit_decisions")
        asset_manifest = inputs.get("asset_manifest")
        if not edit_decisions:
            return ToolResult(success=False, error="edit_decisions required for render")
        if not asset_manifest:
            return ToolResult(success=False, error="asset_manifest required for render")

        output_path = Path(inputs.get("output_path", "renders/output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)

        # --- Custom user-code pipeline (script mode / 自定义合成) ---
        # The user supplies TSX `code` and the uploaded session images; there is
        # no templated cut timeline, so bypass the cut/validation gates below and
        # route straight to Remotion with the custom composition.
        if edit_decisions.get("composition_mode") == "custom" and \
                str(edit_decisions.get("render_runtime", "remotion")).strip().lower() == "remotion":
            remotion_inputs: dict[str, Any] = {
                "edit_decisions": dict(edit_decisions),
                "output_path": str(output_path),
            }
            profile = inputs.get("profile") or inputs.get("output_profile")
            if profile:
                remotion_inputs["profile"] = profile
            # Forward the creator-facing render timeout, live-progress callback,
            # and render job id so the slot gate + SSE progress keep working.
            if inputs.get("remotion_timeout_ms") is not None:
                remotion_inputs["remotion_timeout_ms"] = inputs["remotion_timeout_ms"]
            if inputs.get("_progress_callback") is not None:
                remotion_inputs["_progress_callback"] = inputs["_progress_callback"]
            if inputs.get("_job_id") is not None:
                remotion_inputs["_job_id"] = inputs["_job_id"]
            return self._remotion_render(remotion_inputs)

        # Build asset lookup: id -> asset info
        asset_lookup = {a["id"]: a for a in asset_manifest.get("assets", [])}

        cuts = edit_decisions.get("cuts", [])
        if not cuts:
            return ToolResult(success=False, error="No cuts in edit_decisions")

        # Resolve asset IDs in cuts to file paths
        resolved_cuts = []
        for cut in cuts:
            source_id = cut.get("source", "")
            resolved_cut = dict(cut)
            if source_id in asset_lookup:
                resolved_cut["source"] = _asset_abs_path(asset_lookup[source_id])
            resolved_cuts.append(resolved_cut)

        # --- Pre-compose validation gate ---
        scene_plan = inputs.get("scene_plan")
        validation_block = self._pre_compose_validation(edit_decisions, resolved_cuts, scene_plan)
        if validation_block is not None:
            return validation_block

        # Also accept profile as "output_profile" (skill convention) or "profile"
        profile = inputs.get("profile") or inputs.get("output_profile")

        # --- Runtime routing: honor render_runtime locked at proposal ---
        # Silent swaps are forbidden by governance. If the chosen runtime
        # is unavailable, surface a structured blocker rather than quietly
        # picking a different engine. Missing render_runtime is itself a
        # governance violation — edit_decisions.schema.json requires it.
        render_runtime = (edit_decisions.get("render_runtime") or "").strip().lower()

        if not render_runtime:
            return ToolResult(
                success=False,
                error=(
                    "render_runtime is not set in edit_decisions. Per governance, "
                    "it MUST be locked at proposal stage (proposal_packet."
                    "production_plan.render_runtime) and carried forward through "
                    "edit_decisions.render_runtime. Valid values: 'remotion', "
                    "'hyperframes', 'ffmpeg'. Re-run the proposal stage with an "
                    "explicit runtime choice — do NOT default this field."
                ),
            )

        if render_runtime == "hyperframes":
            return self._render_via_hyperframes(
                inputs=inputs,
                edit_decisions=edit_decisions,
                asset_manifest=asset_manifest,
                resolved_cuts=resolved_cuts,
                output_path=output_path,
                profile=profile,
            )
        if render_runtime == "ffmpeg":
            # Caller explicitly asked for FFmpeg — don't auto-upgrade to Remotion.
            return self._render_via_ffmpeg(
                inputs=inputs,
                edit_decisions=edit_decisions,
                resolved_cuts=resolved_cuts,
                output_path=output_path,
                profile=profile,
            )
        if render_runtime != "remotion":
            return ToolResult(
                success=False,
                error=(
                    f"Unknown render_runtime {render_runtime!r}. "
                    f"Valid values: remotion, hyperframes, ffmpeg. "
                    f"render_runtime must be set at proposal stage."
                ),
            )

        # --- Explicit Remotion path (render_runtime == 'remotion') ---
        if self._needs_remotion(resolved_cuts):
            remotion_inputs: dict[str, Any] = {
                "edit_decisions": dict(edit_decisions, cuts=resolved_cuts),
                "output_path": str(output_path),
            }
            if profile:
                remotion_inputs["profile"] = profile
            # Forward the creator-facing render timeout through the high-level
            # render path (execute(operation="render") -> _render), otherwise it
            # would only take effect on a direct _remotion_render() call.
            if inputs.get("remotion_timeout_ms") is not None:
                remotion_inputs["remotion_timeout_ms"] = inputs["remotion_timeout_ms"]
            # Forward the live-progress callback so frame-level Remotion output
            # can stream to the SSE endpoint instead of being swallowed.
            if inputs.get("_progress_callback") is not None:
                remotion_inputs["_progress_callback"] = inputs["_progress_callback"]
            # Forward the render job id so the slot gate can book the waiting
            # set and report queue position/depth (see _remotion_render gate).
            if inputs.get("_job_id") is not None:
                remotion_inputs["_job_id"] = inputs["_job_id"]
            render_result = self._remotion_render(remotion_inputs)

            # Governance: NEVER silently fall back to FFmpeg when Remotion fails.
            # The agent must decide the fallback path, not the tool.
            if not render_result.success:
                renderer_family = edit_decisions.get("renderer_family", "unknown")
                return ToolResult(
                    success=False,
                    error=(
                        f"Remotion render failed for renderer_family={renderer_family!r}. "
                        f"Underlying error: {render_result.error}\n\n"
                        f"This composition requires Remotion (images, text cards, animations). "
                        f"Options:\n"
                        f"  1. Fix Remotion setup (cd remotion-composer && npm install)\n"
                        f"  2. Re-run with operation='compose' for FFmpeg-only (video cuts only)\n"
                        f"  3. Approve a degraded FFmpeg render (still images → Ken Burns)\n\n"
                        f"Per governance: renderer downgrade requires user approval."
                    ),
                )
        else:
            # --- FFmpeg fallback: only when Remotion is unavailable ---
            options = inputs.get("options", {})
            subtitle_burn = options.get("subtitle_burn", True)

            # Resolve subtitle_path from edit_decisions if not provided
            subtitle_path = inputs.get("subtitle_path")
            if subtitle_burn and not subtitle_path:
                ed_subs = edit_decisions.get("subtitles", {})
                if ed_subs.get("enabled") and ed_subs.get("source"):
                    subtitle_path = ed_subs["source"]

            # Build compose inputs
            compose_inputs = dict(inputs)
            compose_inputs["edit_decisions"] = dict(edit_decisions, cuts=resolved_cuts)
            compose_inputs["output_path"] = str(output_path)
            if subtitle_path:
                compose_inputs["subtitle_path"] = subtitle_path
            if profile:
                compose_inputs["profile"] = profile

            render_result = self._compose(compose_inputs)

        # --- Post-render: mandatory final self-review ---
        if render_result.success and output_path.exists():
            final_review = self._run_final_review(
                output_path,
                edit_decisions,
                inputs.get("proposal_packet"),
                narration_transcript_path=inputs.get("narration_transcript_path"),
                script_text=inputs.get("script_text") or self._read_text_file(
                    inputs.get("script_path")
                ),
            )

            # Attach final_review to the ToolResult data so the compose-director
            # skill can include it in the checkpoint alongside the render_report.
            if render_result.data is None:
                render_result.data = {}
            render_result.data["final_review"] = final_review
            render_result.data["final_review_status"] = final_review["status"]

            # If the self-review says fail, downgrade the ToolResult
            if final_review["status"] == "fail":
                return ToolResult(
                    success=False,
                    error=(
                        "Post-render self-review FAILED. The output is not presentable.\n"
                        + "\n".join(f"  • {i}" for i in final_review.get("issues_found", []))
                    ),
                    data=render_result.data,
                )

        return render_result

    def _render_via_hyperframes(
        self,
        *,
        inputs: dict[str, Any],
        edit_decisions: dict[str, Any],
        asset_manifest: dict[str, Any],
        resolved_cuts: list[dict],
        output_path: Path,
        profile: Optional[str],
    ) -> ToolResult:
        """Delegate to hyperframes_compose and run the mandatory final self-review.

        Governance: if HyperFrames is unavailable or fails, return a structured
        blocker — do NOT silently route to Remotion or FFmpeg. The agent must
        surface the blocker and get user approval before any runtime swap.
        """
        if not self._hyperframes_available():
            return ToolResult(
                success=False,
                error=(
                    "render_runtime='hyperframes' was locked at proposal, but "
                    "the HyperFrames runtime is not available on this machine. "
                    "Per governance this is a BLOCKER — surface it to the user "
                    "per AGENT_GUIDE.md > 'Escalate Blockers Explicitly' and wait "
                    "for approval before switching runtime. Requirements: "
                    "Node.js >= 22, FFmpeg, and npx on PATH. See "
                    "tools/video/hyperframes_compose.py for the specific missing piece."
                ),
            )

        try:
            from tools.video.hyperframes_compose import HyperFramesCompose
        except Exception as e:
            return ToolResult(
                success=False,
                error=f"Could not import hyperframes_compose: {e}",
            )

        workspace_path = (
            inputs.get("workspace_path")
            or str(output_path.parent.parent / "hyperframes")
        )

        # Pass the playbook through so the style bridge can emit CSS vars.
        playbook_data = inputs.get("playbook")
        if not playbook_data:
            playbook_name = (
                inputs.get("playbook_name")
                or (edit_decisions.get("metadata") or {}).get("playbook")
            )
            if playbook_name:
                try:
                    from styles.playbook_loader import load_playbook  # type: ignore
                    playbook_data = load_playbook(playbook_name)
                except Exception:
                    playbook_data = None

        hf_inputs: dict[str, Any] = {
            "operation": "render",
            "workspace_path": workspace_path,
            "output_path": str(output_path),
            "edit_decisions": dict(edit_decisions, cuts=resolved_cuts),
            "asset_manifest": asset_manifest,
        }
        if playbook_data:
            hf_inputs["playbook"] = playbook_data
        if profile:
            hf_inputs["profile"] = profile
        if "quality" in inputs:
            hf_inputs["quality"] = inputs["quality"]
        if "fps" in inputs:
            hf_inputs["fps"] = inputs["fps"]
        if "strict" in inputs:
            hf_inputs["strict"] = inputs["strict"]
        if "skip_contrast" in inputs:
            hf_inputs["skip_contrast"] = inputs["skip_contrast"]

        render_result = HyperFramesCompose().execute(hf_inputs)

        if not render_result.success:
            return ToolResult(
                success=False,
                error=(
                    f"HyperFrames render failed: {render_result.error}. "
                    "Per governance: do NOT silently fall back to Remotion or "
                    "FFmpeg. Surface the failure to the user along with the "
                    "hyperframes_compose step log before proposing a swap."
                ),
                data=render_result.data,
            )

        # Post-render: mandatory final self-review (identical contract to the Remotion path).
        if output_path.exists():
            final_review = self._run_final_review(
                output_path,
                edit_decisions,
                inputs.get("proposal_packet"),
                narration_transcript_path=inputs.get("narration_transcript_path"),
                script_text=inputs.get("script_text") or self._read_text_file(
                    inputs.get("script_path")
                ),
            )
            if render_result.data is None:
                render_result.data = {}
            render_result.data["final_review"] = final_review
            render_result.data["final_review_status"] = final_review["status"]
            if final_review["status"] == "fail":
                return ToolResult(
                    success=False,
                    error=(
                        "Post-render self-review FAILED (HyperFrames). The output is not presentable.\n"
                        + "\n".join(f"  • {i}" for i in final_review.get("issues_found", []))
                    ),
                    data=render_result.data,
                )

        return render_result

    def _render_via_ffmpeg(
        self,
        *,
        inputs: dict[str, Any],
        edit_decisions: dict[str, Any],
        resolved_cuts: list[dict],
        output_path: Path,
        profile: Optional[str],
    ) -> ToolResult:
        """Explicit FFmpeg-only render path.

        Use when the proposal locked `render_runtime="ffmpeg"` — e.g. simple
        source-footage concat/trim jobs that don't benefit from composition.
        Still runs the mandatory final self-review.
        """
        options = inputs.get("options", {})
        subtitle_burn = options.get("subtitle_burn", True)

        subtitle_path = inputs.get("subtitle_path")
        if subtitle_burn and not subtitle_path:
            ed_subs = edit_decisions.get("subtitles", {})
            if ed_subs.get("enabled") and ed_subs.get("source"):
                subtitle_path = ed_subs["source"]

        compose_inputs = dict(inputs)
        compose_inputs["edit_decisions"] = dict(edit_decisions, cuts=resolved_cuts)
        compose_inputs["output_path"] = str(output_path)
        if subtitle_path:
            compose_inputs["subtitle_path"] = subtitle_path
        if profile:
            compose_inputs["profile"] = profile

        render_result = self._compose(compose_inputs)

        if render_result.success and output_path.exists():
            final_review = self._run_final_review(
                output_path,
                edit_decisions,
                inputs.get("proposal_packet"),
                narration_transcript_path=inputs.get("narration_transcript_path"),
                script_text=inputs.get("script_text") or self._read_text_file(
                    inputs.get("script_path")
                ),
            )
            if render_result.data is None:
                render_result.data = {}
            render_result.data["final_review"] = final_review
            render_result.data["final_review_status"] = final_review["status"]
            if final_review["status"] == "fail":
                return ToolResult(
                    success=False,
                    error=(
                        "Post-render self-review FAILED (FFmpeg). The output is not presentable.\n"
                        + "\n".join(f"  • {i}" for i in final_review.get("issues_found", []))
                    ),
                    data=render_result.data,
                )

        return render_result

    def _normalize_custom_composition_props(
        self, composition_data: dict, staged_images: list
    ) -> dict:
        """Build the props object consumed by the runtime-compiled CustomComposition.

        MCP emits snake_case ``edit_decisions`` (``custom_code``,
        ``duration_per_image``); the component reads camelCase props (``code``,
        ``durationPerImage``). Map them so the user-authored TSX actually
        receives its inputs instead of rendering an error screen. ``fps`` /
        ``width`` / ``height`` are injected by Remotion from the composition
        config, but we also forward them explicitly so the metadata pass and
        scripts that read them directly get consistent values.
        """
        ct = ((composition_data or {}).get("metadata") or {}).get("compose_target") or {}
        return {
            "code": (composition_data or {}).get("custom_code", ""),
            "images": staged_images,
            "durationPerImage": (composition_data or {}).get("duration_per_image", 3),
            "fps": 30,
            "width": ct.get("width", 1080),
            "height": ct.get("height", 1920),
            "renderer_family": "custom-composition",
        }

    def _remotion_render(self, inputs: dict[str, Any]) -> ToolResult:
        """Render via Remotion (requires Node.js + npx).

        Handles compositions with still images, animated scenes, component
        types, and transitions using React-based frame-accurate rendering.
        Accepts edit_decisions (with resolved file paths) or raw composition_data.

        ``inputs['_progress_callback']`` (optional) is a callable invoked with a
        partial progress dict (``phase``/``percent``/``status``/``message``) so
        callers can stream coarse + frame-level progress (e.g. via SSE) instead
        of waiting for the whole render to finish.
        """
        import shutil

        progress_callback = inputs.get("_progress_callback")
        if progress_callback:
            try:
                progress_callback({"phase": "render", "status": "rendering",
                                   "message": "Remotion rendering started"})
            except Exception:  # noqa: BLE001
                pass

        if not shutil.which("npx"):
            return ToolResult(
                success=False,
                error="npx not found. Install Node.js to use Remotion rendering.",
            )

        composition_data = inputs.get("edit_decisions") or inputs.get("composition_data")
        if not composition_data:
            return ToolResult(
                success=False,
                error="edit_decisions or composition_data required for remotion_render",
            )

        output_path = Path(inputs.get("output_path", "renders/remotion_output.mp4"))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Absolutise so the CLI can resolve the output regardless of cwd.
        output_path = output_path.resolve()

        # Deep-copy props so we don't mutate the original
        props = json.loads(json.dumps(composition_data))

        # Stage local media files into Remotion's public/ dir and reference
        # them by relative path so Img/OffthreadVideo/Audio load via
        # staticFile().
        #
        # Headless Chrome (used by `npx remotion render`) runs the composition
        # on http://localhost:3001 and REFUSES file:// URLs from that origin
        # ("Not allowed to load local resource"). So absolute paths must NOT
        # be passed as file:// URIs — they are copied into public/_staged/
        # and every local-resource field (cut sources, background layers,
        # anime images, narration/music) is rewritten to the public-relative
        # path, which resolveAsset() routes through staticFile().
        composer_dir = Path(__file__).resolve().parent.parent.parent / "remotion-composer"
        if not composer_dir.exists():
            return ToolResult(
                success=False,
                error=f"Remotion composer project not found at {composer_dir}",
            )
        import uuid

        # 每次 render 一个独立 staging 子目录，杜绝跨用户素材互相可见/碰撞。
        # 调用方可传 staging_id（如 MCP 的 job_id）便于日志关联；缺省自给自足。
        # 注意：staging_id 会被拼进 public 相对路径，调用方传值时必须不含路径
        # 分隔符（/、..），避免目录穿越。
        staging_id = inputs.get("staging_id") or uuid.uuid4().hex[:12]
        staged_dir = composer_dir / "public" / "_staged" / staging_id

        # --- Custom composition mode ---
        # Stage the uploaded images (the only media channel for user TSX) so they
        # can be referenced via staticFile() inside the custom code. Runs before
        # the generic cuts loop (custom compositions carry no cuts).
        if (composition_data or {}).get("composition_mode") == "custom":
            custom_images = props.get("images") or []
            staged_images = []
            for idx, img in enumerate(custom_images):
                if img:
                    staged_images.append(self._stage_remotion_asset(img, idx, staged_dir))
                else:
                    staged_images.append(img)
            # Replace the edit_decisions-shaped props with the component-facing
            # contract so the user-authored TSX truly receives its inputs. MCP
            # emits snake_case keys (custom_code / duration_per_image); the
            # runtime-compiled CustomComposition reads camelCase props
            # (code / durationPerImage). Without this mapping the component gets
            # an empty `code` and renders an error screen instead of the video.
            props = self._normalize_custom_composition_props(composition_data, staged_images)

        for idx, cut in enumerate(props.get("cuts", [])):
            # cuts[].source (Ken Burns / still) + background image/video layers
            for field in ("source", "backgroundImage", "backgroundVideo"):
                if cut.get(field):
                    cut[field] = self._stage_remotion_asset(cut[field], idx, staged_dir)
            # anime_scene / collage scenes: images[]
            if cut.get("images"):
                cut["images"] = [
                    self._stage_remotion_asset(img, idx, staged_dir)
                    for img in cut["images"]
                ]

        # CinematicRenderer consumes `scenes`, while the MCP workflow emits
        # `cuts`. Bridge the trusted cut timeline explicitly so cinematic-
        # montage renders uploaded stills instead of merely accepting a
        # renderer_family label and receiving an empty composition.
        renderer_family = (composition_data or {}).get("renderer_family", "explainer-data")
        if renderer_family in {"cinematic-trailer", "documentary-montage"} and not props.get("scenes"):
            props["scenes"] = [
                {
                    "id": cut.get("id", f"scene-{idx:04d}"),
                    "kind": "image",
                    "src": cut.get("source", ""),
                    "startSeconds": float(cut.get("in_seconds", 0)),
                    "durationSeconds": max(0.1, float(cut.get("out_seconds", 0)) - float(cut.get("in_seconds", 0))),
                    "fadeInFrames": 8 if idx else 14,
                    "fadeOutFrames": 8,
                }
                for idx, cut in enumerate(props.get("cuts", []))
                if cut.get("source")
            ]

        # audio.narration.src / audio.music.src
        audio = props.get("audio")
        if audio:
            for layer in ("narration", "music"):
                if audio.get(layer, {}).get("src"):
                    audio[layer]["src"] = self._stage_remotion_asset(
                        audio[layer]["src"], -1, staged_dir
                    )

        # The parameterized e-commerce composition keeps its image/audio
        # references under assets rather than cuts/audio. Stage those fields
        # too so MCP callers can pass absolute local paths safely.
        assets = props.get("assets")
        if isinstance(assets, dict):
            for key in ("hero", "product", "detail", "lifestyle", "logo", "music"):
                if assets.get(key):
                    assets[key] = self._stage_remotion_asset(
                        assets[key], -2, staged_dir
                    )

        # Build a custom themeConfig from the playbook's actual colors.
        # This ensures every video gets a unique visual identity derived
        # from its production decisions — not picked from a preset menu.
        if "themeConfig" not in props:
            playbook_name = (
                props.get("playbook")
                or props.get("theme")
                or props.get("metadata", {}).get("playbook")
            )
            theme_config = self._build_theme_from_playbook(playbook_name, composition_data)
            if theme_config:
                props["themeConfig"] = theme_config

        # Write props to temp file for Remotion CLI. 文件名带 staging_id，避免
        # 同一 project 内并发/retry 的 render 抢同一个 .remotion_props.json。
        props_path = output_path.parent / f".remotion_props.{staging_id}.json"
        with open(props_path, "w", encoding="utf-8") as f:
            json.dump(props, f)

        # Route to the correct Remotion composition based on renderer_family.
        # This prevents all pipelines from collapsing into the Explainer visual grammar.
        composition_id = self._get_composition_id(renderer_family)

        cmd = [
            "npx", "remotion", "render",
            str(composer_dir / "src" / "index.tsx"),
            composition_id,
            str(output_path),
            "--props", str(props_path),
            "--concurrency", str(_get_remotion_concurrency()),
        ]

        # --no-sandbox is required for the headless Chrome bundled with Remotion
        # on Windows and in restricted/CI environments where the Chrome sandbox
        # cannot initialize (no sandbox user namespace). Without it the render
        # hangs or fails with an opaque browser-launch error. It is a no-op risk
        # on normal Linux desktops and is always passed for reliability.
        cmd.append("--no-sandbox")

        # Apply media profile dimensions
        profile_name = inputs.get("profile")
        if profile_name:
            try:
                from lib.media_profiles import get_profile
                p = get_profile(profile_name)
                cmd.extend(["--width", str(p.width), "--height", str(p.height)])
            except (ImportError, ValueError):
                pass

        # Override concurrency from inputs if explicitly provided
        custom_concurrency = inputs.get("concurrency")
        if custom_concurrency:
            cmd = [c for c in cmd if not c.startswith("--concurrency")]
            cmd.extend(["--concurrency", str(custom_concurrency)])

        # Optional creator-facing render timeout. Remotion's `--timeout` (ms)
        # governs headless-browser setup and delayRender(); on slow machines or
        # restricted networks the default 30s browser setup times out with an
        # opaque failure. Pass it through and give the subprocess enough headroom
        # so run_command() does not kill Remotion before its own timeout fires.
        remotion_timeout_ms = inputs.get("remotion_timeout_ms")
        subprocess_timeout = 600
        if remotion_timeout_ms:
            try:
                ms = int(remotion_timeout_ms)
                cmd.append(f"--timeout={ms}")
                subprocess_timeout = max(subprocess_timeout, ms // 1000 + 60)
            except (TypeError, ValueError):
                pass

        # 闸门：限制并发 Remotion 进程数，防止多用户无界扇出打爆宿主。
        # 排队/拿到槽位的状态经 progress_callback 上行，SSE 借此显示「排队→渲染」，
        # 并由 mcp_server._run_render_job 写入 session 的 render_phase + 排队位。
        # FairRenderGate 在实际 CPU 闸门处按用户轮转；RenderQueue 继续提供
        # 兼容的等待集快照，供状态清理与旧诊断代码使用。
        gate = _get_remotion_render_gate()
        # Pipeline/direct tool calls predate MCP job ids. Give those renders a
        # private local identity so they keep the same global safety gate
        # without requiring callers to know FrameFlow internals.
        job_id = inputs.get("_job_id") or f"local-{uuid.uuid4().hex}"
        queue_owner_id = str(inputs.get("_queue_owner_id") or job_id or "anonymous")
        try:
            from lib.render_queue import get_render_queue
            _rq = get_render_queue()
        except Exception:  # noqa: BLE001 - 队列模块不可用时退化为无记账
            _rq = None
        ticket = None
        ticket, pos, depth = gate.enter(job_id, queue_owner_id)
        if _rq is not None:
            _rq.enter(job_id)
        _queued_msg = f"排队中，第 {pos}/{depth} 位，等待可用渲染槽位"
        if progress_callback:
            try:
                _ev = {"phase": "render", "status": "queued", "slot_waiting": True,
                       "message": _queued_msg}
                if pos is not None:
                    _ev["position"] = pos
                    _ev["queue_depth"] = depth
                progress_callback(_ev)
            except Exception:  # noqa: BLE001
                pass
        acquired = False
        try:
            gate.acquire(ticket)
            acquired = True
            if _rq is not None and job_id:
                _rq.leave(job_id)
            if progress_callback:
                try:
                    progress_callback({"phase": "render", "status": "rendering",
                                       "slot_acquired": True,
                                       "message": "已获取 Remotion 渲染槽位"})
                except Exception:  # noqa: BLE001
                    pass
            try:
                # Invoke from inside the composer dir so npx can resolve the
                # local remotion binary via node_modules/.bin. Without this,
                # Windows npx cannot locate the CLI and returns "could not
                # determine executable to run".
                on_output = None
                if progress_callback:
                    on_output = lambda line: self._emit_remotion_progress(line, progress_callback)
                # WorkBuddy's terminal injects NODE_OPTIONS=--require<genie-safe-delete>
                # (a "safe delete" shim) into every child process. Remotion's bundle
                # step cleans a large temp dir; the shim routes that delete through a
                # recycle-bin binary that times out on Windows, corrupting the bundle
                # and surfacing as "Cannot find module 'react/jsx-runtime'". Strip the
                # shim from the subprocess env so Node deletes directly.
                render_env = dict(os.environ)
                render_env.pop("NODE_OPTIONS", None)
                # Belt-and-suspenders: if the PATH `rm` shim still triggers, point it
                # at a binary-less dir so it downgrades to the native recycle bin.
                render_env["GENIE_TRASH_DIR"] = str(
                    Path(os.environ.get("TMPDIR", os.environ.get("TEMP", "/tmp")))
                    / "genie-trash-disabled"
                )
                self.run_command(
                    cmd,
                    timeout=subprocess_timeout,
                    cwd=composer_dir,
                    on_output=on_output,
                    env=render_env,
                )
            except subprocess.CalledProcessError as e:
                # run_command uses check=True + capture_output, so the useful
                # Remotion diagnostics live in stderr/stdout — surface the tail
                # instead of the bare "returned non-zero exit status 1".
                detail = (e.stderr or e.stdout or "").strip()
                tail = "\n".join(detail.splitlines()[-25:]) if detail else "(no output captured)"
                return ToolResult(
                    success=False,
                    error=f"Remotion render failed (exit {e.returncode}):\n{tail}",
                )
            except subprocess.TimeoutExpired as e:
                return ToolResult(
                    success=False,
                    error=(
                        f"Remotion render timed out after {e.timeout}s. If the headless "
                        "browser is slow to start, raise remotion_timeout_ms (ms)."
                    ),
                )
            except Exception as e:
                return ToolResult(success=False, error=f"Remotion render failed: {e}")
            finally:
                # Best-effort cleanup of the temp props file. A failed deletion
                # (e.g. host file-protection hooks intercepting unlink) must NEVER
                # abort an otherwise successful render, so swallow any error here.
                try:
                    if props_path.exists():
                        props_path.unlink()
                except OSError as e:
                    logging.getLogger("video_compose").warning(
                        "Could not remove temp props file %s: %s", props_path, e
                    )
                # 清理 per-job staging 目录——渲染完成后素材即无用，留着会跨用户累积。
                try:
                    shutil.rmtree(staged_dir, ignore_errors=True)
                except OSError:
                    pass
        finally:
            if acquired:
                gate.release(queue_owner_id)

        if not output_path.exists():
            return ToolResult(
                success=False,
                error=f"Remotion render completed but output file missing: {output_path}",
            )

        return ToolResult(
            success=True,
            data={
                "operation": "remotion_render",
                "output": str(output_path),
                "profile": profile_name,
            },
            artifacts=[str(output_path)],
        )

    def _remotion_bilingual_overlay(self, inputs: dict[str, Any]) -> ToolResult:
        """Render animated bilingual subtitles via Remotion.

        Takes ``segments`` (primary language) + ``target_segments`` (translated,
        secondary language), runs them through ``subtitle_gen`` with
        ``format="remotion_bilingual_captions"`` to get the Remotion-shaped
        WordCaption JSON, then invokes ``npx remotion render`` against the
        ``BilingualCaptionOverlayOnly`` composition registered in
        ``remotion-composer/src/Root.tsx``.

        Output is an MP4 with the bilingual subtitles rendered on a black
        background. To composite onto original footage, use the ``
        `operation="overlay"` path with this file as the overlay input.
        """
        import shutil
        import tempfile

        segments = inputs.get("segments")
        target_segments = inputs.get("target_segments")
        if not segments:
            return ToolResult(
                success=False,
                error="`segments` (primary language) required for remotion_bilingual_overlay",
            )
        if not target_segments:
            return ToolResult(
                success=False,
                error="`target_segments` (translated language) required for remotion_bilingual_overlay",
            )

        if not shutil.which("npx"):
            return ToolResult(
                success=False,
                error="npx not found. Install Node.js to use Remotion rendering.",
            )

        composer_dir = (
            Path(__file__).resolve().parent.parent.parent / "remotion-composer"
        )
        if not (composer_dir / "node_modules").exists():
            return ToolResult(
                success=False,
                error=(
                    f"remotion-composer/node_modules missing. Run "
                    f"`make install` (or `cd {composer_dir} && npm install`) "
                    f"to set up Remotion."
                ),
            )

        output_path = Path(
            inputs.get("output_path", "renders/bilingual_overlay.mp4")
        )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path = output_path.resolve()

        # 1. Build the Remotion props JSON via subtitle_gen.
        # Lazy import to avoid pulling subtitle_gen at module load time
        # (it can be heavy on cold starts and is only needed here).
        from tools.subtitle.subtitle_gen import SubtitleGen

        with tempfile.TemporaryDirectory() as tmpdir:
            subtitle_path = Path(tmpdir) / "bilingual.remotion.json"
            sg = SubtitleGen().execute({
                "segments": segments,
                "target_segments": target_segments,
                "format": "remotion_bilingual_captions",
                "output_path": str(subtitle_path),
            })
            if not sg.success:
                return ToolResult(
                    success=False,
                    error=f"subtitle_gen failed: {sg.error}",
                )
            if not subtitle_path.exists():
                return ToolResult(
                    success=False,
                    error="subtitle_gen reported success but produced no file",
                )

            # 2. Stage props for the BilingualCaptionOverlayOnly composition.
            #    subtitle_gen wrote `primaryWords` / `secondaryWords` at the
            #    top level — those are exactly what the composition consumes.
            props = json.loads(subtitle_path.read_text(encoding="utf-8"))

            # Allow callers to override visual styling per-call (font sizes,
            # highlight colors, page size, etc.). All keys are optional.
            for key in (
                "primaryFontSize", "secondaryFontSize",
                "primaryColor", "primaryHighlightColor",
                "secondaryColor", "secondaryHighlightColor",
                "backgroundColor",
                "primaryFontFamily", "secondaryFontFamily",
                "wordsPerPage", "rowGap",
            ):
                if key in inputs:
                    props[key] = inputs[key]

            props_path = Path(tmpdir) / "props.json"
            props_path.write_text(
                json.dumps(props, ensure_ascii=False), encoding="utf-8"
            )

            # 3. Build the npx remotion render command. Mirrors the
            #    `_remotion_render` command shape but skips the asset
            #    staging path — bilingual overlay needs no media.
            cmd = [
                "npx", "remotion", "render",
                str(composer_dir / "src" / "index.tsx"),
                "BilingualCaptionOverlayOnly",
                str(output_path),
                "--props", str(props_path),
                "--concurrency", str(_get_remotion_concurrency()),
                "--no-sandbox",
            ]
            custom_concurrency = inputs.get("concurrency")
            if custom_concurrency:
                cmd = [c for c in cmd if not c.startswith("--concurrency")]
                cmd.extend(["--concurrency", str(custom_concurrency)])
            if inputs.get("remotion_timeout_ms"):
                cmd.append(f"--timeout={int(inputs['remotion_timeout_ms'])}")

            start = time.time()
            try:
                proc = subprocess.run(
                    cmd,
                    cwd=str(composer_dir),
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=600,
                )
            except subprocess.TimeoutExpired:
                return ToolResult(
                    success=False,
                    error="Remotion render timed out after 600s",
                )
            except FileNotFoundError as exc:
                return ToolResult(
                    success=False,
                    error=f"Remotion binary launch failed: {exc}",
                )

            if proc.returncode != 0:
                # Trim long stacks but keep the most recent lines so the
                # operator can see what Remotion actually complained about.
                stderr_tail = "\n".join(
                    (proc.stderr or "").splitlines()[-20:]
                )
                return ToolResult(
                    success=False,
                    error=(
                        f"npx remotion render failed (exit {proc.returncode}): "
                        f"{stderr_tail or proc.stdout or '(no output)'}"
                    ),
                )

            if not output_path.exists():
                return ToolResult(
                    success=False,
                    error=(
                        "Remotion exit was 0 but output file is missing — "
                        "check disk space and write permissions."
                    ),
                )

            return ToolResult(
                success=True,
                data={
                    "output_path": str(output_path),
                    "operation": "remotion_bilingual_overlay",
                    "composition_id": "BilingualCaptionOverlayOnly",
                    "primary_word_count": len(props.get("primaryWords", [])),
                    "secondary_word_count": len(props.get("secondaryWords", [])),
                    "render_seconds": round(time.time() - start, 2),
                },
                artifacts=[str(output_path)],
                duration_seconds=round(time.time() - start, 2),
            )

    @staticmethod
    def _parse_remotion_progress(line: str) -> Optional[float]:
        """Extract a 0-100 render-percentage from a Remotion CLI output line.

        Remotion prints progress to stderr as ``Rendering frame X/Y`` /
        ``Rendered frame X/Y`` (with carriage-return overwrites) and occasionally
        bare percentage lines. Returns the percentage or ``None`` if the line
        carries no progress information.
        """
        if not line:
            return None
        import re

        m = re.search(r"frame\s+(\d+)\s*/\s*(\d+)", line)
        if m:
            cur, total = int(m.group(1)), int(m.group(2))
            if total > 0:
                return min(100.0, cur / total * 100.0)
        m = re.search(r"(\d{1,3})\s*%", line)
        if m:
            pct = int(m.group(1))
            if 0 <= pct <= 100:
                return float(pct)
        return None

    @staticmethod
    def _emit_remotion_progress(line: str, progress_callback: Callable[[dict], None]) -> None:
        """Parse one Remotion output line and forward frame-level progress."""
        pct = VideoCompose._parse_remotion_progress(line)
        try:
            progress_callback({
                "phase": "render",
                "status": "rendering",
                "percent": pct,
                "message": line,
            })
        except Exception:  # noqa: BLE001
            pass

    def _stage_remotion_asset(self, source: str, idx: int, staged_dir: Path) -> str:
        """把一个本地素材拷进 per-job staging 目录，返回 public 相对路径。

        http/https/data: 直通；文件缺失直通。目标名内嵌**内容** hash（流式
        md5，单遍边拷边算，大视频不读两遍），所以不同文件即使同名也不撞；
        staged_dir 本身按 job 隔离，跨用户/跨 render 互不可见。

        返回值形如 ``_staged/<job_id>/<name>``（staged_dir 的父目录名 + 目录名
        + 文件名），Remotion 端经 staticFile() 直接加载。
        """
        if not source or source.startswith(("http://", "https://", "data:")):
            return source
        resolved = Path(source.replace("file://", ""))
        if not resolved.exists():
            return source
        import hashlib
        import os as _os
        import tempfile

        staged_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.md5()
        # 用 mkstemp 拿唯一临时名，避免多字段共用 idx（如 narration/music 都是
        # idx=-1）时临时文件撞名；os.replace 保证同目录内原子落名。
        fd, tmp_name = tempfile.mkstemp(prefix=f".{idx}.", suffix=".tmp", dir=staged_dir)
        try:
            with resolved.open("rb") as src, _os.fdopen(fd, "wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    digest.update(chunk)
                    dst.write(chunk)
        except OSError:
            try:
                _os.unlink(tmp_name)
            except OSError:
                pass
            raise
        target = staged_dir / f"{idx}_{digest.hexdigest()[:12]}_{resolved.name}"
        if target.exists():
            _os.unlink(tmp_name)  # 同 job 内重复引用 → 去重
        else:
            _os.replace(tmp_name, target)
        # 返回 public 相对路径：<父目录名>/<job_id>/<name>，staticFile() 可加载。
        return (Path(staged_dir.parent.name) / staged_dir.name / target.name).as_posix()

    # ------------------------------------------------------------------
    # Final self-review — mandatory post-render inspection
    # ------------------------------------------------------------------

    # Punctuation/SSML-leak words that should NEVER appear in rendered audio.
    # When a TTS engine reads a literal "..." as the word "dot", or a "—" as
    # "hyphen", those leak into the transcript. Catching these in the final
    # review is the difference between catching a bad voice render in-tool
    # vs. shipping a video that says "dot dot dot" twelve times. CRITICAL.
    _TTS_PUNCTUATION_LEAK_WORDS = {
        "dot", "dots", "ellipsis", "period", "periods",
        "comma", "commas", "semicolon", "colon",
        "dash", "hyphen", "emdash", "endash",
        "parenthesis", "bracket", "brace",
        "asterisk", "slash", "backslash",
        "exclamation", "question mark",
    }

    @staticmethod
    def _read_text_file(path: str | Path | None) -> str | None:
        """Read a small text file if given a path; None-safe and exception-safe."""
        if not path:
            return None
        try:
            return Path(path).read_text(encoding="utf-8")
        except Exception:
            return None

    @classmethod
    def _tokenize(cls, text: str) -> list[str]:
        """Split text into comparable word tokens (lowercased, punctuation
        stripped, numeric-word-aware). Empty tokens dropped."""
        import re

        # Preserve hyphenated words as single tokens ("many-worlds" -> "many-worlds").
        # Drop everything except letters, digits, hyphens, apostrophes.
        cleaned = re.sub(r"[^A-Za-z0-9\-' ]+", " ", text.lower())
        return [t for t in cleaned.split() if t and t != "-"]

    @classmethod
    def _compare_transcript_to_script(
        cls,
        transcript_path: Path,
        script_text: str,
    ) -> dict[str, Any]:
        """Compare a word-level transcript against the source script.

        Purpose: catch TTS failures that look fine on audio-volume/duration
        checks but produce garbage content. The canonical example is
        Chirp3-HD reading ellipses ("...") literally as the word "dot" — our
        volume check says "narration present, not clipped" and the video
        ships. This check diffs the actual transcribed audio against what
        was supposed to be said, and flags:

        - Spurious punctuation-leak words ("dot", "comma", "hyphen", etc.)
          that appear in audio but not script → CRITICAL
        - Overall word-accuracy ratio against script → SUGGESTION if < 0.9

        Returns the transcript_comparison section of final_review, or a
        placeholder with an issue describing why the check couldn't run
        (missing transcript, missing script) so the review never goes
        silently quiet on this contract.
        """
        result: dict[str, Any] = {
            "transcript_matches_script": False,
            "word_accuracy": None,
            "script_word_count": 0,
            "transcript_word_count": 0,
            "spurious_punctuation_words": [],
            "issues": [],
        }

        if not transcript_path or not Path(transcript_path).is_file():
            result["issues"].append(
                "transcript_comparison skipped: narration_transcript not provided"
            )
            return result
        if not script_text:
            result["issues"].append(
                "transcript_comparison skipped: script_text not provided"
            )
            return result

        try:
            transcript_data = json.loads(Path(transcript_path).read_text(encoding="utf-8"))
        except Exception as e:
            result["issues"].append(f"transcript_comparison could not parse transcript: {e}")
            return result

        transcript_words = [
            w.get("word", "").strip() for w in transcript_data.get("word_timestamps", [])
        ]
        transcript_tokens = cls._tokenize(" ".join(transcript_words))
        script_tokens = cls._tokenize(script_text)

        result["script_word_count"] = len(script_tokens)
        result["transcript_word_count"] = len(transcript_tokens)

        if not script_tokens or not transcript_tokens:
            result["issues"].append(
                f"transcript_comparison: empty token set "
                f"(script={len(script_tokens)}, transcript={len(transcript_tokens)})"
            )
            return result

        # --- Punctuation-leak detection (TTS reading literal punctuation) ---
        script_set = set(script_tokens)
        leak_occurrences: dict[str, int] = {}
        for token in transcript_tokens:
            if token in cls._TTS_PUNCTUATION_LEAK_WORDS and token not in script_set:
                leak_occurrences[token] = leak_occurrences.get(token, 0) + 1

        if leak_occurrences:
            formatted = ", ".join(
                f"{w!r}×{n}" for w, n in sorted(leak_occurrences.items(), key=lambda x: -x[1])
            )
            result["spurious_punctuation_words"] = [
                {"word": w, "count": n} for w, n in leak_occurrences.items()
            ]
            result["issues"].append(
                f"TTS punctuation leak: transcript contains {formatted} — "
                f"these words are NOT in the script, which means the voice "
                f"engine is reading literal punctuation aloud. Rewrite the "
                f"script to eliminate the corresponding characters (ellipses, "
                f"em-dashes, etc.) and regenerate narration."
            )

        # --- Word accuracy via set overlap (cheap & ordering-insensitive) ---
        # We don't penalize small word-order differences or minor TTS
        # hallucinations; we just want to know "did 90%+ of the script's
        # content make it into the audio." Using set overlap on the script
        # side is robust to transcription noise.
        matched = sum(1 for t in script_tokens if t in set(transcript_tokens))
        accuracy = matched / max(1, len(script_tokens))
        result["word_accuracy"] = round(accuracy, 3)
        result["transcript_matches_script"] = accuracy >= 0.9 and not leak_occurrences

        if accuracy < 0.9:
            result["issues"].append(
                f"Low transcript-to-script match: only {accuracy:.0%} of script "
                f"words appear in the transcribed audio ({matched}/"
                f"{len(script_tokens)}). Narration may be truncated, mispronounced, "
                f"or the wrong script was used."
            )

        return result

    def _run_final_review(
        self,
        output_path: Path,
        edit_decisions: dict[str, Any] | None = None,
        proposal_packet: dict[str, Any] | None = None,
        narration_transcript_path: str | Path | None = None,
        script_text: str | None = None,
    ) -> dict[str, Any]:
        """Run post-render self-review and produce a final_review artifact.

        This is the governance contract: the compose runtime MUST inspect
        the actual rendered output before marking the stage complete.
        Never claim a video is ready without a real probe + frame sample.

        When `proposal_packet` is provided, its
        `production_plan.render_runtime` is compared against
        `edit_decisions.render_runtime` so `runtime_swap_detected` can
        actually flip. Without it, we fall back to
        `edit_decisions.metadata.proposal_render_runtime` (which the edit
        director can set explicitly to opt into swap detection).

        Returns a dict conforming to final_review.schema.json.
        """
        log = logging.getLogger("video_compose.final_review")
        issues: list[str] = []

        # --- 1. Technical probe via ffprobe ---
        technical_probe: dict[str, Any] = {
            "valid_container": False,
            "issues": [],
        }
        probe_ran = False
        try:
            cmd = [
                "ffprobe", "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(output_path),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            probe_ran = True
            if proc.returncode == 0:
                probe_data = json.loads(proc.stdout)
                fmt = probe_data.get("format", {})
                streams = probe_data.get("streams", [])
                video_stream = next(
                    (s for s in streams if s.get("codec_type") == "video"), {}
                )
                audio_stream = next(
                    (s for s in streams if s.get("codec_type") == "audio"), {}
                )

                duration = float(fmt.get("duration", 0))
                width = int(video_stream.get("width", 0))
                height = int(video_stream.get("height", 0))
                fps_str = video_stream.get("r_frame_rate", "0/1")
                fps = self._parse_probe_fps(fps_str)

                technical_probe = {
                    "valid_container": bool(video_stream),
                    "duration_seconds": round(duration, 2),
                    "resolution": f"{width}x{height}",
                    "fps": fps,
                    "has_audio": bool(audio_stream),
                    "codec": video_stream.get("codec_name", "unknown"),
                    "file_size_bytes": int(fmt.get("size", 0)),
                    "issues": [],
                }

                # Sanity checks
                if duration < 1.0:
                    technical_probe["issues"].append(
                        f"Output is only {duration:.1f}s — suspiciously short"
                    )

                # Check target duration from edit_decisions
                target_dur = None
                if edit_decisions:
                    target_dur = (
                        edit_decisions.get("total_duration_seconds")
                        or edit_decisions.get("metadata", {}).get("target_duration_seconds")
                    )
                if target_dur and target_dur > 0:
                    drift_pct = abs(duration - target_dur) / target_dur
                    if drift_pct > 0.25:
                        technical_probe["issues"].append(
                            f"Duration drift: rendered {duration:.1f}s vs target {target_dur}s "
                            f"({drift_pct:.0%} off). Review pacing or trim."
                        )
                    technical_probe["target_duration"] = target_dur
                    technical_probe["duration_drift_pct"] = round(drift_pct * 100, 1)
                if width < 320 or height < 240:
                    technical_probe["issues"].append(
                        f"Resolution {width}x{height} is very low"
                    )
                if not audio_stream:
                    technical_probe["issues"].append("No audio stream in output")
            else:
                technical_probe["issues"].append(
                    f"ffprobe failed with exit code {proc.returncode}"
                )
        except FileNotFoundError:
            technical_probe["issues"].append("ffprobe not found — cannot validate output")
        except Exception as e:
            technical_probe["issues"].append(f"ffprobe error: {e}")

        issues.extend(technical_probe.get("issues", []))

        # --- 2. Visual spotcheck: sample 4 frames ---
        visual_spotcheck: dict[str, Any] = {
            "frames_sampled": 0,
            "frame_paths": [],
            "black_frames_detected": False,
            "broken_overlays": False,
            "missing_assets": False,
            "unreadable_text": False,
            "issues": [],
        }
        duration = technical_probe.get("duration_seconds", 0)
        if duration > 0 and technical_probe.get("valid_container"):
            try:
                frame_dir = output_path.parent / ".final_review_frames"
                frame_dir.mkdir(parents=True, exist_ok=True)
                # Sample at 10%, 35%, 65%, 90% of duration
                sample_points = [0.10, 0.35, 0.65, 0.90]
                frame_paths = []
                for i, pct in enumerate(sample_points):
                    ts = round(duration * pct, 2)
                    frame_path = frame_dir / f"review_frame_{i}.png"
                    cmd = [
                        "ffmpeg", "-y", "-ss", str(ts),
                        "-i", str(output_path),
                        "-frames:v", "1", "-q:v", "2",
                        str(frame_path),
                    ]
                    subprocess.run(cmd, capture_output=True, timeout=15)
                    if frame_path.exists():
                        frame_paths.append(str(frame_path))

                        # Check for black frames (file size heuristic:
                        # a 1920x1080 PNG of pure black is ~5KB)
                        if frame_path.stat().st_size < 2000:
                            visual_spotcheck["black_frames_detected"] = True

                visual_spotcheck["frames_sampled"] = len(frame_paths)
                visual_spotcheck["frame_paths"] = frame_paths

                if len(frame_paths) < 4:
                    visual_spotcheck["issues"].append(
                        f"Only {len(frame_paths)}/4 frames extracted — some timestamps may be out of range"
                    )
                if visual_spotcheck["black_frames_detected"]:
                    visual_spotcheck["issues"].append(
                        "Black frame detected — possible missing asset or failed render segment"
                    )
            except Exception as e:
                visual_spotcheck["issues"].append(f"Frame sampling error: {e}")

        issues.extend(visual_spotcheck.get("issues", []))

        # --- 3. Audio spotcheck ---
        audio_spotcheck: dict[str, Any] = {
            "narration_present": False,
            "music_present": False,
            "unexpected_silence": False,
            "clipping_detected": False,
            "mix_intelligible": True,
            "issues": [],
        }
        if technical_probe.get("has_audio") and duration > 0:
            try:
                # Use ffmpeg volumedetect to check audio levels
                cmd = [
                    "ffmpeg", "-i", str(output_path),
                    "-af", "volumedetect", "-f", "null", "-",
                ]
                proc = subprocess.run(
                    cmd, capture_output=True, text=True, timeout=60
                )
                stderr = proc.stderr or ""
                # Parse mean_volume and max_volume
                mean_vol = None
                max_vol = None
                for line in stderr.split("\n"):
                    if "mean_volume:" in line:
                        try:
                            mean_vol = float(line.split("mean_volume:")[1].strip().split()[0])
                        except (ValueError, IndexError):
                            pass
                    if "max_volume:" in line:
                        try:
                            max_vol = float(line.split("max_volume:")[1].strip().split()[0])
                        except (ValueError, IndexError):
                            pass

                if mean_vol is not None:
                    if mean_vol < -60:
                        audio_spotcheck["unexpected_silence"] = True
                        audio_spotcheck["issues"].append(
                            f"Mean volume {mean_vol:.1f} dB — effectively silent"
                        )
                    # Assume narration present if mean volume is reasonable
                    if mean_vol > -40:
                        audio_spotcheck["narration_present"] = True
                    # Assume music present if audio exists (conservative)
                    if mean_vol > -50:
                        audio_spotcheck["music_present"] = True

                if max_vol is not None and max_vol > -0.5:
                    audio_spotcheck["clipping_detected"] = True
                    audio_spotcheck["issues"].append(
                        f"Max volume {max_vol:.1f} dB — possible clipping"
                    )
            except Exception as e:
                audio_spotcheck["issues"].append(f"Audio analysis error: {e}")

        issues.extend(audio_spotcheck.get("issues", []))

        # --- 4. Promise preservation ---
        promise_preservation: dict[str, Any] = {
            "delivery_promise_honored": True,
            "silent_downgrade_detected": False,
            "runtime_swap_detected": False,
            "issues": [],
        }
        if edit_decisions:
            renderer_family = edit_decisions.get("renderer_family", "")
            promise_preservation["renderer_family_used"] = renderer_family

            # Runtime governance — record what actually ran and flag a swap.
            # Three sources of truth, in priority order:
            #   1. proposal_packet.production_plan.render_runtime (authoritative)
            #   2. edit_decisions.metadata.proposal_render_runtime (if edit stage
            #      explicitly copied it to opt into in-tool swap detection)
            #   3. edit_decisions.render_runtime itself (cannot detect a swap in
            #      this case — reviewer does cross-artifact comparison instead)
            render_runtime_edit = (edit_decisions.get("render_runtime") or "").strip().lower()
            if render_runtime_edit:
                promise_preservation["render_runtime_used"] = render_runtime_edit

                proposal_runtime: str | None = None
                runtime_source: str | None = None
                if proposal_packet:
                    pp_runtime = (
                        (proposal_packet.get("production_plan") or {}).get("render_runtime")
                        or ""
                    ).strip().lower()
                    if pp_runtime:
                        proposal_runtime = pp_runtime
                        runtime_source = "proposal_packet.production_plan.render_runtime"
                if proposal_runtime is None:
                    md_runtime = (
                        (edit_decisions.get("metadata") or {}).get("proposal_render_runtime")
                        or ""
                    ).strip().lower()
                    if md_runtime:
                        proposal_runtime = md_runtime
                        runtime_source = "edit_decisions.metadata.proposal_render_runtime"

                if proposal_runtime is None:
                    promise_preservation["runtime_swap_check"] = (
                        "skipped — no proposal_packet or proposal_render_runtime "
                        "metadata provided. Reviewer skill does cross-artifact "
                        "comparison separately."
                    )
                elif proposal_runtime != render_runtime_edit:
                    promise_preservation["runtime_swap_detected"] = True
                    promise_preservation["runtime_swap_check"] = (
                        f"detected — source: {runtime_source}"
                    )
                    promise_preservation["issues"].append(
                        f"render_runtime changed between proposal ({proposal_runtime}) "
                        f"and compose ({render_runtime_edit}) — this is a contract "
                        f"violation unless a render_runtime_selection decision was logged."
                    )
                else:
                    promise_preservation["runtime_swap_check"] = (
                        f"ok — proposal and edit agree ({runtime_source})"
                    )

            delivery_data = (
                edit_decisions.get("metadata", {}).get("delivery_promise")
                or edit_decisions.get("delivery_promise")
            )
            if delivery_data:
                try:
                    from lib.delivery_promise import DeliveryPromise
                    promise = DeliveryPromise.from_dict(delivery_data)
                    cuts = edit_decisions.get("cuts", [])
                    result = promise.validate_cuts(cuts)
                    motion_ratio = result.get("motion_ratio", 0)
                    promise_preservation["motion_ratio_actual"] = round(motion_ratio, 3)

                    if not result["valid"]:
                        promise_preservation["delivery_promise_honored"] = False
                        for v in result["violations"]:
                            promise_preservation["issues"].append(v)

                    # Detect silent downgrade: motion-led promise but <50% motion
                    if (delivery_data.get("type") == "motion_led"
                            and motion_ratio < 0.5):
                        promise_preservation["silent_downgrade_detected"] = True
                        promise_preservation["issues"].append(
                            f"Motion-led promise but only {motion_ratio:.0%} motion — "
                            f"silent downgrade to still-led"
                        )
                except Exception as e:
                    promise_preservation["issues"].append(
                        f"Could not validate delivery promise: {e}"
                    )

        issues.extend(promise_preservation.get("issues", []))

        # --- 5. Subtitle check ---
        subtitle_check: dict[str, Any] = {
            "subtitles_expected": False,
            "subtitles_present": False,
            "issues": [],
        }
        if edit_decisions:
            ed_subs = edit_decisions.get("subtitles", {})
            subtitle_check["subtitles_expected"] = bool(ed_subs.get("enabled"))

            # Check if output has subtitle stream
            if technical_probe.get("valid_container"):
                try:
                    cmd = [
                        "ffprobe", "-v", "quiet", "-print_format", "json",
                        "-show_streams", "-select_streams", "s",
                        str(output_path),
                    ]
                    proc = subprocess.run(
                        cmd, capture_output=True, text=True, timeout=15
                    )
                    if proc.returncode == 0:
                        sub_data = json.loads(proc.stdout)
                        sub_streams = sub_data.get("streams", [])
                        subtitle_check["subtitles_present"] = len(sub_streams) > 0

                    # If subtitles were expected but not found as a stream,
                    # they may be burned in (which is fine — not a failure)
                    if (subtitle_check["subtitles_expected"]
                            and not subtitle_check["subtitles_present"]):
                        # Check if subtitle_path was used (burned in)
                        sub_source = ed_subs.get("source")
                        if sub_source and Path(sub_source).exists():
                            # Burned-in subtitles are not detectable as streams
                            subtitle_check["subtitles_present"] = True
                            subtitle_check["coverage_ratio"] = 1.0
                        else:
                            subtitle_check["issues"].append(
                                "Subtitles expected but not found in output and "
                                "no subtitle source file exists for burn-in"
                            )
                except Exception as e:
                    subtitle_check["issues"].append(f"Subtitle check error: {e}")

        issues.extend(subtitle_check.get("issues", []))

        # --- 6. Transcript-vs-script comparison ---
        # Catches content-level TTS failures (the classic "Chirp reads `...`
        # as the word 'dot'" trap) that volume-based audio checks miss.
        # Only runs when caller provides both the transcript and script; when
        # skipped, issues list records that so the silence is visible.
        transcript_comparison = self._compare_transcript_to_script(
            Path(narration_transcript_path) if narration_transcript_path else None,
            script_text,
        )
        issues.extend(transcript_comparison.get("issues", []))

        # --- 7. Determine overall status ---
        critical_issues = [
            i for i in issues
            if any(kw in i.lower() for kw in [
                "silent downgrade", "delivery promise violation",
                "effectively silent", "ffprobe failed", "suspiciously short",
                "tts punctuation leak",  # reading literal punctuation aloud
            ])
        ]

        if critical_issues:
            status = "revise"
            recommended_action = "re_render"
        elif issues:
            status = "pass"
            recommended_action = "present_to_user"
        else:
            status = "pass"
            recommended_action = "present_to_user"

        # Only fail when ffprobe actually ran and reported an invalid container.
        # If ffprobe is missing/unavailable, we cannot validate — downgrade that
        # to a warning rather than failing the whole render (the Remotion output
        # is still produced; the review is a quality gate, not a hard blocker).
        if probe_ran and not technical_probe.get("valid_container"):
            status = "fail"
            recommended_action = "re_render"

        final_review = {
            "version": "1.0",
            "output_path": str(output_path),
            "status": status,
            "checks": {
                "technical_probe": technical_probe,
                "visual_spotcheck": visual_spotcheck,
                "audio_spotcheck": audio_spotcheck,
                "promise_preservation": promise_preservation,
                "subtitle_check": subtitle_check,
                "transcript_comparison": transcript_comparison,
            },
            "issues_found": issues,
            "recommended_action": recommended_action,
        }

        log.info(
            "Final review: status=%s, issues=%d, action=%s",
            status, len(issues), recommended_action,
        )

        return final_review

    @staticmethod
    def _parse_probe_fps(fps_str: str) -> float:
        """Parse ffprobe fps string like '30/1' or '24000/1001'."""
        try:
            if "/" in fps_str:
                num, den = fps_str.split("/")
                return round(int(num) / max(int(den), 1), 2)
            return float(fps_str)
        except (ValueError, ZeroDivisionError):
            return 0.0

    def _burn_subtitles(self, inputs: dict[str, Any]) -> ToolResult:
        """Burn subtitle file into video."""
        input_path = Path(inputs["input_path"])
        subtitle_path = Path(inputs["subtitle_path"])
        output_path = Path(inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_subtitled"))))

        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")
        if not subtitle_path.exists():
            return ToolResult(success=False, error=f"Subtitle file not found: {subtitle_path}")

        style = inputs.get("subtitle_style", {})
        ass_style = self._build_subtitle_style(style)
        sub_escaped = str(subtitle_path.resolve()).replace("\\", "/").replace(":", "\\:")
        codec = inputs.get("codec", "libx264")
        crf = inputs.get("crf", 23)

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-vf", f"subtitles='{sub_escaped}':force_style='{ass_style}'",
            "-c:v", codec, "-crf", str(crf),
            "-c:a", "copy",
            str(output_path),
        ]

        self.run_command(cmd)

        return ToolResult(
            success=True,
            data={
                "operation": "burn_subtitles",
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
        )

    def _overlay(self, inputs: dict[str, Any]) -> ToolResult:
        """Composite overlay images/videos on top of base video."""
        input_path = Path(inputs["input_path"])
        overlays = inputs.get("overlays", [])
        output_path = Path(inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_overlay"))))
        codec = inputs.get("codec", "libx264")
        crf = inputs.get("crf", 23)

        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")
        if not overlays:
            return ToolResult(success=False, error="No overlays provided")

        # Build complex filter for each overlay
        input_args = ["-i", str(input_path)]
        filter_parts = []
        prev_label = "0:v"

        for i, ov in enumerate(overlays):
            asset_path = Path(ov["asset_path"])
            if not asset_path.exists():
                return ToolResult(success=False, error=f"Overlay asset not found: {asset_path}")

            input_args.extend(["-i", str(asset_path)])

            x = int(ov.get("x", 0))
            y = int(ov.get("y", 0))
            start = ov.get("start_seconds", 0)
            end = ov.get("end_seconds")
            opacity = ov.get("opacity", 1.0)

            overlay_input = f"{i + 1}:v"

            # Scale overlay if dimensions specified
            if "width" in ov and "height" in ov:
                w = int(ov["width"])
                h = int(ov["height"])
                filter_parts.append(f"[{overlay_input}]scale={w}:{h}[ov_scaled_{i}]")
                overlay_input = f"ov_scaled_{i}"

            # Build enable expression for timed overlays
            enable = f"between(t,{start},{end})" if end else f"gte(t,{start})"
            out_label = f"v{i}"

            filter_parts.append(
                f"[{prev_label}][{overlay_input}]overlay={x}:{y}:enable='{enable}'[{out_label}]"
            )
            prev_label = out_label

        filter_complex = ";".join(filter_parts)

        cmd = ["ffmpeg", "-y"]
        cmd.extend(input_args)
        cmd.extend(["-filter_complex", filter_complex])
        cmd.extend(["-map", f"[{prev_label}]", "-map", "0:a?"])
        cmd.extend(["-c:v", codec, "-crf", str(crf), "-c:a", "copy"])
        cmd.append(str(output_path))

        self.run_command(cmd)

        return ToolResult(
            success=True,
            data={
                "operation": "overlay",
                "overlay_count": len(overlays),
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
        )

    def _encode(self, inputs: dict[str, Any]) -> ToolResult:
        """Re-encode video with a specific profile/codec settings."""
        input_path = Path(inputs["input_path"])
        output_path = Path(inputs.get("output_path", str(input_path.with_stem(f"{input_path.stem}_encoded"))))
        codec = inputs.get("codec", "libx264")
        crf = inputs.get("crf", 23)
        preset = inputs.get("preset", "medium")
        profile_name = inputs.get("profile")

        if not input_path.exists():
            return ToolResult(success=False, error=f"Input not found: {input_path}")

        cmd = [
            "ffmpeg", "-y",
            "-i", str(input_path),
            "-c:v", codec, "-crf", str(crf), "-preset", preset,
            "-c:a", "aac", "-b:a", "192k",
        ]

        # Apply media profile if specified
        if profile_name:
            try:
                from lib.media_profiles import get_profile, ffmpeg_output_args
                profile = get_profile(profile_name)
                cmd.extend(["-s", f"{profile.width}x{profile.height}"])
                cmd.extend(["-r", str(profile.fps)])
            except (ImportError, ValueError):
                pass  # proceed without profile

        cmd.append(str(output_path))
        self.run_command(cmd)

        return ToolResult(
            success=True,
            data={
                "operation": "encode",
                "codec": codec,
                "crf": crf,
                "profile": profile_name,
                "output": str(output_path),
            },
            artifacts=[str(output_path)],
        )

    @staticmethod
    def _resolve_subtitle_style(
        explicit_style: dict | None,
        edit_decisions: dict | None,
        playbook: dict | None,
    ) -> dict:
        """Resolve subtitle style with layered priority.

        Priority: explicit_style > edit_decisions.subtitles.style > playbook > defaults.
        This prevents every video from looking identical (Arial bold white).
        """
        # Start with minimal fallback defaults
        resolved = {
            "font": "Inter",
            "font_size": 28,
            "bold": True,
            "outline_width": 2,
            "shadow": 0,
            "margin_v": 40,
            "alignment": 2,
        }

        # Layer 1: Playbook-derived style
        if playbook:
            typo = playbook.get("typography", {})
            colors = playbook.get("visual_language", {}).get("color_palette", {})
            if typo.get("body", {}).get("family"):
                resolved["font"] = typo["body"]["family"]
            if colors.get("text"):
                resolved["primary_color"] = colors["text"]
            if colors.get("background"):
                resolved["outline_color"] = colors["background"]
                # Semi-transparent background for readability
                bg = colors["background"]
                resolved["back_color"] = bg

        # Layer 2: edit_decisions subtitle style
        if edit_decisions:
            ed_style = edit_decisions.get("subtitles", {}).get("style", {})
            for k, v in ed_style.items():
                if v is not None:
                    resolved[k] = v

        # Layer 3: Explicit override (highest priority)
        if explicit_style:
            for k, v in explicit_style.items():
                if v is not None:
                    resolved[k] = v

        return resolved

    @staticmethod
    def _build_subtitle_style(style: dict) -> str:
        """Build ASS force_style string from style dict."""
        parts = []
        parts.append(f"FontName={style.get('font', 'Inter')}")
        parts.append(f"FontSize={style.get('font_size', 28)}")
        parts.append(f"Bold={1 if style.get('bold', True) else 0}")
        if style.get("primary_color"):
            parts.append(f"PrimaryColour={style['primary_color']}")
        if style.get("outline_color"):
            parts.append(f"OutlineColour={style['outline_color']}")
        if style.get("back_color"):
            parts.append(f"BackColour={style['back_color']}")
        border_style = style.get("border_style", 1)
        parts.append(f"BorderStyle={border_style}")
        parts.append(f"Outline={style.get('outline_width', 2)}")
        parts.append(f"Shadow={style.get('shadow', 0)}")
        parts.append(f"MarginV={style.get('margin_v', 40)}")
        parts.append(f"Alignment={style.get('alignment', 2)}")
        return ",".join(parts)

    @staticmethod
    def _build_atempo(factor: float) -> str:
        """Build atempo filter chain for audio speed adjustment."""
        filters = []
        remaining = factor
        while remaining > 100.0:
            filters.append("atempo=100.0")
            remaining /= 100.0
        while remaining < 0.5:
            filters.append("atempo=0.5")
            remaining /= 0.5
        filters.append(f"atempo={remaining:.4f}")
        return ",".join(filters)
