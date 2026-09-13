"""HyperFrames Blueprint Transformer.

Internal R&D project. Converts OpenMontage MVP doc `target_blueprint.json`
into the `cuts: list[dict]` shape consumed by `tools/video/hyperframes_compose.py`,
then invokes that tool to render `final.mp4`.

The transformation itself is data-driven (pure functions over Pydantic models)
and parallelized per scene via `concurrent.futures.ProcessPoolExecutor`.
"""

from .models import (
    Format,
    Scene,
    SceneType,
    TargetBlueprint,
    Transition,
)
from .mapping import build_audio_refs, compute_timeline, scene_to_cut

__all__ = [
    "Format",
    "Scene",
    "SceneType",
    "TargetBlueprint",
    "Transition",
    "build_audio_refs",
    "compute_timeline",
    "scene_to_cut",
]
