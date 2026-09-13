"""Unit tests for the pure scene → cut translation.

These cover every MVP doc §9 scene type:
    hook, pain_point, product_reveal, feature_demo,
    lifestyle, social_proof, offer, cta

Each test asserts:
    - the cut's `type` (text_card / image / video)
    - the cut's `text`/`subtitle` placement (headline vs voiceover)
    - the asset lookup is honored when present
    - fallback semantics when an asset is missing

These run in milliseconds — they're the regression net for any
mapping change.
"""

from __future__ import annotations

import pytest

from transformer.mapping import (
    AssetLookup,
    detect_cut_kind,
    scene_to_cut,
)
from transformer.models import Scene, SceneType


def _scene(scene_type: SceneType, **kwargs: object) -> Scene:
    """Build a Scene with sensible defaults so tests stay terse."""
    base: dict[str, object] = dict(
        id="s1",
        order=1,
        type=scene_type,
        duration=2.0,
        headline="H",
        voiceover="V",
    )
    base.update(kwargs)
    return Scene.model_validate(base)


def test_hook_without_asset_is_text_card() -> None:
    cut = scene_to_cut(_scene(SceneType.HOOK), 0.0, 2.0, {})
    assert cut["type"] == "text_card"
    assert cut["text"] == "H"
    assert "source" not in cut


def test_pain_point_uses_voiceover_as_text() -> None:
    cut = scene_to_cut(_scene(SceneType.PAIN_POINT), 0.0, 2.0, {})
    assert cut["type"] == "text_card"
    assert cut["text"] == "V"
    assert cut["subtitle"] == "H"


def test_product_reveal_with_image() -> None:
    lookup: AssetLookup = {
        "bag-front": {
            "asset_id": "bag-front",
            "local_path": "/abs/bag-front.png",
            "label": "front",
        }
    }
    cut = scene_to_cut(
        _scene(SceneType.PRODUCT_REVEAL, asset_id="bag-front"),
        0.0, 2.0, lookup,
    )
    assert cut["type"] == "image"
    assert cut["source"] == "/abs/bag-front.png"
    # Product reveal intentionally skips overlay text so the product shines.
    assert "text" not in cut


def test_feature_demo_with_image_renders_text_overlay() -> None:
    lookup: AssetLookup = {
        "bag-side": {
            "asset_id": "bag-side",
            "local_path": "/abs/bag-side.png",
            "label": "side",
        }
    }
    cut = scene_to_cut(
        _scene(SceneType.FEATURE_DEMO, asset_id="bag-side"),
        0.0, 2.0, lookup,
    )
    assert cut["type"] == "image"
    assert cut["source"] == "/abs/bag-side.png"
    assert cut["text"] == "H"


def test_lifestyle_with_video_extension() -> None:
    lookup: AssetLookup = {
        "lifestyle": {
            "asset_id": "lifestyle",
            "local_path": "/abs/lifestyle.mp4",
            "label": "lifestyle",
        }
    }
    cut = scene_to_cut(
        _scene(SceneType.LIFESTYLE, asset_id="lifestyle"),
        0.0, 2.0, lookup,
    )
    assert cut["type"] == "video"
    assert cut["source"] == "/abs/lifestyle.mp4"


def test_offer_is_text_card() -> None:
    cut = scene_to_cut(
        _scene(SceneType.OFFER, headline="$39.99 — Free Shipping"),
        0.0, 2.0, {},
    )
    assert cut["type"] == "text_card"
    assert cut["text"] == "$39.99 — Free Shipping"


def test_cta_is_text_card() -> None:
    cut = scene_to_cut(_scene(SceneType.CTA), 0.0, 2.0, {})
    assert cut["type"] == "text_card"
    assert cut["text"] == "H"


def test_social_proof_without_asset_falls_back_to_text_card() -> None:
    """Per MVP doc §9, social_proof → feature_demo (text card here)."""
    cut = scene_to_cut(
        _scene(SceneType.SOCIAL_PROOF, asset_id=None),
        0.0, 2.0, {},
    )
    assert cut["type"] == "text_card"
    assert cut["text"] == "H"
    assert cut["subtitle"] == "V"


def test_feature_demo_without_asset_falls_back_to_text_card() -> None:
    cut = scene_to_cut(
        _scene(SceneType.FEATURE_DEMO, asset_id=None),
        0.0, 2.0, {},
    )
    assert cut["type"] == "text_card"
    assert cut["text"] == "H"
    assert cut["subtitle"] == "V"


def test_missing_asset_id_falls_back_to_text_card() -> None:
    """If asset_id references something not in lookup, fall back gracefully."""
    cut = scene_to_cut(
        _scene(SceneType.LIFESTYLE, asset_id="missing"),
        0.0, 2.0, {},
    )
    assert cut["type"] == "text_card"


def test_detect_cut_kind_classifies_extensions() -> None:
    assert detect_cut_kind("a.png") == "image"
    assert detect_cut_kind("a.JPG") == "image"
    assert detect_cut_kind("a.mp4") == "video"
    assert detect_cut_kind("a.MOV") == "video"
    assert detect_cut_kind("") == "text_card"
    assert detect_cut_kind(None) == "text_card"  # type: ignore[arg-type]
    assert detect_cut_kind("a.pdf") == "text_card"


def test_timeline_is_cumulative() -> None:
    from transformer.mapping import compute_timeline

    s1 = _scene(SceneType.HOOK, duration=2.0)
    s2 = _scene(SceneType.PAIN_POINT, duration=3.0)
    s3 = _scene(SceneType.CTA, duration=1.5)
    pairs = compute_timeline([s1, s2, s3])
    assert pairs == [(0.0, 2.0), (2.0, 5.0), (5.0, 6.5)]


def test_empty_scenes_timeline() -> None:
    from transformer.mapping import compute_timeline

    assert compute_timeline([]) == []
