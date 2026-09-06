"""Field whitelist + payload validation for the tweak server.

The Remotion props format (matches `remotion-composer/public/sample-props/*.json`):

    {
      "theme": "flat-motion-graphics",
      "captions": [],
      "cuts": [
        {"id": "...", "source": "...", "in_seconds": 0, "out_seconds": 9.07,
         "animation": "zoom-in", "backgroundColor": "#0F172A"},
        {"id": "...", "type": "text_card", "text": "...", "fontSize": 96,
         "color": "#F8FAFC", "backgroundColor": "#0F172A",
         "in_seconds": 9.07, "out_seconds": 14.64}
      ],
      "audio": {
        "narration": {"src": "...", "volume": 1.0},
        "music":     {"src": "...", "volume": 0.18, ...}
      }
    }

We validate a *user-submitted* TweakPayload (only safe fields) and merge it back
into a *full* props dict that the MCP server can consume directly.

See: docs/plans/rosy-dazzling-bear.md §4 for the field whitelist table.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

# -----------------------------------------------------------------------------
# Whitelist constants
# -----------------------------------------------------------------------------

VALID_THEMES: list[str] = [
    "clean-professional",
    "premium-minimalist",
    "flat-motion-graphics",
    "minimalist-diagram",
    "anime-ghibli",
]

# Scene types that accept a `text` field (per SCENE_TYPES.md)
TEXT_CARD_TYPES: frozenset[str] = frozenset({
    "text_card", "hero_title", "stat_card", "callout", "section_title",
})

# Scene types that accept an `animation` field (image / video — no `type`)
IMAGE_VIDEO_TYPES: frozenset[str] = frozenset({"__image__", "__video__"})

# Allowed animation values for image/video cuts (kept conservative — see
# remotion-composer/src for the full list; we only expose the simplest 3 for
# end-user micro-tweaks).
VALID_ANIMATIONS: list[str] = ["zoom-in", "pan-down", "ken-burns", "none"]

# Field value ranges
FONT_SIZE_RANGE: tuple[int, int] = (24, 200)
VOLUME_RANGE: tuple[float, float] = (0.0, 1.0)
FADE_RANGE: tuple[float, float] = (0.0, 3.0)
OFFSET_RANGE: tuple[float, float] = (0.0, 30.0)

HEX_COLOR_RE = re.compile(r"^#([0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})$")


# -----------------------------------------------------------------------------
# Request payload (what the browser sends)
# -----------------------------------------------------------------------------

class CutTweak(BaseModel):
    """One cut's user-facing tweaks. Must reference an existing cut by id."""

    id: str = Field(..., min_length=1, max_length=128)
    in_seconds: float | None = Field(None, ge=0.0, le=600.0)
    out_seconds: float | None = Field(None, ge=0.0, le=600.0)
    # Text-card fields (only valid when cut.type ∈ TEXT_CARD_TYPES)
    text: str | None = Field(None, max_length=500)
    fontSize: int | None = Field(None, ge=FONT_SIZE_RANGE[0], le=FONT_SIZE_RANGE[1])
    color: str | None = None
    # Image / video fields (only valid when cut has `source` but no `type`)
    animation: str | None = None
    # Universal
    backgroundColor: str | None = None

    @field_validator("color", "backgroundColor")
    @classmethod
    def _check_hex(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not HEX_COLOR_RE.match(v):
            raise ValueError(f"color must be #RRGGBB or #RRGGBBAA, got {v!r}")
        return v.upper()

    @field_validator("animation")
    @classmethod
    def _check_animation(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in VALID_ANIMATIONS:
            raise ValueError(f"animation must be one of {VALID_ANIMATIONS}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _check_duration_order(self) -> "CutTweak":
        if self.in_seconds is not None and self.out_seconds is not None:
            if self.out_seconds <= self.in_seconds:
                raise ValueError(
                    f"out_seconds ({self.out_seconds}) must be > "
                    f"in_seconds ({self.in_seconds})"
                )
        return self


class AudioTweak(BaseModel):
    """Audio mixer tweaks. Only `volume`, fade, offset — never src."""

    volume: float | None = Field(None, ge=VOLUME_RANGE[0], le=VOLUME_RANGE[1])
    fadeInSeconds: float | None = Field(None, ge=FADE_RANGE[0], le=FADE_RANGE[1])
    fadeOutSeconds: float | None = Field(None, ge=FADE_RANGE[0], le=FADE_RANGE[1])
    offsetSeconds: float | None = Field(None, ge=OFFSET_RANGE[0], le=OFFSET_RANGE[1])


class AudioBlockTweak(BaseModel):
    narration: AudioTweak | None = None
    music: AudioTweak | None = None


class TweakPayload(BaseModel):
    """The full payload the browser submits."""

    theme: str | None = None
    cuts: list[CutTweak] = Field(default_factory=list)
    audio: AudioBlockTweak | None = None
    comment: str = Field(default="", max_length=1000)

    @field_validator("theme")
    @classmethod
    def _check_theme(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in VALID_THEMES:
            raise ValueError(f"theme must be one of {VALID_THEMES}, got {v!r}")
        return v


# -----------------------------------------------------------------------------
# Merge logic — turn (template, tweaks) into the full props MCP consumes
# -----------------------------------------------------------------------------

def _is_text_card(cut: dict[str, Any]) -> bool:
    return cut.get("type") in TEXT_CARD_TYPES


def _is_image_or_video(cut: dict[str, Any]) -> bool:
    """Image/video cuts have a `source` (path or asset ID) and no `type`."""
    return bool(cut.get("source")) and not cut.get("type")


def merge_into_template(template: dict[str, Any], tweak: TweakPayload) -> dict[str, Any]:
    """Merge user-safe tweaks into a full props dict.

    - Theme is replaced wholesale (only one allowed per render).
    - For each cut tweak: must reference an existing cut by id; field-level
      validation against the cut's scene type happens here (after merge
      against the template, we know whether it's text_card vs image/video).
    - Audio block is merged field-by-field (only present fields are touched).

    Returns a NEW dict (no mutation of input).
    """
    import copy

    merged = copy.deepcopy(template)

    if tweak.theme is not None:
        merged["theme"] = tweak.theme

    # Build id → cut index map once
    cuts: list[dict[str, Any]] = merged.setdefault("cuts", [])
    id_to_idx: dict[str, int] = {
        c.get("id"): i for i, c in enumerate(cuts) if c.get("id")
    }

    errors: list[str] = []

    for ct in tweak.cuts:
        idx = id_to_idx.get(ct.id)
        if idx is None:
            errors.append(
                f"cut id {ct.id!r} not found in template "
                f"(have: {sorted(id_to_idx)})"
            )
            continue
        cut = cuts[idx]

        # Universal fields (safe to set unconditionally)
        if ct.in_seconds is not None:
            cut["in_seconds"] = ct.in_seconds
        if ct.out_seconds is not None:
            cut["out_seconds"] = ct.out_seconds
        if ct.backgroundColor is not None:
            cut["backgroundColor"] = ct.backgroundColor

        if _is_text_card(cut):
            if ct.text is not None:
                cut["text"] = ct.text
            if ct.fontSize is not None:
                cut["fontSize"] = ct.fontSize
            if ct.color is not None:
                cut["color"] = ct.color
            # Reject image-only fields if mistakenly sent for text_card
            if ct.animation is not None:
                errors.append(
                    f"cut {ct.id!r} is text_card (type={cut.get('type')}); "
                    f"animation field is not valid here"
                )
        elif _is_image_or_video(cut):
            if ct.animation is not None:
                cut["animation"] = ct.animation
            # Reject text-only fields for image/video
            for f in ("text", "fontSize", "color"):
                if getattr(ct, f) is not None:
                    errors.append(
                        f"cut {ct.id!r} is image/video; {f} field is not valid here"
                    )
        else:
            # Unknown cut shape (e.g. custom scene types we don't whitelist)
            errors.append(
                f"cut {ct.id!r} has unsupported shape (type={cut.get('type')!r}, "
                f"source present={bool(cut.get('source'))})"
            )

    # Audio merge
    audio = merged.setdefault("audio", {})
    if tweak.audio is not None:
        for block_name in ("narration", "music"):
            sub = getattr(tweak.audio, block_name)
            if sub is None:
                continue
            target = audio.setdefault(block_name, {})
            for f in ("volume", "fadeInSeconds", "fadeOutSeconds", "offsetSeconds"):
                val = getattr(sub, f)
                if val is not None:
                    target[f] = val

    if errors:
        # Raise as ValueError so FastAPI returns 400 with a useful message
        raise ValueError("; ".join(errors))

    return merged