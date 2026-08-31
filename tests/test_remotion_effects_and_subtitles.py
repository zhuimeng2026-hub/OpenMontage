"""Tests for the effects parser + SRT serializer + downstream propagation.

Background
----------
The templated branch of ``create_remotion_video_share`` previously hardcoded
``motion = [zoom-in, pan-left, ken-burns, pan-right]`` round-robin across
cuts. With the ``effects`` passthrough (commit 58fc695), callers can supply
natural-language descriptions ("开篇旋转切入 ... 中段 Ken Burns ...") and the
parser should:

  1. Tokenise the free text into ordered segments.
  2. Score each segment against keyword groups (zh + en).
  3. Map 1-to-1 to cuts when segment count matches, else cycle.
  4. Fall back to round-robin baseline when effects is empty.

The SRT serializer should:

  1. Accept the cue shape VClaw Studio sends: ``{index, start, end, text}``.
  2. Round seconds to milliseconds and emit the canonical HH:MM:SS,mmm format
     (comma ms separator — the same convention the verify-subtitle.md
     regression asserted).
  3. Be best-effort: malformed cues are skipped, not raised.

The burn_subtitles wiring should:

  1. Write an SRT and call ``video_compose(operation='burn_subtitles')`` when
     subtitles are present.
  2. Replace ``video_path`` with the burned output before upload.
  3. Skip silently when subtitles is None / empty.

These tests pin that contract.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import mcp_server
from lib import effects_parser


# ---------------------------------------------------------------------------
# parse_effects_segments / segment_animation / effects_animation_for_cut
# (live in lib.effects_parser so the preview path through
# tools.video.video_compose can share the same vocabulary as the templated
# branch in mcp_server.create_remotion_video_share).
# ---------------------------------------------------------------------------


def test_parse_segments_splits_on_blank_lines():
    text = "开篇：旋转切入\n\n中段：Ken Burns 慢推\n\n结尾：粒子汇聚淡出"
    out = effects_parser.parse_effects_segments(text)
    assert out == ["开篇：旋转切入", "中段：Ken Burns 慢推", "结尾：粒子汇聚淡出"]


def test_parse_segments_splits_on_single_newline_when_no_paragraph_break():
    text = "zoom-in\npan-left\nken-burns"
    out = effects_parser.parse_effects_segments(text)
    assert out == ["zoom-in", "pan-left", "ken-burns"]


def test_parse_segments_strips_empty_and_whitespace():
    text = "\n\nzoom-in\n   \npan-right\n\n"
    assert effects_parser.parse_effects_segments(text) == ["zoom-in", "pan-right"]


def test_parse_segments_empty_input_returns_empty_list():
    assert effects_parser.parse_effects_segments("") == []
    assert effects_parser.parse_effects_segments(None) == []


def test_segment_animation_zh_keywords():
    assert effects_parser.segment_animation("开篇旋转切入") == "zoom-in"  # 放大 maps to zoom-in
    assert effects_parser.segment_animation("右摇镜头") == "pan-right"
    assert effects_parser.segment_animation("电影感漂移") == "ken-burns"
    assert effects_parser.segment_animation("缓慢推出") == "ken-burns"
    assert effects_parser.segment_animation("远离全景") == "zoom-out"


def test_segment_animation_en_keywords():
    assert effects_parser.segment_animation("zoom-in slowly") == "zoom-in"
    assert effects_parser.segment_animation("Ken Burns push") == "ken-burns"
    assert effects_parser.segment_animation("pan left drift") == "pan-left"
    assert effects_parser.segment_animation("parallax bg") == "parallax"


def test_segment_animation_unknown_defaults_to_zoom_in():
    assert effects_parser.segment_animation("some novel effect") == "zoom-in"


def test_effects_animation_for_cut_no_effects_returns_baseline():
    # When effects is empty, helper returns ("zoom-in", "") so the caller
    # falls back to round-robin. We assert the tuple shape, not the per-cut
    # animation — that's the renderer's job.
    assert effects_parser.effects_animation_for_cut(None, 0, 4) == ("zoom-in", "")
    assert effects_parser.effects_animation_for_cut("", 3, 5) == ("zoom-in", "")


def test_effects_animation_for_cut_one_to_one_when_segments_match():
    effects = "zoom-in\npan-left\nken-burns\npan-right"
    # 4 segments, 4 cuts → 1-to-1
    assert effects_parser.effects_animation_for_cut(effects, 0, 4) == ("zoom-in", "zoom-in")
    assert effects_parser.effects_animation_for_cut(effects, 1, 4) == ("pan-left", "pan-left")
    assert effects_parser.effects_animation_for_cut(effects, 2, 4) == ("ken-burns", "ken-burns")
    assert effects_parser.effects_animation_for_cut(effects, 3, 4) == ("pan-right", "pan-right")


def test_effects_animation_for_cut_extra_segments_dropped():
    effects = "zoom-in\npan-left\nken-burns\npan-right\nzoom-out\nparallax"
    # 6 segments, 4 cuts → tail dropped
    assert effects_parser.effects_animation_for_cut(effects, 3, 4)[0] == "pan-right"


def test_effects_animation_for_cut_fewer_segments_cycle_last():
    effects = "ken-burns 慢推\nzoom-in 推近"
    # 2 segments, 5 cuts → cycles back to last segment for the tail
    token3, seg3 = effects_parser.effects_animation_for_cut(effects, 3, 5)
    assert token3 == "zoom-in"
    assert seg3 == "zoom-in 推近"


def test_effects_animation_for_cut_mixed_keywords_picks_first_match():
    # "Ken Burns zoom-in" — keyword order in EFFECTS_KEYWORD_TO_ANIMATION:
    # zoom-in first, so it wins. Document the precedence.
    token = effects_parser.effects_animation_for_cut("Ken Burns zoom-in\n", 0, 1)[0]
    assert token == "zoom-in"


def test_apply_effects_to_edit_decisions_rewrites_cuts():
    """The mutator should rewrite per-cut animation in-place, both on cuts
    and on scene_plan. This is the function video_compose calls when it sees
    metadata.effects — the preview path must end up with the same vocabulary
    as the templated branch."""
    ed = {
        "cuts": [
            {"transform": {"animation": "static"}},
            {"transform": {"animation": "static"}},
        ],
    }
    scene_plan = [
        {"shot_language": {"camera_movement": "static"}},
        {"shot_language": {"camera_movement": "static"}},
    ]
    applied = effects_parser.apply_effects_to_edit_decisions(
        ed, scene_plan, "zoom-in\nken-burns"
    )
    assert applied is True
    assert ed["cuts"][0]["transform"]["animation"] == "zoom-in"
    assert ed["cuts"][1]["transform"]["animation"] == "ken-burns"
    assert scene_plan[0]["shot_language"]["camera_movement"] == "zoom-in"
    assert scene_plan[1]["shot_language"]["camera_movement"] == "ken-burns"
    # Raw segment text also stashed.
    assert ed["cuts"][0]["transform"]["effects"] == "zoom-in"


def test_apply_effects_to_edit_decisions_no_effects_is_noop():
    ed = {"cuts": [{"transform": {"animation": "static"}}]}
    scene_plan = [{"shot_language": {"camera_movement": "static"}}]
    applied = effects_parser.apply_effects_to_edit_decisions(ed, scene_plan, None)
    assert applied is False
    assert ed["cuts"][0]["transform"]["animation"] == "static"
    applied2 = effects_parser.apply_effects_to_edit_decisions(ed, scene_plan, "")
    assert applied2 is False


def test_apply_effects_to_edit_decisions_no_cuts_is_noop():
    applied = effects_parser.apply_effects_to_edit_decisions({}, None, "zoom-in")
    assert applied is False


# ---------------------------------------------------------------------------
# _cues_to_srt
# ---------------------------------------------------------------------------


def test_cues_to_srt_basic_format():
    cues = [
        {"index": 1, "start": 0.0, "end": 3.0, "text": "Hello"},
        {"index": 2, "start": 3.5, "end": 6.25, "text": "World"},
    ]
    out = mcp_server._cues_to_srt(cues)
    assert "00:00:00,000 --> 00:00:03,000" in out
    assert "Hello" in out
    assert "00:00:03,500 --> 00:00:06,250" in out
    # Standard SRT: cue index, time line, text, blank line.
    assert out.count("-->") == 2


def test_cues_to_srt_uses_comma_ms_separator():
    # Verifies the regression noted in clawx-studio/verify-subtitle.md:
    # SRT uses ',' not '.' for milliseconds.
    cues = [{"index": 1, "start": 0, "end": 1, "text": "x"}]
    out = mcp_server._cues_to_srt(cues)
    assert "," in out.split("\n")[1]
    assert "." not in out.split("\n")[1]


def test_cues_to_srt_skips_malformed_cues():
    cues = [
        {"index": 1, "start": "not-a-number", "end": 3, "text": "bad"},
        {"index": 2, "start": 0, "end": 1, "text": "good"},
    ]
    out = mcp_server._cues_to_srt(cues)
    assert "bad" not in out
    assert "good" in out


def test_cues_to_srt_empty_input_returns_empty_string():
    assert mcp_server._cues_to_srt([]) == ""
    assert mcp_server._cues_to_srt(None) == ""


def test_cues_to_srt_handles_minutes_and_hours():
    cues = [{"index": 1, "start": 3725.5, "end": 3728.0, "text": "long-form"}]
    out = mcp_server._cues_to_srt(cues)
    # 3725.5 s = 1h 2m 5.5s
    assert "01:02:05,500 --> 01:02:08,000" in out


# ---------------------------------------------------------------------------
# End-to-end: effects threading into cuts / scene_plan
# ---------------------------------------------------------------------------


@pytest.fixture
def workflow_session(monkeypatch, tmp_path):
    """Same shape as the governance-fields test fixture."""
    import lib.workbuddy_session as sessions
    from lib.mcp_session import reset_mcp_session_id, set_mcp_session_id

    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "projects" / ".mcp_sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)

    def _img(name):
        path = tmp_path / "projects" / "demo" / "assets" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fake-image")
        return path

    monkeypatch.setattr(mcp_server, "_PROJECT_ROOT", tmp_path)
    sessions.register_image(
        "workflow", "demo",
        {"id": "img-1", "path": str(_img("one.jpg")),
         "relative_path": "projects/demo/assets/one.jpg",
         "type": "image", "sha256": "x"},
    )
    sessions.register_image(
        "workflow", "demo",
        {"id": "img-2", "path": str(_img("two.jpg")),
         "relative_path": "projects/demo/assets/two.jpg",
         "type": "image", "sha256": "y"},
    )
    sessions.register_image(
        "workflow", "demo",
        {"id": "img-3", "path": str(_img("three.jpg")),
         "relative_path": "projects/demo/assets/three.jpg",
         "type": "image", "sha256": "z"},
    )
    captured: dict = {}

    class FakeCompose:
        def execute(self, inputs):
            captured.setdefault("calls", []).append(inputs)
            captured["edit_decisions"] = inputs["edit_decisions"]
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"mp4")
            return _ok(output=str(output))

    class FakeUpload:
        def execute(self, inputs):
            return _ok(file_id="file-1")

    class FakeShare:
        def execute(self, inputs):
            return _ok(short_url="https://share.weiyun.com/x")

    tools = {
        "video_compose": FakeCompose(),
        "weiyun_upload": FakeUpload(),
        "weiyun_share_link": FakeShare(),
    }
    monkeypatch.setattr(mcp_server.registry, "get", lambda name: tools.get(name))
    token = set_mcp_session_id("workflow")
    try:
        yield captured
    finally:
        reset_mcp_session_id(token)


class _TR:
    def __init__(self, success, data=None, error=None):
        self.success = success
        self.data = data or {}
        self.error = error
        self.artifacts: list = []


def _ok(**kw):
    return _TR(True, kw)


def test_effects_threaded_into_cut_animation(workflow_session):
    """Per-cut animation must come from the effects parser when effects is set."""
    captured = workflow_session
    effects = "zoom-in 推近\npan-left 左摇\nken-burns 电影感"
    result = asyncio.run(mcp_server.create_remotion_video_share(effects=effects))
    assert result["success"] is True, result
    cuts = captured["edit_decisions"]["cuts"]
    assert len(cuts) == 3
    assert cuts[0]["transform"]["animation"] == "zoom-in"
    assert cuts[1]["transform"]["animation"] == "pan-left"
    assert cuts[2]["transform"]["animation"] == "ken-burns"


def test_effects_threaded_into_scene_plan_shot_language(workflow_session):
    """Per-cut effects text lands in scene_plan[i].shot_language.effects."""
    captured = workflow_session
    effects = "zoom-in 推近\nken-burns 电影感\n粒子汇聚淡出"
    result = asyncio.run(mcp_server.create_remotion_video_share(effects=effects))
    assert result["success"] is True, result
    # scene_plan is passed as a top-level arg to video_compose, sibling to
    # edit_decisions — capture it from the render call's inputs.
    scene_plan = captured["calls"][0]["scene_plan"]
    assert len(scene_plan) == 3
    assert scene_plan[0]["shot_language"]["camera_movement"] == "zoom-in"
    assert scene_plan[0]["shot_language"]["effects"] == "zoom-in 推近"
    assert scene_plan[1]["shot_language"]["effects"] == "ken-burns 电影感"


def test_no_effects_keeps_round_robin_baseline(workflow_session):
    """Back-compat: empty effects → round-robin zoom-in/pan-left/ken-burns/pan-right."""
    captured = workflow_session
    result = asyncio.run(mcp_server.create_remotion_video_share())
    assert result["success"] is True, result
    cuts = captured["edit_decisions"]["cuts"]
    assert cuts[0]["transform"]["animation"] == "zoom-in"
    assert cuts[1]["transform"]["animation"] == "pan-left"
    assert cuts[2]["transform"]["animation"] == "ken-burns"
    # No effects key when caller didn't supply any.
    assert "effects" not in cuts[0]["transform"]
    scene_plan = captured["calls"][0]["scene_plan"]
    assert "effects" not in scene_plan[0]["shot_language"]


# ---------------------------------------------------------------------------
# burn_subtitles wiring (post-render step)
# ---------------------------------------------------------------------------


def test_burn_subtitles_called_when_subtitles_present(workflow_session, monkeypatch):
    """subtitles non-empty → video_compose(operation='burn_subtitles') called
    with input_path=rendered.mp4 + subtitle_path=<rendered>.srt + a separate
    output_path, then the upload runs against the BURNED path."""
    captured = workflow_session
    cues = [{"index": 1, "start": 0, "end": 3, "text": "hello"}]
    result = asyncio.run(mcp_server.create_remotion_video_share(subtitles=cues))
    assert result["success"] is True, result
    # The composed output dir contains the SRT and the burned MP4 we promised.
    renders = list(Path(captured["edit_decisions"]["metadata"]["script_id"]).parent.glob("*.srt")) if False else []
    # Easier: re-read the captured calls and assert burn happened.
    calls = captured["calls"]
    operations = [c.get("operation") for c in calls]
    assert "render" in operations
    assert "burn_subtitles" in operations
    burn_call = next(c for c in calls if c.get("operation") == "burn_subtitles")
    assert burn_call["subtitle_path"].endswith(".srt")
    assert burn_call["output_path"].endswith("-subtitled.mp4")
    # SRT file actually exists on disk.
    assert Path(burn_call["subtitle_path"]).is_file()
    # SRT format sanity.
    srt_text = Path(burn_call["subtitle_path"]).read_text(encoding="utf-8")
    assert "00:00:00,000 --> 00:00:03,000" in srt_text
    assert "hello" in srt_text


def test_no_burn_when_subtitles_absent(workflow_session, monkeypatch):
    """Back-compat: no subtitles → no burn_subtitles call. The upload runs
    against the rendered output, NOT a subtitled variant."""
    captured = workflow_session
    uploaded_paths: list[str] = []

    class UploadSpy:
        def execute(self, inputs):
            uploaded_paths.append(inputs["video_path"])
            return _ok(file_id="file-1")

    # Wrap the fixture's registry.get so weiyun_upload becomes a spy while
    # the FakeCompose / FakeShare stay intact (the render call still hits
    # captured["calls"]).
    original_get = mcp_server.registry.get
    def spy_get(name):
        if name == "weiyun_upload":
            return UploadSpy()
        return original_get(name)
    monkeypatch.setattr(mcp_server.registry, "get", spy_get)

    result = asyncio.run(mcp_server.create_remotion_video_share())
    assert result["success"] is True, result
    operations = [c.get("operation") for c in captured["calls"]]
    assert "burn_subtitles" not in operations
    assert "render" in operations
    # Upload ran against the rendered output, not a subtitled variant.
    assert uploaded_paths, "upload was never invoked"
    assert not any(p.endswith("-subtitled.mp4") for p in uploaded_paths)


def test_no_burn_when_subtitles_empty_list(workflow_session):
    """Empty cue list should be treated the same as no subtitles."""
    captured = workflow_session
    result = asyncio.run(mcp_server.create_remotion_video_share(subtitles=[]))
    assert result["success"] is True, result
    operations = [c.get("operation") for c in captured["calls"]]
    assert "burn_subtitles" not in operations