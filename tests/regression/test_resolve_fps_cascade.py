"""Regression: edit_decisions FPS cascade — commit 9266752 fix(video_compose): from=NaN 全链路.

Before the fix, clients that emitted fps only at `metadata.fps` (or other legacy
locations) caused Remotion's Sequence to compute `from = seconds * undefined`
and crash with NaN. The fix introduces `_resolve_fps` which tries five cascade
locations and falls back to 30.0 with a WARNING when none qualify.

These tests pin the cascade contract so a future refactor can't silently
re-break it.
"""

from __future__ import annotations

import logging

import pytest

from tools.video.video_compose import _resolve_fps


# ---- happy path: every cascade location resolves correctly ----


def test_top_level_fps_wins():
    assert _resolve_fps({"fps": 24}) == 24.0


def test_compose_target_fps_used_when_no_top_level_fps():
    ed = {"compose_target": {"width": 1920, "height": 1080, "fps": 25}}
    assert _resolve_fps(ed) == 25.0


def test_format_fps_used_when_no_top_level_or_compose_target_fps():
    ed = {"format": {"fps": 50}}
    assert _resolve_fps(ed) == 50.0


def test_metadata_fps_used_when_no_top_level_shape_emits_it():
    # Pre-schema-fix legacy: clients still emit only `metadata.fps`.
    # Without this branch, fps would be undefined → Sequence from=NaN crash.
    ed = {"metadata": {"fps": 29.97}}
    assert _resolve_fps(ed) == 29.97


def test_metadata_compose_target_fps_used_as_last_resort():
    ed = {"metadata": {"compose_target": {"width": 720, "height": 1280, "fps": 60}}}
    assert _resolve_fps(ed) == 60.0


def test_priority_top_level_beats_metadata():
    # Top-level shape must win over the legacy metadata.* shape.
    ed = {"fps": 24, "metadata": {"fps": 60}}
    assert _resolve_fps(ed) == 24.0


def test_priority_compose_target_beats_metadata_compose_target():
    ed = {
        "compose_target": {"fps": 24},
        "metadata": {"compose_target": {"fps": 60}},
    }
    assert _resolve_fps(ed) == 24.0


# ---- fallback path: no candidate → 30.0 + warning (the NaN-safety net) ----


def test_falls_back_to_30_when_no_candidate(caplog):
    with caplog.at_level(logging.WARNING, logger="video_compose"):
        result = _resolve_fps({})
    assert result == 30.0
    assert any("fps missing" in m for m in caplog.messages)


def test_falls_back_to_30_when_all_candidates_are_zero_or_negative(caplog):
    with caplog.at_level(logging.WARNING, logger="video_compose"):
        result = _resolve_fps(
            {"fps": 0, "compose_target": {"fps": -1}, "metadata": {"fps": "not-a-number"}}
        )
    assert result == 30.0
    assert any("fps missing" in m for m in caplog.messages)


def test_falls_back_to_30_when_candidates_are_wrong_type(caplog):
    # Strings, None, dicts-as-fps must all be ignored — only positive numbers count.
    with caplog.at_level(logging.WARNING, logger="video_compose"):
        result = _resolve_fps({"fps": "30", "compose_target": {"fps": None}})
    assert result == 30.0


def test_falls_back_to_30_on_none_edit_decisions(caplog):
    with caplog.at_level(logging.WARNING, logger="video_compose"):
        result = _resolve_fps(None)
    assert result == 30.0


def test_falls_back_to_30_on_metadata_none(caplog):
    # metadata=None must not raise — the bug used to crash downstream in Remotion.
    with caplog.at_level(logging.WARNING, logger="video_compose"):
        result = _resolve_fps({"metadata": None})
    assert result == 30.0


# ---- regression: a specific client shape that triggered the original NaN ----


def test_real_world_bag_video_mvp_shape_does_not_nan():
    # The shape in docs/openmontage-173-server-fixes.md §P2: fps nested under
    # metadata.compose_target with no top-level fps anywhere.
    ed = {
        "metadata": {
            "compose_target": {"width": 1080, "height": 1920, "fps": 30}
        }
    }
    assert _resolve_fps(ed) == 30.0  # would have been NaN before the fix