"""Tests for the custom-composition props contract normalization.

MCP emits snake_case edit_decisions (custom_code / duration_per_image); the
runtime-compiled CustomComposition reads camelCase props (code /
durationPerImage). _normalize_custom_composition_props bridges the two so the
user-authored TSX actually receives its inputs instead of an empty `code`.
"""

from tools.video.video_compose import VideoCompose


def test_maps_custom_code_to_code():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props(
        {"custom_code": "export const MyComposition = () => null;"},
        [],
    )
    assert out["code"] == "export const MyComposition = () => null;"
    # snake_case key must not leak into the component-facing props
    assert "custom_code" not in out


def test_maps_duration_per_image_to_duration_per_image():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props(
        {"custom_code": "x", "duration_per_image": 5},
        ["a.png"],
    )
    assert out["durationPerImage"] == 5


def test_duration_per_image_defaults_to_three():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props({"custom_code": "x"}, [])
    assert out["durationPerImage"] == 3


def test_forwards_fps_and_dimensions_from_compose_target():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props(
        {
            "custom_code": "x",
            "metadata": {"compose_target": {"width": 1920, "height": 1080}},
        },
        [],
    )
    assert out["fps"] == 30
    assert out["width"] == 1920
    assert out["height"] == 1080


def test_dimensions_default_when_compose_target_missing():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props({"custom_code": "x"}, [])
    assert out["width"] == 1080
    assert out["height"] == 1920


def test_images_passed_through_and_routing_key_set():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props(
        {"custom_code": "x"},
        ["_staged/abc/0.png", "_staged/abc/1.png"],
    )
    assert out["images"] == ["_staged/abc/0.png", "_staged/abc/1.png"]
    assert out["renderer_family"] == "custom-composition"


def test_empty_code_is_preserved_not_silently_filled():
    vc = VideoCompose()
    out = vc._normalize_custom_composition_props({}, [])
    assert out["code"] == ""
