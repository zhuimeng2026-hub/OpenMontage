"""Multi-process worker pool tests.

What we verify:
    1. N scenes in -> N cuts out.
    2. Order is preserved (executor.map contract).
    3. Each cut picks up the correct timeline slot.
    4. The worker count knob is honored.

We always run with `workers=2` to force process-based dispatch — a
`ThreadPoolExecutor` would be hard to distinguish from `Process` if
workers=1 because the GIL masks the difference.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from transformer.mapping import compute_timeline
from transformer.models import Scene, SceneType
from transformer.scene_workers import map_scenes_parallel


def _scenes(n: int) -> list[Scene]:
    return [
        Scene.model_validate(
            dict(
                id=f"scene_{i:02d}",
                order=i,
                type=SceneType.HOOK,
                duration=1.0,
                headline=f"H{i}",
                voiceover=f"V{i}",
            )
        )
        for i in range(1, n + 1)
    ]


def test_workers_preserves_order() -> None:
    """The MVP doc contract has explicit scene order — never shuffle them."""
    scenes = _scenes(8)
    timeline = compute_timeline(scenes)
    cuts = map_scenes_parallel(scenes, timeline, asset_lookup={}, workers=2)
    assert len(cuts) == len(scenes)
    # in_seconds should be strictly increasing in 1.0 steps.
    in_secs = [c["in_seconds"] for c in cuts]
    assert in_secs == [float(i) for i in range(len(scenes))]


def test_zero_scenes_returns_empty() -> None:
    assert map_scenes_parallel([], [], {}, workers=2) == []


def test_workers_count_honored() -> None:
    """Smoke test that workers knob is accepted. Real wall-clock
    measurement would be flaky; this just ensures the surface doesn't
    crash for workers=1 (sequential, single process)."""
    scenes = _scenes(3)
    timeline = compute_timeline(scenes)
    cuts = map_scenes_parallel(scenes, timeline, {}, workers=1)
    assert len(cuts) == 3


def test_timeline_length_mismatch_raises() -> None:
    scenes = _scenes(4)
    bad_timeline = compute_timeline(scenes)[:3]
    with pytest.raises(ValueError, match="lengths disagree"):
        map_scenes_parallel(scenes, bad_timeline, {}, workers=2)


def test_workers_process_pids_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Each worker logs its pid + the scene it handled. Useful for the
    CLI demo's proof-of-parallelism output."""
    import logging

    scenes = _scenes(4)
    timeline = compute_timeline(scenes)
    with caplog.at_level(logging.INFO, logger="transformer.scene_workers"):
        cuts = map_scenes_parallel(scenes, timeline, {}, workers=2)
    assert len(cuts) == 4
    worker_logs = [r for r in caplog.records if "worker pid" in r.getMessage()]
    assert len(worker_logs) == 4, "expected one log line per scene"


def test_assets_propagate_to_all_scenes() -> None:
    """The asset lookup must be visible in every worker. Regression
    test against accidental closure capture."""
    lookup = {
        "a1": {"asset_id": "a1", "local_path": "/x/a1.png", "label": "a1"},
        "a2": {"asset_id": "a2", "local_path": "/x/a2.mp4", "label": "a2"},
    }
    scenes = [
        Scene.model_validate(
            dict(
                id=f"scene_{i:02d}",
                order=i,
                type=SceneType.LIFESTYLE,
                duration=1.0,
                headline=f"H{i}",
                voiceover=f"V{i}",
                asset_id="a1" if i % 2 else "a2",
            )
        )
        for i in range(1, 5)
    ]
    timeline = compute_timeline(scenes)
    cuts = map_scenes_parallel(scenes, timeline, lookup, workers=2)
    for cut in cuts:
        assert cut["type"] in ("image", "video")
        assert cut["source"].startswith("/x/")
