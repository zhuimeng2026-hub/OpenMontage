"""Multi-process scene → cut mapping.

The translation per scene is independent, so we fan out across a
`ProcessPoolExecutor`. Process over Thread:

* scene_to_cut is CPU-bound (string serialization + dict construction);
  no GIL contention to dodge, but no global state to share either.
* `ProcessPoolExecutor` keeps each worker hermetic — we don't have to
  worry about a leaky module-level dict contaminating another scene.
* The order of `executor.map()` preserves input order, so scene[i]
  always maps to cut[i] even though the workers run concurrently.

The asset lookup is picklable and small, so we pass it as an explicit
second arg instead of mutating a closure (which pickles by reference
and is fragile across spawn()).

Each worker emits one log line on completion so the test harness and
the CLI demo can show the user "this worker did these scenes".
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ProcessPoolExecutor

from .mapping import AssetLookup, CutDict, scene_to_cut
from .models import Scene

log = logging.getLogger(__name__)


def _worker(args: tuple[Scene, float, float, AssetLookup]) -> CutDict:
    scene, in_s, out_s, lookup = args
    cut = scene_to_cut(scene, in_s, out_s, lookup)
    log.info(
        "worker pid=%d mapped scene id=%s type=%s -> cut type=%s",
        os.getpid(),
        scene.id,
        scene.type.value,
        cut.get("type"),
    )
    return cut


def map_scenes_parallel(
    scenes: list[Scene],
    timeline: list[tuple[float, float]],
    asset_lookup: AssetLookup,
    *,
    workers: int | None = None,
) -> list[CutDict]:
    """Translate scenes → cuts in parallel, preserving scene order.

    Args:
        scenes: ordered list of Scene (must match `timeline` length).
        timeline: cumulative (in, out) seconds from `compute_timeline`.
        asset_lookup: picklable mapping id -> AssetMeta (path on disk).
        workers: parallel worker count. None → min(cpu_count(), 8).

    Returns:
        list of CutDict, same length and order as `scenes`.
    """
    if len(scenes) != len(timeline):
        raise ValueError(
            f"scenes ({len(scenes)}) and timeline ({len(timeline)}) lengths disagree"
        )
    if not scenes:
        return []

    payload: list[tuple[Scene, float, float, AssetLookup]] = [
        (scene, in_s, out_s, asset_lookup)
        for scene, (in_s, out_s) in zip(scenes, timeline, strict=True)
    ]

    worker_count = workers if workers is not None else min(os.cpu_count() or 2, 8)
    log.info(
        "Dispatching %d scenes across %d worker process(es)", len(scenes), worker_count
    )

    # `executor.map` preserves input order. Iterable length is at most a
    # few dozen scenes, so we don't bother chunking.
    with ProcessPoolExecutor(max_workers=worker_count) as ex:
        results = list(ex.map(_worker, payload))

    if len(results) != len(scenes):
        raise RuntimeError(
            f"worker pool returned {len(results)} cuts for {len(scenes)} scenes"
        )
    return list(results)
