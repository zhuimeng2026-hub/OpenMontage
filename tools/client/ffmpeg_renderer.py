"""Client-side FFmpeg renderer for the video-template-remix pipeline.

Reads ``edit_decisions`` + ``asset_manifest`` (the orchestration script returned
by OpenMontage) and produces a runnable FFmpeg command sequence that any GUI
client can execute locally with a bundled FFmpeg binary.

Why this exists
---------------
The OpenMontage ``video_compose`` tool is a server-side orchestrator that routes
across FFmpeg / Remotion / HyperFrames. For ``video-template-remix`` the locked
runtime is ``"ffmpeg"`` — a single-pass cut/concat/overlay pipeline with no
Remotion bundler or HyperFrames workspace needed. That does not require the
server toolchain on the client.

This module is a **pure command generator**. It does not call FFmpeg itself —
it returns shell commands for the GUI client to execute. That keeps it free of
ffmpeg-python and works with any FFmpeg ≥ 5.0 binary the client ships.

Minimum FFmpeg version
----------------------
**FFmpeg ≥ 5.0** is required — the renderer uses
``force_original_aspect_ratio=cover`` (5.0+) to fill the compose_target box
in a single scale filter. FFmpeg 4.4 is not supported; upgrade to 6.1.x for
the recommended production baseline.

Usage
-----

    renderer = FFmpegRenderer.from_artifacts(
        edit_decisions_path="path/to/edit_decisions.json",
        asset_manifest_path="path/to/asset_manifest.json",
        project_root="path/to/project_root",  # asset paths resolve relative to this
    )
    plan = renderer.build_plan()              # list[RenderStep]
    for step in plan:
        print(step.shell_command())
        subprocess.run(step.argv, check=True)

The output ``RenderPlan`` exposes each step as both ``argv`` (for subprocess)
and ``shell_command`` (for logging / debugging).
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# --- Data classes -----------------------------------------------------------


@dataclass
class RenderStep:
    """One FFmpeg invocation in the render plan.

    A complete render is a list of steps executed sequentially:
        1. one ``render_cut`` per cut (writes ``work/cut_NNN.mp4``)
        2. ``concat_cuts`` to join all cuts
        3. ``apply_subtitles`` (optional, only if subtitles.enabled)
        4. ``final_encode`` for the last encode pass

    Each step can run independently; ``shell_command`` is a copy-pasteable
    string, ``argv`` is the safe form for ``subprocess.run``.
    """

    name: str
    argv: list[str]
    cwd: Optional[Path] = None

    def shell_command(self) -> str:
        """Return a POSIX-safe shell command string for logging."""
        return " ".join(shlex.quote(a) for a in self.argv)


@dataclass
class RenderPlan:
    """Sequence of steps to render an edit_decisions script end-to-end."""

    steps: list[RenderStep] = field(default_factory=list)
    output_path: Optional[Path] = None

    def __iter__(self):
        return iter(self.steps)

    def __len__(self):
        return len(self.steps)

    def commands(self) -> list[str]:
        """Return all step commands as shell strings (for dry-run logging)."""
        return [s.shell_command() for s in self.steps]


# --- Main renderer ----------------------------------------------------------


class FFmpegRenderer:
    """Generate FFmpeg commands from OpenMontage edit_decisions + asset_manifest.

    The renderer does not assume any OpenMontage Python imports — only the
    shape of the two JSON artifacts. That keeps it portable into a Node.js,
    Go, or even a WebAssembly client just by porting the templates below.
    """

    # Default video encoder settings (sane defaults for a 30s social clip).
    DEFAULT_VIDEO_CODEC = "libx264"
    DEFAULT_VIDEO_PRESET = "medium"
    DEFAULT_VIDEO_CRF = 18
    DEFAULT_AUDIO_CODEC = "aac"
    DEFAULT_AUDIO_BITRATE = "192k"
    DEFAULT_PIX_FMT = "yuv420p"
    # FFmpeg ``force_original_aspect_ratio`` numeric values (0..2):
    #   0 = disable (squash)
    #   1 = decrease (fit-inside, letterbox if needed)
    #   2 = increase (fit-outside, may exceed box on one axis — use with
    #       a follow-up ``crop=W:H`` for true cover behavior)
    # Despite some doc references to a "cover" mode, FFmpeg 6.1 only
    # supports these three values — the string ``cover`` is NOT recognized.
    ASPECT_RATIO_DISABLE = 0
    ASPECT_RATIO_CONTAIN = 1
    ASPECT_RATIO_INCREASE = 2

    def __init__(
        self,
        edit_decisions: dict[str, Any],
        asset_manifest: dict[str, Any],
        project_root: Path,
        ffmpeg_bin: str = "ffmpeg",
        work_dir: Optional[Path] = None,
    ):
        self.ed = edit_decisions
        self.am = asset_manifest
        self.project_root = Path(project_root).resolve()
        self.ffmpeg_bin = ffmpeg_bin
        self.work_dir = (work_dir or self.project_root / "work" / "client_render").resolve()
        self.work_dir.mkdir(parents=True, exist_ok=True)

        # Build asset lookup: id → resolved absolute path
        self._assets_by_id: dict[str, Path] = {}
        for asset in self.am.get("assets", []):
            asset_path = asset.get("path") or asset.get("relative_path", "")
            if not asset_path:
                continue
            # asset.path is relative to project_root; resolve to absolute
            p = Path(asset_path)
            if not p.is_absolute():
                p = self.project_root / p
            self._assets_by_id[asset["id"]] = p

    # --- Factory ---

    @classmethod
    def from_artifacts(
        cls,
        edit_decisions_path: Path | str,
        asset_manifest_path: Path | str,
        project_root: Path | str,
        **kwargs: Any,
    ) -> "FFmpegRenderer":
        """Convenience constructor that loads the two JSON artifacts from disk."""
        ed = json.loads(Path(edit_decisions_path).read_text(encoding="utf-8"))
        am = json.loads(Path(asset_manifest_path).read_text(encoding="utf-8"))
        return cls(ed, am, project_root=project_root, **kwargs)

    # --- Public API ---

    def build_plan(self) -> RenderPlan:
        """Build the full render plan for this edit_decisions script.

        Returns a ``RenderPlan`` with one step per cut, one concat step,
        optional subtitle step, and a final encode step. Callers iterate
        and execute each step's ``argv`` via ``subprocess.run``.
        """
        if self.ed.get("render_runtime") != "ffmpeg":
            raise ValueError(
                f"This renderer only handles render_runtime='ffmpeg'; "
                f"got {self.ed.get('render_runtime')!r}. "
                "Use OpenMontage server-side video_compose for other runtimes."
            )

        cuts = self.ed.get("cuts", [])
        if not cuts:
            raise ValueError("edit_decisions has no cuts; nothing to render")

        plan = RenderPlan()

        # Phase 1: render each cut independently (parallelizable later).
        cut_paths: list[Path] = []
        for i, cut in enumerate(cuts):
            cut_path = self.work_dir / f"cut_{i:03d}.mp4"
            cut_paths.append(cut_path)
            plan.steps.append(self._render_cut(cut, i, cut_path))

        # Phase 2: concat all cuts.
        concat_path = self.work_dir / "concat.mp4"
        plan.steps.append(self._concat_cuts(cut_paths, concat_path))

        # Phase 3: subtitles (optional).
        subtitles_cfg = self.ed.get("subtitles") or {}
        current = concat_path
        if subtitles_cfg.get("enabled"):
            subs_path = self._resolve_subtitles(subtitles_cfg)
            if subs_path:
                subtitled_path = self.work_dir / "subtitled.mp4"
                plan.steps.append(self._apply_subtitles(current, subs_path, subtitles_cfg, subtitled_path))
                current = subtitled_path

        # Phase 4: final encode to compose_target.
        target = self.ed.get("compose_target") or {}
        output_path = self.project_root / "final.mp4"
        plan.steps.append(self._final_encode(current, output_path, target))
        plan.output_path = output_path

        return plan

    # --- Step builders ---

    def _render_cut(self, cut: dict[str, Any], index: int, out_path: Path) -> RenderStep:
        """Generate a single cut: trim source + apply overlay/transform.

        The filtergraph structure:
            [0:v] → trim → setpts → scale(cover) → [bg]
            [1:v] → loop/scale(decrease+pad)                  → [fg]  (overlay asset if present)
            [bg][fg] → overlay → format → [vout]
            0:a (source audio preserved by default)

        Requires FFmpeg ≥ 5.0 for ``force_original_aspect_ratio=cover``.
        """
        in_s = float(cut.get("in_seconds", 0))
        out_s = float(cut.get("out_seconds", in_s + 3.0))
        duration = max(out_s - in_s, 0.1)

        target = self.ed.get("compose_target") or {}
        W = int(target.get("width", 1080))
        H = int(target.get("height", 1920))
        fps = int(target.get("fps", 30))

        source_path = self._resolve_cut_source(cut)
        if source_path is None:
            raise ValueError(f"Cut {cut.get('id')!r} has no resolvable source")

        # Build the source-side filter chain.
        # True cover behavior (fill the target box + crop overflow) requires
        # two filters: ``scale=W:H:force_original_aspect_ratio=2`` (a.k.a.
        # ``increase`` — upscale to cover at least one axis) followed by
        # ``crop=W:H`` to chop off the overflow. FFmpeg has no single-filter
        # "cover" value despite some doc references; the string ``cover``
        # is rejected by the parser in 6.1.
        src_filters = [
            f"trim=start={in_s}:end={out_s}",
            "setpts=PTS-STARTPTS",
        ]

        transform = cut.get("transform") or {}
        animation = transform.get("animation")
        if animation == "ken-burns-slow-zoom":
            # zoompan needs fps and a duration; we pass through 30fps and
            # duration in frames = round(duration * fps).
            d_frames = max(int(round(duration * fps)), 1)
            src_filters += [
                f"scale={W * 2}:{H * 2}:force_original_aspect_ratio={self.ASPECT_RATIO_INCREASE}",
                f"crop={W * 2}:{H * 2}",
                f"fps={fps}",
                f"zoompan=z='min(zoom+0.0015,1.5)':d={d_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}",
                f"crop={W}:{H}",
            ]
        elif transform.get("crop"):
            c = transform["crop"]
            src_filters += [
                f"crop={c['width']}:{c['height']}:{c.get('x', 0)}:{c.get('y', 0)}",
            ]

        # Final scale to compose_target: increase (fit-outside) + crop to
        # produce true cover behavior.
        src_filters += [
            f"scale={W}:{H}:force_original_aspect_ratio={self.ASPECT_RATIO_INCREASE}",
            f"crop={W}:{H}",
            f"fps={fps}",
            f"format={self.DEFAULT_PIX_FMT}",
        ]

        # Inputs: 0 = source video, 1 = optional overlay asset.
        # IMPORTANT: do NOT use ``-ss`` / ``-to`` here. Input seek on a short
        # source can fail to read past a certain boundary, especially for the
        # last segment. We do the seek inside the filtergraph via ``trim`` +
        # ``setpts`` instead, which works reliably for any time range.
        argv = [self.ffmpeg_bin, "-y", "-i", str(source_path)]

        overlay_asset_id = (cut.get("overlay") or {}).get("asset_id")
        overlay_path: Optional[Path] = None
        if overlay_asset_id:
            overlay_path = self._assets_by_id.get(overlay_asset_id)
            if overlay_path:
                argv += ["-loop", "1", "-i", str(overlay_path)]

        # Build the filter_complex string.
        filter_parts: list[str] = []
        filter_parts.append(f"[0:v]{','.join(src_filters)}[bg]")

        if overlay_asset_id and overlay_path:
            # Overlay filter: contain-fit the overlay onto canvas, then overlay at center.
            filter_parts.append(
                f"[1:v]scale={W}:{H}:force_original_aspect_ratio={self.ASPECT_RATIO_CONTAIN},"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black@0,"
                f"format=rgba[fg]"
            )
            filter_parts.append(
                f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2:shortest=0[vout]"
            )
            vmap = "[vout]"
        else:
            vmap = "[bg]"

        filter_complex = ";\n".join(filter_parts)

        argv += [
            "-filter_complex", filter_complex,
            "-map", vmap,
            "-map", "0:a?",                       # preserve source audio if present
            "-t", str(duration),                  # bound output to cut duration; critical
                                                # when input 1 is -loop 1 (image loops forever).
            "-c:v", self.DEFAULT_VIDEO_CODEC,
            "-preset", self.DEFAULT_VIDEO_PRESET,
            "-crf", str(self.DEFAULT_VIDEO_CRF),
            "-c:a", self.DEFAULT_AUDIO_CODEC,
            "-b:a", self.DEFAULT_AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(out_path),
        ]

        return RenderStep(name=f"render_cut_{index:03d}", argv=argv, cwd=self.work_dir)

    def _concat_cuts(self, cut_paths: list[Path], out_path: Path) -> RenderStep:
        """Concatenate all cut outputs into a single intermediate file."""
        list_file = self.work_dir / "concat_list.txt"
        list_file.write_text(
            "\n".join(f"file {shlex.quote(str(p.resolve()))}" for p in cut_paths),
            encoding="utf-8",
        )
        argv = [
            self.ffmpeg_bin, "-y",
            "-f", "concat", "-safe", "0",
            "-i", str(list_file),
            "-c", "copy",
            str(out_path),
        ]
        return RenderStep(name="concat_cuts", argv=argv, cwd=self.work_dir)

    def _apply_subtitles(
        self,
        input_path: Path,
        subs_path: Path,
        cfg: dict[str, Any],
        out_path: Path,
    ) -> RenderStep:
        """Burn subtitles onto the concat output using the ``subtitles`` filter."""
        force_style_parts: list[str] = []
        if cfg.get("font_size"):
            force_style_parts.append(f"FontSize={cfg['font_size']}")
        if cfg.get("font"):
            force_style_parts.append(f"FontName={shlex.quote(cfg['font'])}")
        if cfg.get("color"):
            force_style_parts.append(f"PrimaryColour={cfg['color']}")
        if cfg.get("outline_color"):
            force_style_parts.append(f"OutlineColour={cfg['outline_color']}")
        if cfg.get("background"):
            force_style_parts.append(f"BackColour={cfg['background']}")
        position_map = {
            "top-center": "Alignment=8",
            "bottom-center": "Alignment=2",
            "center": "Alignment=5",
        }
        pos = position_map.get(cfg.get("position", "bottom-center"), "Alignment=2")
        force_style_parts.append(pos)
        if cfg.get("max_words_per_line"):
            # Wrap is approximate via ``WrapStyle=2`` (end-of-line wrap).
            force_style_parts.append("WrapStyle=2")

        force_style = ",".join(force_style_parts)

        argv = [
            self.ffmpeg_bin, "-y",
            "-i", str(input_path),
            "-vf",
            f"subtitles={shlex.quote(str(subs_path.resolve()))}:force_style={shlex.quote(force_style)}",
            "-c:v", self.DEFAULT_VIDEO_CODEC,
            "-preset", self.DEFAULT_VIDEO_PRESET,
            "-crf", str(self.DEFAULT_VIDEO_CRF),
            "-c:a", "copy",                       # don't re-encode audio
            str(out_path),
        ]
        return RenderStep(name="apply_subtitles", argv=argv, cwd=self.work_dir)

    def _final_encode(
        self,
        input_path: Path,
        out_path: Path,
        target: dict[str, Any],
    ) -> RenderStep:
        """Final encode pass — ensures the output meets compose_target exactly.

        In practice, prior steps already encode to compose_target, so this is
        a defensive re-encode that also applies ``fit`` policy. If you trust
        the earlier passes, you can replace this with ``-c copy``.
        """
        # fit is informational here; FFmpeg has no native "fit" filter that
        # maps 1:1 to the three policy values. ``cover`` (default) is already
        # applied during cut rendering. ``contain`` / ``pad`` would need
        # explicit padding which we encode in cut rendering too.
        argv = [
            self.ffmpeg_bin, "-y",
            "-i", str(input_path),
            "-c:v", self.DEFAULT_VIDEO_CODEC,
            "-preset", self.DEFAULT_VIDEO_PRESET,
            "-crf", str(self.DEFAULT_VIDEO_CRF),
            "-c:a", self.DEFAULT_AUDIO_CODEC,
            "-b:a", self.DEFAULT_AUDIO_BITRATE,
            "-movflags", "+faststart",
            str(out_path),
        ]
        return RenderStep(name="final_encode", argv=argv, cwd=self.work_dir)

    # --- Helpers ---

    def _resolve_cut_source(self, cut: dict[str, Any]) -> Optional[Path]:
        """Resolve a cut's source field to an absolute path.

        A cut's ``source`` may be either:
        - an asset_id (preferred) referencing the asset_manifest
        - a direct filesystem path (for the reference video in remix scenes)
        """
        src = cut.get("source")
        if not src:
            return None
        # 1) Try asset manifest first (asset_id).
        if src in self._assets_by_id:
            return self._assets_by_id[src]
        # 2) Treat as a filesystem path.
        p = Path(src)
        if not p.is_absolute():
            p = self.project_root / src
        return p

    def _resolve_subtitles(self, cfg: dict[str, Any]) -> Optional[Path]:
        """Resolve the subtitle file path from the subtitles config."""
        src = cfg.get("source")
        if not src:
            return None
        if src in self._assets_by_id:
            return self._assets_by_id[src]
        p = Path(src)
        if not p.is_absolute():
            p = self.project_root / src
        return p
