"""Pydantic schema tests — verify strict validation against MVP doc §11.

The MVP acceptance flow (§40) lists at least 5 scenes (Hook / Reveal /
Feature / Feature / CTA). The fixture ships 6 to cover the social_proof
fallback and offer types in addition. Strict schema means a typo in a
field name will fail loudly here instead of silently corrupting cuts
later in the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from transformer.models import (
    Format,
    Scene,
    SceneType,
    TargetBlueprint,
    Transition,
)

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "target_blueprint.example.json"


def test_fixture_loads() -> None:
    raw = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    blueprint = TargetBlueprint.model_validate(raw)
    assert blueprint.project_id == "demo_proj"
    assert blueprint.format.width == 1080
    assert blueprint.format.height == 1920
    assert blueprint.format.fps == 30
    assert len(blueprint.scenes) == 6
    types_in_order = [s.type for s in blueprint.sorted_scenes()]
    assert types_in_order == [
        SceneType.HOOK,
        SceneType.PAIN_POINT,
        SceneType.PRODUCT_REVEAL,
        SceneType.FEATURE_DEMO,
        SceneType.LIFESTYLE,
        SceneType.CTA,
    ]


def test_format_is_strict() -> None:
    with pytest.raises(ValidationError):
        Format.model_validate({"width": 1920, "height": 1080, "fps": 30})


def test_unknown_scene_type_rejected() -> None:
    payload = {
        "project_id": "p",
        "format": {"width": 1080, "height": 1920, "fps": 30},
        "scenes": [
            {
                "id": "s1",
                "order": 1,
                "type": "explainer_overlay",  # not in whitelist
                "duration": 2.0,
                "headline": "h",
                "voiceover": "v",
            }
        ],
    }
    with pytest.raises(ValidationError) as exc_info:
        TargetBlueprint.model_validate(payload)
    # Pydantic surfaces the enum choice field clearly.
    assert any("type" in err["loc"] for err in exc_info.value.errors())


def test_negative_duration_rejected() -> None:
    payload = {
        "project_id": "p",
        "format": {"width": 1080, "height": 1920, "fps": 30},
        "scenes": [
            {
                "id": "s1",
                "order": 1,
                "type": "hook",
                "duration": 0.0,
                "headline": "h",
                "voiceover": "v",
            }
        ],
    }
    with pytest.raises(ValidationError):
        TargetBlueprint.model_validate(payload)


def test_unknown_transition_rejected() -> None:
    payload = {
        "id": "s1",
        "order": 1,
        "type": "hook",
        "duration": 2.0,
        "headline": "h",
        "voiceover": "v",
        "transition": "swipe",  # only cut / fade allowed
    }
    with pytest.raises(ValidationError):
        Scene.model_validate(payload)


def test_sorted_scenes_respects_order_field() -> None:
    payload = {
        "project_id": "p",
        "format": {"width": 1080, "height": 1920, "fps": 30},
        "scenes": [
            {"id": "c", "order": 3, "type": "cta", "duration": 1.0, "headline": "c", "voiceover": "c"},
            {"id": "a", "order": 1, "type": "hook", "duration": 1.0, "headline": "a", "voiceover": "a"},
            {"id": "b", "order": 2, "type": "pain_point", "duration": 1.0, "headline": "b", "voiceover": "b"},
        ],
    }
    blueprint = TargetBlueprint.model_validate(payload)
    assert [s.id for s in blueprint.sorted_scenes()] == ["a", "b", "c"]


def test_extra_fields_rejected() -> None:
    payload = {
        "project_id": "p",
        "render_runtime": "remotion",  # ← would leak from MVP doc
        "format": {"width": 1080, "height": 1920, "fps": 30},
        "scenes": [
            {
                "id": "s1", "order": 1, "type": "hook",
                "duration": 1.0, "headline": "h", "voiceover": "v",
                "color_grade": "warm",  # ← stray
            }
        ],
    }
    with pytest.raises(ValidationError):
        TargetBlueprint.model_validate(payload)


def test_transition_defaults_to_cut() -> None:
    scene = Scene.model_validate(
        {
            "id": "s1", "order": 1, "type": "hook",
            "duration": 1.0, "headline": "h", "voiceover": "v",
        }
    )
    assert scene.transition is Transition.CUT
