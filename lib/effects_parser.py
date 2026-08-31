"""Natural-language effects → per-cut animation token parser.

Shared between ``mcp_server.create_remotion_video_share`` (templated branch)
and ``tools.video.video_compose._render`` (preview / non-session path) so
that an effects description drives the same animation vocabulary on both
the final render and the animatic / sample / render preview levels.

Contract
--------
``apply_effects_to_edit_decisions(edit_decisions, scene_plan, effects)``
mutates ``edit_decisions.cuts[i].transform.animation`` and
``scene_plan[i].shot_language.camera_movement`` for every cut/plan pair,
based on the keyword-scored segment for that index. When ``effects`` is
None/empty the function is a no-op and the caller is expected to fall back
to its own baseline (round-robin in mcp_server.py, last-mile defaults in
video_compose.py).

Tokens must match the switch in
``remotion-composer/src/Explainer.tsx::EnhancedVideoScene`` — adding a
new token here is a one-line change in three places (this file, Explainer
animation switch, and ``KNOWN_ANIMATION_TOKENS`` below for testability).

Why a shared module?
--------------------
The same parsing has to run whether the render was triggered via
``create_remotion_video_share`` (MCP session + per-cut construction) or
``video_compose(operation="render")`` (preview / studio-built edit_decisions).
Duplicating the parser would mean two places to drift — one of them would
eventually disagree with the user's text and the preview would show a
different animation than the final.
"""
from __future__ import annotations

from typing import Optional


# Animation tokens emitted by this parser. Each maps 1:1 to a branch in
# Explainer.tsx::EnhancedVideoScene. Adding a token here requires:
#   1. Adding the keyword tuples below.
#   2. Adding the case in Explainer.tsx.
#   3. Extending tests in tests/test_remotion_effects_and_subtitles.py.
KNOWN_ANIMATION_TOKENS = frozenset(
    {"zoom-in", "zoom-out", "pan-left", "pan-right", "ken-burns", "parallax"}
)


# Keyword → animation token map. Order matters: first match wins. Both
# Chinese and English variants are accepted because clawx-studio's 视频效果
# panel accepts either (see placeholder text in
# /opt/vclaw/openclaw/clawx-studio/src/App.vue:1522).
EFFECTS_KEYWORD_TO_ANIMATION = (
    (("zoom-in", "zoom in", "zoom_in", "推近", "放大", "拉近"), "zoom-in"),
    (("zoom-out", "zoom out", "zoom_out", "拉远", "缩小", "远离"), "zoom-out"),
    (("pan-left", "pan left", "pan_left", "左摇", "向左", "往左"), "pan-left"),
    (("pan-right", "pan right", "pan_right", "右摇", "向右", "往右"), "pan-right"),
    (("parallax", "视差", "视差滚动"), "parallax"),
    (("ken-burns", "kenburns", "ken burns", "电影感", "漂移", "缓慢", "缓推"), "ken-burns"),
)


def parse_effects_segments(effects: Optional[str]) -> list[str]:
    """Split free-text effects into ordered segments, one per cut intent.

    Splits on blank lines (paragraph) first, then newline; strips empties.
    Returns [] when effects is None / empty.
    """
    if not effects:
        return []
    text = effects.strip()
    if "\n\n" in text:
        segments = text.split("\n\n")
    else:
        segments = text.split("\n")
    return [s.strip() for s in segments if s.strip()]


def segment_animation(segment: str) -> str:
    """Pick the animation token that best matches a single effects segment.

    Best-effort keyword scan; first match wins. Falls back to 'zoom-in' when
    no keyword is recognised (so we always return a token Explainer.tsx knows).
    """
    lowered = segment.lower()
    for keywords, token in EFFECTS_KEYWORD_TO_ANIMATION:
        for kw in keywords:
            if kw in lowered:
                return token
    return "zoom-in"


def effects_animation_for_cut(
    effects: Optional[str], index: int, total: int
) -> tuple[str, str]:
    """Map an effects description to (animation, segment_for_this_cut).

    Contract:
      - When effects is None/empty: returns ("zoom-in", "") — the parser stays
        out of the way and the caller applies its own baseline (round-robin
        in mcp_server, last-mile defaults in video_compose).
      - When effects has N segments matching total: 1-to-1 assignment.
      - When fewer segments than cuts: cycles the parsed animations so the
        intent still propagates.
      - When more segments than cuts: keeps the first ``total`` (drop tail).
    """
    segments = parse_effects_segments(effects)
    if not segments:
        return "zoom-in", ""
    if total <= 0:
        return "zoom-in", segments[0]
    pick_index = min(index, len(segments) - 1) if index >= len(segments) else index
    segment = segments[pick_index]
    return segment_animation(segment), segment


def apply_effects_to_edit_decisions(
    edit_decisions: Optional[dict],
    scene_plan: Optional[list],
    effects: Optional[str],
) -> bool:
    """Rewrite per-cut animation tokens in edit_decisions + scene_plan from
    a free-text effects description.

    Returns True iff effects was non-empty AND the rewrite was applied
    (useful for callers that want to log/skip their own baseline when the
    parser took over).

    The mutator is side-effect-on-input for consistency with the existing
    pipeline pattern (``_ensure_governance_fields`` mutates in place).
    """
    if not effects:
        return False
    cuts = (edit_decisions or {}).get("cuts") or []
    if not cuts:
        return False
    total = len(cuts)
    for index, cut in enumerate(cuts):
        token, segment = effects_animation_for_cut(effects, index, total)
        transform = cut.setdefault("transform", {})
        if isinstance(transform, dict):
            transform["animation"] = token
            if segment:
                transform["effects"] = segment
    if scene_plan:
        for index, plan in enumerate(scene_plan):
            if index >= total:
                break
            token, segment = effects_animation_for_cut(effects, index, total)
            shot_lang = plan.setdefault("shot_language", {})
            if isinstance(shot_lang, dict):
                shot_lang["camera_movement"] = token
                if segment:
                    shot_lang["effects"] = segment
    return True