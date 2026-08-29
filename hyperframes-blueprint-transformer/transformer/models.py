"""Pydantic schemas for `target_blueprint.json`.

Strictly mirrors MVP doc §11. Validation fails loudly if upstream
OpenMontage emits a malformed payload — easier to debug than silent
fallback to wrong scene types.

Allowed scene types (MVP doc §9):
    hook, pain_point, product_reveal, feature_demo,
    lifestyle, social_proof, offer, cta
Fallback policy (MVP doc §9): unknown -> feature_demo.
The fallback is applied in `mapping.py` so the schema stays
strict and bugs surface fast.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class SceneType(str, Enum):
    """MVP doc §9 — first-version scene type whitelist."""

    HOOK = "hook"
    PAIN_POINT = "pain_point"
    PRODUCT_REVEAL = "product_reveal"
    FEATURE_DEMO = "feature_demo"
    LIFESTYLE = "lifestyle"
    SOCIAL_PROOF = "social_proof"
    OFFER = "offer"
    CTA = "cta"


class Transition(str, Enum):
    """MVP doc §11 — only `cut` and `fade` are accepted."""

    CUT = "cut"
    FADE = "fade"


class Format(BaseModel):
    """Video canvas. MVP is locked to 1080x1920 / 30fps."""

    model_config = ConfigDict(extra="forbid")

    width: Literal[1080]
    height: Literal[1920]
    fps: Literal[30]


class Scene(BaseModel):
    """One scene from MVP doc §11."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    order: int = Field(ge=1)
    type: SceneType
    duration: float = Field(gt=0.0, le=60.0, description="Seconds")
    headline: str = Field(min_length=1)
    voiceover: str = Field(min_length=1)
    asset_id: str | None = None
    transition: Transition = Transition.CUT


class TargetBlueprint(BaseModel):
    """MVP doc §11 — root document."""

    model_config = ConfigDict(extra="forbid")

    project_id: str = Field(min_length=1)
    sku: str | None = None
    scenario: str | None = None
    variant: str | None = None
    format: Format
    scenes: list[Scene] = Field(min_length=1)

    def sorted_scenes(self) -> list[Scene]:
        """Scenes in canonical playback order (by `order` field)."""
        return sorted(self.scenes, key=lambda s: s.order)
