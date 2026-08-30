"""End-to-end orchestration:

    blueprint.json
        -> validate (Pydantic)
        -> parallel scene -> cut (ProcessPoolExecutor)
        -> audio_refs + asset staging
        -> (optional) HyperFramesCompose execute() render

The public surface is `run()`. The CLI wraps it in argparse. Tests
import it directly and call `run(..., render=False)` to exercise the
translation + workspace-assembly path without the npx round-trip.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .mapping import (
    AssetLookup,
    build_audio_refs,
    compute_timeline,
)
from .models import TargetBlueprint
from .render_adapter import (
    RenderOutcome,
    render_via_existing_tool,
    write_cuts_snapshot,
)
from .scene_workers import map_scenes_parallel

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    """Outcome of one transform + (optional) render pass."""

    project_id: str
    workspace: Path
    cuts_path: Path
    cut_count: int
    scene_count: int
    total_duration_seconds: float
    render: RenderOutcome | None = None
    timings: dict[str, float] = field(default_factory=dict)


def _load_blueprint(path: Path) -> TargetBlueprint:
    """Read & validate the blueprint. Pydantic gives us a structured failure."""
    import json

    raw = json.loads(path.read_text(encoding="utf-8"))
    blueprint = TargetBlueprint.model_validate(raw)
    log.info(
        "Loaded blueprint project_id=%s scenes=%d format=%dx%d@%dfps",
        blueprint.project_id,
        len(blueprint.scenes),
        blueprint.format.width,
        blueprint.format.height,
        blueprint.format.fps,
    )
    return blueprint


def _resolve_asset_lookup(
    blueprint_path: Path,
    explicit_assets_dir: Path | None,
) -> AssetLookup:
    """Build the asset id -> {local_path, label} lookup.

    Resolution rules (in order):
      1. `explicit_assets_dir` if the caller passed one (test fixture).
      2. `<blueprint_stem>_assets/` next to the blueprint file.
      3. `fixtures/assets/` relative to the package.
      4. Empty lookup — scenes that lack assets fall back to text cards.

    Each file's `asset_id` is derived from its filename stem
    (e.g. `bag-front.png` -> `bag-front`). The MVP doc's
    `crossborder_bag_video_mvp` example uses `asset_001`-style IDs, so
    this convention matches the test fixtures we'll author.
    """
    candidates: list[Path] = []
    if explicit_assets_dir is not None:
        candidates.append(explicit_assets_dir)
    candidates.append(blueprint_path.parent / f"{blueprint_path.stem}_assets")
    candidates.append(blueprint_path.parent / "assets")
    candidates.append(Path(__file__).resolve().parent.parent / "fixtures" / "assets")

    lookup: AssetLookup = {}
    for candidate in candidates:
        if not candidate.is_dir():
            continue
        for asset_path in sorted(candidate.iterdir()):
            if asset_path.is_dir():
                continue
            if asset_path.suffix.lower() not in {
                ".png",
                ".jpg",
                ".jpeg",
                ".webp",
                ".gif",
                ".mp4",
                ".mov",
                ".webm",
                ".mkv",
            }:
                continue
            asset_id = asset_path.stem
            lookup[asset_id] = {
                "asset_id": asset_id,
                "local_path": str(asset_path.resolve()),
                "label": asset_path.stem,
            }
        if lookup:
            log.info("Resolved %d assets from %s", len(lookup), candidate)
            return lookup
    log.warning(
        "No asset directory found in candidates %s — scenes will degrade to text cards.",
        [str(c) for c in candidates],
    )
    return lookup


def run(
    *,
    blueprint_path: Path,
    workspace_root: Path,
    assets_dir: Path | None = None,
    music_path: Path | None = None,
    workers: int | None = None,
    render: bool = True,
    quality: str = "draft",
) -> RunResult:
    """Translate + (optionally) render. Public entry point.

    Args:
        blueprint_path: target_blueprint.json path.
        workspace_root: where `<workspace_root>/projects/<project_id>/`
            will be created. Typically the prototype's `data/` dir; for
            integration with the main repo this becomes `projects/`.
        assets_dir: optional explicit asset directory (tests).
        music_path: optional background music file.
        workers: parallel worker count (None → auto).
        render: if True, invoke HyperFramesCompose at the end.
        quality: passed to HyperFramesCompose.

    Returns a RunResult. Raises on validation failure; never raises on
    render failure (that's surfaced via `result.render`).
    """
    import time

    timings: dict[str, float] = {}
    t0 = time.perf_counter()

    blueprint = _load_blueprint(blueprint_path)
    scenes = blueprint.sorted_scenes()
    asset_lookup = _resolve_asset_lookup(blueprint_path, assets_dir)
    timeline = compute_timeline(scenes)
    total_duration = timeline[-1][1] if timeline else 0.0
    timings["validate"] = time.perf_counter() - t0

    t1 = time.perf_counter()
    cuts = map_scenes_parallel(
        scenes, timeline, asset_lookup, workers=workers
    )
    timings["map_scenes"] = time.perf_counter() - t1

    t2 = time.perf_counter()
    audio_refs = build_audio_refs(scenes, timeline, music_path=str(music_path) if music_path else None)
    timings["audio_refs"] = time.perf_counter() - t2

    project_workspace = workspace_root / "projects" / blueprint.project_id
    project_workspace.mkdir(parents=True, exist_ok=True)

    cuts_path = write_cuts_snapshot(project_workspace, cuts, audio_refs)
    log.info("Wrote cuts snapshot to %s", cuts_path)

    render_outcome: RenderOutcome | None = None
    if render:
        t3 = time.perf_counter()
        render_outcome = render_via_existing_tool(
            project_id=blueprint.project_id,
            workspace_root=workspace_root,
            cuts=cuts,
            audio_refs_flat=audio_refs,
            asset_lookup=asset_lookup,
            width=blueprint.format.width,
            height=blueprint.format.height,
            fps=blueprint.format.fps,
            quality=quality,
        )
        timings["render"] = time.perf_counter() - t3
        if render_outcome.success:
            log.info("Render OK: %s", render_outcome.output_path)
        else:
            log.error(
                "Render failed: %s (runtime_check=%s)",
                render_outcome.error,
                render_outcome.runtime_check,
            )

    timings["total"] = time.perf_counter() - t0
    return RunResult(
        project_id=blueprint.project_id,
        workspace=project_workspace,
        cuts_path=cuts_path,
        cut_count=len(cuts),
        scene_count=len(scenes),
        total_duration_seconds=total_duration,
        render=render_outcome,
        timings=timings,
    )


def cleanup_workspace(workspace_root: Path, project_id: str) -> None:
    """Remove a project's workspace. Convenience for tests."""
    target = workspace_root / "projects" / project_id
    if target.exists():
        shutil.rmtree(target)


def result_to_dict(result: RunResult) -> dict[str, Any]:
    """Serialize for JSON / printing."""
    payload = asdict(result)
    if result.render is not None and result.render.output_path is not None:
        payload["render"]["output_path"] = str(result.render.output_path)
    if result.render is not None and result.render.workspace is not None:
        payload["render"]["workspace"] = str(result.render.workspace)
    payload["workspace"] = str(result.workspace)
    payload["cuts_path"] = str(result.cuts_path)
    return payload
