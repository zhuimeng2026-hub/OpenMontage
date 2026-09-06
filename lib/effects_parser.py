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
``remotion-composer/src/Explainer.tsx::ImageScene`` (function declared at
``Explainer.tsx:349``; animation if/else chain at `:377-425`) — adding a
new token here is a one-line change in three places (this file, ImageScene
animation switch, and ``KNOWN_ANIMATION_TOKENS`` below for testability).

When ``effects`` parses a numeric range (rotation/zoom amplitude/fade duration),
``apply_effects_to_edit_decisions`` mirrors it as ``transform.animation_params``
and ``shot_language.animation_params`` so downstream Remotion templates can drive
exact values rather than token-only defaults.  See
``docs/remotion-effects-template-implementation-2026-08-31.md`` §3.1.

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

import re

from typing import Optional


# Animation tokens emitted by this parser. Each maps 1:1 to a branch in
# Explainer.tsx::ImageScene (the if/else chain at :377-425). Adding a token here requires:
#   1. Adding the keyword tuples below.
#   2. Adding the case in ImageScene.
#   3. Extending tests in tests/test_remotion_effects_and_subtitles.py.
KNOWN_ANIMATION_TOKENS = frozenset(
    {"zoom-in", "zoom-out", "pan-left", "pan-right", "ken-burns", "parallax",
     "rotate", "fade-in", "fade-out"}
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
    (("rotate", "rotation", "旋转", "转场旋转", "360"), "rotate"),
    (("fade in", "fade-in", "淡入"), "fade-in"),
    (("fade out", "fade-out", "淡出"), "fade-out"),
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


def extract_animation_params(segment: str) -> dict:
    """Extract structured animation parameters from a free-text effects segment.

    Recognised (case-insensitive) numeric patterns:
      - ``rotation`` / ``rotate`` + ``from<°>[-→=]<to><°>`` → ``{"rotate": [from, to]}``
        Bare ``rotation`` / ``rotate`` / ``360`` with no range → ``{"rotate": [0, 360]}``.
      - ``zoom <from>x? [-→=] <to>x?`` → ``{"scale": [from, to]}`` (floats).
      - ``fade[- ]?in`` + optional ``<sec>s`` → ``{"fade_in": sec}`` (defaults to 0.5).
      - ``fade[- ]?out`` + optional ``<sec>s`` → ``{"fade_out": sec}`` (defaults to 0.5).

    Multiple parameters in the same segment are merged into one dict.

    Contract: this function NEVER raises. Any regex failure, empty input, or
    non-string value returns ``{}`` so callers (Remotion template bridge) can
    fall back to token-only defaults instead of crashing the render.
    """
    if not segment or not isinstance(segment, str):
        return {}
    try:
        text = segment
        result: dict = {}

        # Rotation: prefer an explicit range; fall back to [0, 360] when the
        # rotation/rotate keyword is mentioned but no range is given. Both the
        # "-", "→", "=" single-char separators and the common "->" / "→" arrow
        # forms are accepted.
        rot_range = re.search(
            r"rotat(?:e|ion)\s*(\d+(?:\.\d+)?)\s*[-→=]+>?\s*(\d+(?:\.\d+)?)\s*deg",
            text,
            re.IGNORECASE,
        )
        if rot_range:
            result["rotate"] = [float(rot_range.group(1)), float(rot_range.group(2))]
        elif re.search(r"rotat(?:e|ion)", text, re.IGNORECASE):
            result["rotate"] = [0.0, 360.0]

        # Zoom amplitude (scale range). Accept "zoom 0.4x->1.6x", "zoom 0.4→1.6",
        # and the "x" suffix as optional. The separator handles both single-char
        # forms ("-", "→", "=") and the common "->" arrow form.
        zoom_range = re.search(
            r"zoom\s*([\d.]+)x?\s*[-→=]+>?\s*([\d.]+)x?",
            text,
            re.IGNORECASE,
        )
        if zoom_range:
            result["scale"] = [float(zoom_range.group(1)), float(zoom_range.group(2))]

        # Fade-in duration (seconds). Accept "fade in", "fade-in", "fadein"
        # and the optional "<n>s" duration either BEFORE ("0.5s fade-in") or
        # AFTER ("fade-in 0.5s") the keyword. Default 0.5s if no duration.
        fade_in_match = re.search(
            r"(?:(\d+(?:\.\d+)?)\s*s\s+fade[- ]?in|fade[- ]?in(?:\s+(\d+(?:\.\d+)?)\s*s)?)",
            text,
            re.IGNORECASE,
        )
        if fade_in_match:
            sec_raw = fade_in_match.group(1) or fade_in_match.group(2)
            result["fade_in"] = float(sec_raw) if sec_raw is not None else 0.5

        # Fade-out duration (seconds). Same before/after keyword tolerance.
        fade_out_match = re.search(
            r"(?:(\d+(?:\.\d+)?)\s*s\s+fade[- ]?out|fade[- ]?out(?:\s+(\d+(?:\.\d+)?)\s*s)?)",
            text,
            re.IGNORECASE,
        )
        if fade_out_match:
            sec_raw = fade_out_match.group(1) or fade_out_match.group(2)
            result["fade_out"] = float(sec_raw) if sec_raw is not None else 0.5

        return result
    except Exception:
        return {}


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
                params = extract_animation_params(segment)
                if params:
                    transform["animation_params"] = params
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
                    params = extract_animation_params(segment)
                    if params:
                        shot_lang["animation_params"] = params
    return True