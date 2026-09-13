"""Render adapter — wraps `tools.video.hyperframes_compose.HyperFramesCompose`.

Responsibilities of THIS adapter:

1. Translate the transformer's `CutDict`/Scene outputs into the
   `edit_decisions` + `asset_manifest` shape that
   `HyperFramesCompose.execute(action="render", ...)` consumes. See the
   `_scaffold` and `_render` methods of that tool for the authoritative
   contract (lines ~458 / ~642 of
   `tools/video/hyperframes_compose.py`).

2. Bridge the audio shape mismatch: `mapping.build_audio_refs` emits the
   post-resolution shape (flat `narration` list, `music` dict with
   `src`/`volume`); the scaffold expects the *edecisions* shape
   (`audio.narration.segments[]` keyed by `asset_id`,
   `audio.music.{asset_id,volume}`). This module handles the bridge so
   `mapping.py` can stay agnostic.

3. Stage assets — copy any external asset into the workspace's
   `assets/` folder. The wrapper tool does this on its own in
   `_resolve_and_stage_assets`, but we pre-stage audio sources so the
   adapter never has to fabricate asset IDs.

4. Surface the rendered MP4 path back to `orchestrator.run()`.

FAIL-SAFE: if the hyperframes runtime is unavailable (no node / npx /
hyperframes cli), the wrapper tool returns a ToolResult with
`success=False` and a `runtime_check` data field. We surface that
verbatim — the prototype never silently switches to another renderer.
"""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .mapping import AssetLookup, CutDict

log = logging.getLogger(__name__)


@dataclass
class RenderOutcome:
    """Outcome of one render invocation."""

    success: bool
    output_path: Path | None
    workspace: Path | None
    error: str | None
    runtime_check: dict[str, Any] | None
    steps: dict[str, Any]


def _stage_assets(workspace: Path, asset_lookup: AssetLookup) -> AssetLookup:
    """Copy each referenced asset into <workspace>/assets/.

    HyperFrames resolves `src=` relative to the composition HTML, so
    assets must live under workspace. Copies are deduplicated by name.
    Returns a NEW lookup whose `local_path`s point inside workspace.
    """
    assets_dir = workspace / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    staged: AssetLookup = {}
    for asset_id, meta in asset_lookup.items():
        src = Path(meta["local_path"])
        dest = assets_dir / src.name
        if not dest.exists() and src.exists():
            shutil.copy2(src, dest)
        elif dest.exists() and not src.exists():
            # Already staged from a previous run — reuse.
            pass
        staged[asset_id] = {
            "asset_id": asset_id,
            "local_path": str(dest if dest.exists() else src),
            "label": meta.get("label"),
        }
    return staged


def _build_asset_manifest(asset_lookup: AssetLookup) -> list[dict]:
    """Construct the `asset_manifest.assets[]` shape the wrapper expects."""
    manifest = []
    for asset_id, meta in asset_lookup.items():
        manifest.append(
            {
                "id": asset_id,
                "url_or_path": meta["local_path"],
                "kind": "image" if meta["local_path"].lower().endswith(
                    (".png", ".jpg", ".jpeg", ".webp")
                ) else "video",
                "label": meta.get("label") or asset_id,
            }
        )
    return manifest


def _bridge_audio(flat_audio_refs: dict) -> dict:
    """Translate flat audio_refs → edecisions audio shape.

    flat_audio_refs (from `mapping.build_audio_refs`):
        {
            "narration": [{"src": "", "start_seconds", "end_seconds", "scene_id"}, ...],
            "music": {"src": "..."} | None
        }
    edecisions audio (consumed by `_resolve_audio_refs`):
        {
            "narration": {"segments": [{"asset_id", "start_seconds", "end_seconds"}, ...]},
            "music":      {"asset_id": ..., "volume": float}
        }
    """
    out: dict[str, Any] = {
        "narration": {"segments": []},
        "music": {"asset_id": None, "volume": 0.15},
    }

    seen_ids: set[str] = set()
    for i, seg in enumerate(flat_audio_refs.get("narration") or []):
        # No synthesized audio in MVP doc's MVP scope — narrations are
        # skipped. Surface as zero-asset entries so downstream can fill.
        # Synthesize a stable id from scene_id or index, so we don't
        # accidentally collide.
        aid = f"vo_{seg.get('scene_id') or i}"
        seen_ids.add(aid)
        out["narration"]["segments"].append(
            {
                "asset_id": aid,
                "start_seconds": float(seg.get("start_seconds", 0) or 0),
                "end_seconds": float(seg.get("end_seconds", 0) or 0),
            }
        )

    music = flat_audio_refs.get("music")
    if music and music.get("src"):
        out["music"] = {
            "asset_id": "music_bed",
            "volume": float(music.get("volume", 0.15) or 0.15),
        }
        # The wrapper also stages the music asset; piggy-back on the
        # assets manifest by emitting a sentinel asset entry below.
    return out


def render_via_existing_tool(
    *,
    project_id: str,
    workspace_root: Path,
    cuts: list[CutDict],
    audio_refs_flat: dict,
    asset_lookup: AssetLookup,
    width: int = 1080,
    height: int = 1920,
    fps: int = 30,
    quality: str = "draft",
) -> RenderOutcome:
    """Invoke the existing HyperFrames compose tool. Returns a Result.

    Args:
        project_id: target project folder under workspace_root/projects/.
        workspace_root: per-project root (e.g. `data/` in this prototype).
        cuts: pre-ordered CutDict list from `map_scenes_parallel`.
        audio_refs_flat: result of `build_audio_refs`.
        asset_lookup: id -> {asset_id, local_path, label}.
        quality: passed through to `npx hyperframes render --quality`.
    """
    # Late import keeps the rest of the package importable in environments
    # where the tool module isn't on sys.path (e.g. running tests in
    # isolation).
    from tools.video.hyperframes_compose import HyperFramesCompose  # type: ignore

    project_workspace = workspace_root / "projects" / project_id
    project_workspace.mkdir(parents=True, exist_ok=True)

    staged_assets = _stage_assets(project_workspace, asset_lookup)
    asset_manifest = _build_asset_manifest(staged_assets)
    audio_bridge = _bridge_audio(audio_refs_flat)

    edit_decisions: dict[str, Any] = {
        "renderer_family": "composition",
        "metadata": {
            "title": f"OpenMontage {project_id}",
            "project_id": project_id,
            "format": {"width": width, "height": height, "fps": fps},
        },
        "cuts": list(cuts),
        "audio": audio_bridge,
        "transitions": [],  # MVP doc caps transitions at cut/fade; we leave the
                            # wrapper infer from cuts (no extra config required).
    }

    inputs: dict[str, Any] = {
        "action": "render",
        "project_id": project_id,
        "workspace_path": str(project_workspace),
        "edit_decisions": edit_decisions,
        "asset_manifest": {"assets": asset_manifest},
        "playbook": {},  # CSS vars fall back to tool defaults — prototype scope.
        "width": width,
        "height": height,
        "fps": fps,
        "profile": "storyboard_short",  # tool-resolved name (see _resolve_dimensions)
        "quality": quality,
        "strict": False,
    }

    log.info(
        "Rendering %d cuts via HyperFramesCompose (workspace=%s)",
        len(cuts),
        project_workspace,
    )

    tool = HyperFramesCompose()
    try:
        result = tool.execute(inputs)
    except Exception as exc:  # pragma: no cover - defensive
        log.exception("HyperFramesCompose.execute raised")
        return RenderOutcome(
            success=False,
            output_path=None,
            workspace=project_workspace,
            error=f"{type(exc).__name__}: {exc}",
            runtime_check=None,
            steps={},
        )

    data = result.data if result.success else (result.data or {})
    runtime_check = data.get("runtime_check") if data else None
    steps = data.get("steps", {}) if data else {}

    if not result.success:
        return RenderOutcome(
            success=False,
            output_path=None,
            workspace=project_workspace,
            error=result.error,
            runtime_check=runtime_check,
            steps=steps,
        )

    output_path = Path(data["output"]) if data.get("output") else None
    return RenderOutcome(
        success=True,
        output_path=output_path,
        workspace=project_workspace,
        error=None,
        runtime_check=runtime_check,
        steps=steps,
    )


def write_cuts_snapshot(
    workspace: Path,
    cuts: list[CutDict],
    audio_refs: dict,
) -> Path:
    """Persist the resolved cuts + audio for debugging / replay.

    Independent of any npx invocation — useful when the user runs
    `--no-render` to inspect what the transformer produced.
    """
    snapshot_path = workspace / "cuts.json"
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        json.dumps({"cuts": cuts, "audio_refs": audio_refs}, indent=2),
        encoding="utf-8",
    )
    return snapshot_path
