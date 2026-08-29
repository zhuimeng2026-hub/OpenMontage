"""Regression: edit_decisions compose_target cascade — commit eedf74b fix(video_compose): respect edit_decisions.compose_target at top level.

Before the fix, `_resolve_compose_target` only consulted the legacy
`metadata.compose_target` location. Top-level `edit_decisions.compose_target`
(the schema-canonical location per schemas/artifacts/edit_decisions.schema.json)
was silently dropped, so vertical/portrait profiles via the top-level shape
would fall through to the 1920x1080 landscape default.

These tests pin the cascade contract so the top-level location stays honored.
"""

from __future__ import annotations

import pytest

from tools.video.video_compose import _resolve_compose_target


# ---- happy path: every cascade location resolves correctly ----


def test_top_level_compose_target_wins():
    ed = {"compose_target": {"width": 1080, "height": 1920, "fit": "cover"}}
    out = _resolve_compose_target(ed)
    assert out == {"width": 1080, "height": 1920, "fit": "cover"}


def test_top_level_format_used_when_no_top_level_compose_target():
    ed = {"format": {"width": 720, "height": 1280}}
    out = _resolve_compose_target(ed)
    assert out == {"width": 720, "height": 1280}


def test_metadata_compose_target_used_as_legacy_fallback():
    # Pre-schema-fix clients only emit metadata.compose_target.
    ed = {"metadata": {"compose_target": {"width": 1080, "height": 1920}}}
    out = _resolve_compose_target(ed)
    assert out == {"width": 1080, "height": 1920}


def test_metadata_format_used_as_oldest_fallback():
    ed = {"metadata": {"format": {"width": 1920, "height": 1080}}}
    out = _resolve_compose_target(ed)
    assert out == {"width": 1920, "height": 1080}


def test_priority_top_level_beats_metadata():
    # Top-level shape must win — that's the whole point of the eedf74b fix.
    ed = {
        "compose_target": {"width": 1080, "height": 1920, "fit": "cover"},
        "metadata": {"compose_target": {"width": 1920, "height": 1080}},
    }
    out = _resolve_compose_target(ed)
    assert out["width"] == 1080
    assert out["height"] == 1920


# ---- cascade stability: priority is first-present, not last-write ----


def test_cascade_priority_order_top_level_then_format_then_metadata():
    # Verify the documented order: top-level compose_target > top-level format
    # > metadata.compose_target > metadata.format.
    ed = {
        "metadata": {"format": {"width": 640, "height": 360}},  # last
        "compose_target": {"width": 1080, "height": 1920},  # first
    }
    out = _resolve_compose_target(ed)
    assert (out["width"], out["height"]) == (1080, 1920)


# ---- failure modes: no candidate or invalid candidate → None ----


def test_returns_none_when_no_candidate():
    assert _resolve_compose_target({}) is None


def test_returns_none_on_none_edit_decisions():
    assert _resolve_compose_target(None) is None


def test_returns_none_when_all_candidates_lack_positive_dims():
    # Dict present but missing/invalid width/height — must be skipped.
    ed = {
        "compose_target": {"fit": "cover"},  # no width/height
        "metadata": {"compose_target": {"width": 0, "height": 0}},
    }
    assert _resolve_compose_target(ed) is None


def test_returns_none_when_candidate_is_not_a_dict():
    # Top-level compose_target can be None or a string (legacy quirk).
    ed = {"compose_target": None, "metadata": {"compose_target": "720x1280"}}
    assert _resolve_compose_target(ed) is None


def test_returns_none_when_dims_are_wrong_type():
    ed = {"compose_target": {"width": "1080", "height": "1920"}}
    assert _resolve_compose_target(ed) is None


# ---- regression: the exact shape that triggered eedf74b ----


def test_bag_video_mvp_top_level_shape_does_not_silently_fall_through():
    # The MCP client (per commit eedf74b) emits compose_target at the top
    # level with no metadata wrapper. Before the fix this returned None and
    # the composition silently rendered at the 1920x1080 landscape default.
    ed = {"compose_target": {"width": 1080, "height": 1920, "fit": "cover"}}
    out = _resolve_compose_target(ed)
    assert out is not None
    assert (out["width"], out["height"]) == (1080, 1920)


def test_returned_dict_is_a_copy_not_a_reference():
    # The implementation uses `dict(c)` — verify so callers can't mutate the
    # cascade-internal dict via the returned reference.
    ct = {"width": 1080, "height": 1920}
    ed = {"compose_target": ct}
    out = _resolve_compose_target(ed)
    assert out is not ct
    out["width"] = 9999
    assert ct["width"] == 1080  # original untouched