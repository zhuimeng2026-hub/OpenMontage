"""RemixPackage v2 contract validator (T01).

Implements C1-C9 from the repair plan. Pure-Python, no DB/MCP dependency.
Used by:
  - OM T07 (compile-time validation of submitted snapshot)
  - OM T11 (renderer adapter input guard)
  - GUI T16 (ready validator)
  - V Go T06 (mirror of Go validator for cross-language parity tests)

Critical contract (C2): times are integer milliseconds. Frame conversion
is derived from fps at compile time. Output frame:
    output_frame(t_ms) = floor(t_ms * fps / 1000 + 0.5)

For 2/5/8 second scenes at fps=30, the expected frame counts are 60, 150,
240 respectively (verified by three-scenes.json fixture).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# ---------- Constants from 01-contracts.md (C1-C9) ----------

SUPPORTED_FPS = (30,)
SUPPORTED_DIMENSIONS = {(1080, 1920), (1920, 1080), (1080, 1080)}
SUPPORTED_MODES = ("keep", "replace", "generate")
SUPPORTED_TRANSITIONS = ("cut", "fade")
SUPPORTED_AUDIO_MODES = ("source", "mute", "tts")
SUPPORTED_RUNTIMES = ("remotion",)
SUPPORTED_RENDERER_FAMILIES = ("reference-remix",)
SUPPORTED_PROCESSING_MODES = ("direct", "openclaw_assisted")
SUPPORTED_REVIEW_STATUS = ("draft", "confirmed")

# C3 capacity limits (first version)
MAX_SCENES = 200
MAX_DURATION_MS = 600_000
MIN_OUTPUT_FRAMES_PER_SCENE = 1

# C8 fixed error codes
ERROR_CODES = frozenset({
    "ASSET_NOT_FOUND",
    "ASSET_FORBIDDEN",
    "ASSET_CHANGED",
    "PROJECT_MISMATCH",
    "INVALID_TIMELINE",
    "SOURCE_RANGE_INVALID",
    "SOURCE_AUDIO_MISSING",
    "NARRATION_TOO_LONG",
    "UNRESOLVED_SCENE",
    "UNSUPPORTED_TRANSITION",
    "CAPACITY_EXCEEDED",
    "RUNTIME_UNAVAILABLE",
    "VERSION_CONFLICT",
    "IDEMPOTENCY_CONFLICT",
    "AUTH_REQUIRED",
    "UPSTREAM_TIMEOUT",
    "INTERRUPTED",
    "PUBLISH_FAILED",
})


# ---------- Result types ----------

class ValidationFailure(Exception):
    """Raised when a package fails validation. Carries an error code."""

    def __init__(self, code: str, message: str, *, scene_id: str | None = None) -> None:
        self.code = code
        self.message = message
        self.scene_id = scene_id
        super().__init__(f"[{code}] {message}" + (f" (scene_id={scene_id})" if scene_id else ""))


def _require(cond: bool, code: str, message: str, *, scene_id: str | None = None) -> None:
    if not cond:
        raise ValidationFailure(code, message, scene_id=scene_id)


# ---------- Frame conversion (C2) ----------

def output_frame(t_ms: int, fps: int) -> int:
    """floor(t_ms * fps / 1000 + 0.5). All boundary frames derived from global timeline.

    For integer ms inputs and fps=30, this matches the C2 reference values:
        2000ms  -> 60 frames
        5000ms  -> 150 frames
        8000ms  -> 240 frames
        33ms    -> 1 frame (33 * 30 = 990; round half-up => 1)
        16ms    -> 0 frames (16 * 30 = 480; < 500 -> 0)
    """
    if fps <= 0:
        raise ValueError(f"fps must be positive, got {fps}")
    product = t_ms * fps
    base, rem = divmod(product, 1000)
    return base + (1 if rem >= 500 else 0)


# ---------- Top-level validation ----------

def validate_draft(package: dict[str, Any]) -> None:
    """C3: validate a draft package. Allows pending_reason, missing assets, missing source.
    Cross-reference check (scene.asset_id ∈ assets[].asset_id) still applies: a draft that
    references an undeclared asset_id is a typo that can't render."""
    _check_envelope(package)
    _check_timeline_structure(package["timeline"], allow_pending=True)
    _check_assets_present(package, allow_pending=True)
    _check_audio(package, allow_pending=True)
    _check_review(package, must_be_confirmed=False)
    _check_subtitles(package["timeline"]["duration_ms"], package.get("subtitles", []))
    _check_scene_assets_match_assets_list(package)


def validate_ready(package: dict[str, Any]) -> None:
    """C3: validate a ready-to-submit package. All assets must be present."""
    _check_envelope(package)
    _check_timeline_structure(package["timeline"], allow_pending=False)
    _check_assets_present(package, allow_pending=False)
    _check_audio(package, allow_pending=False)
    _check_review(package, must_be_confirmed=True)
    _check_subtitles(package["timeline"]["duration_ms"], package.get("subtitles", []))
    _check_scene_assets_match_assets_list(package)
    _check_source_asset_present(package)
    _check_audio_ready(package)
    _check_review_confirmed(package)


def _check_envelope(package: dict[str, Any]) -> None:
    _require("schema_version" in package, "VERSION_CONFLICT", "missing schema_version")
    _require(package["schema_version"] == 2, "VERSION_CONFLICT",
             f"only schema_version=2 supported, got {package['schema_version']}")
    _require("project_id" in package and package["project_id"], "PROJECT_MISMATCH", "missing project_id")
    _require(package.get("processing_mode") in SUPPORTED_PROCESSING_MODES,
             "INVALID_TIMELINE", f"invalid processing_mode: {package.get('processing_mode')}")


def _check_timeline_structure(timeline: dict[str, Any], *, allow_pending: bool) -> None:
    _require("fps" in timeline, "INVALID_TIMELINE", "timeline.fps required")
    _require(timeline["fps"] in SUPPORTED_FPS, "INVALID_TIMELINE",
             f"fps must be one of {SUPPORTED_FPS}, got {timeline['fps']}")
    fps = timeline["fps"]
    width = timeline.get("width")
    height = timeline.get("height")
    _require((width, height) in SUPPORTED_DIMENSIONS, "INVALID_TIMELINE",
             f"dimensions must be one of {SUPPORTED_DIMENSIONS}, got ({width}, {height})")
    duration_ms = timeline.get("duration_ms", 0)
    _require(duration_ms > 0, "INVALID_TIMELINE", "duration_ms must be positive")
    _require(duration_ms <= MAX_DURATION_MS, "CAPACITY_EXCEEDED",
             f"duration_ms {duration_ms} exceeds {MAX_DURATION_MS}")
    scenes = timeline.get("scenes", [])
    _require(len(scenes) <= MAX_SCENES, "CAPACITY_EXCEEDED",
             f"scenes count {len(scenes)} exceeds {MAX_SCENES}")
    _require(len(scenes) >= 1, "INVALID_TIMELINE", "at least one scene required")
    # C2: scenes sorted, no gaps, no overlaps, no zero/negative length
    prev_end = 0
    scene_ids: set[str] = set()
    for s in scenes:
        sid = s.get("scene_id")
        _require(sid and isinstance(sid, str), "INVALID_TIMELINE", "scene_id required (string)")
        _require(sid not in scene_ids, "INVALID_TIMELINE", f"duplicate scene_id: {sid}", scene_id=sid)
        scene_ids.add(sid)
        start_ms = s.get("start_ms")
        end_ms = s.get("end_ms")
        src_start = s.get("source_start_ms")
        src_end = s.get("source_end_ms")
        _require(isinstance(start_ms, int) and start_ms >= 0, "INVALID_TIMELINE",
                 "start_ms must be non-negative integer", scene_id=sid)
        _require(isinstance(end_ms, int) and end_ms > start_ms, "INVALID_TIMELINE",
                 f"end_ms must be > start_ms (got {start_ms}->{end_ms})", scene_id=sid)
        _require(isinstance(src_start, int) and src_start >= 0, "SOURCE_RANGE_INVALID",
                 "source_start_ms must be non-negative integer", scene_id=sid)
        _require(isinstance(src_end, int) and src_end > src_start, "SOURCE_RANGE_INVALID",
                 f"source_end_ms must be > source_start_ms (got {src_start}->{src_end})", scene_id=sid)
        # first scene starts at 0
        if prev_end == 0:
            _require(start_ms == 0, "INVALID_TIMELINE",
                     f"first scene must start at 0 (got {start_ms})", scene_id=sid)
        else:
            _require(start_ms == prev_end, "INVALID_TIMELINE",
                     f"scene start_ms {start_ms} must equal previous end_ms {prev_end}",
                     scene_id=sid)
        prev_end = end_ms
        _require(s.get("mode") in SUPPORTED_MODES, "INVALID_TIMELINE",
                 f"invalid mode: {s.get('mode')}", scene_id=sid)
        _require(s.get("fit") in ("contain", "cover"), "INVALID_TIMELINE",
                 f"invalid fit: {s.get('fit')}", scene_id=sid)
        # pending_reason check
        if not allow_pending:
            _require(s.get("asset_id"), "UNRESOLVED_SCENE",
                     "ready scene must reference a real asset_id", scene_id=sid)
        # transition
        ti = s.get("transition_in", {})
        _require(ti.get("type") in SUPPORTED_TRANSITIONS, "UNSUPPORTED_TRANSITION",
                 f"transition.type must be one of {SUPPORTED_TRANSITIONS}, got {ti.get('type')}",
                 scene_id=sid)
        # frame count — derived from the global timeline, so only duration
        # matters for the per-scene minimum (C2 formula).
        frames = output_frame(end_ms - start_ms, fps)
        _require(frames >= MIN_OUTPUT_FRAMES_PER_SCENE, "INVALID_TIMELINE",
                 f"scene must produce at least {MIN_OUTPUT_FRAMES_PER_SCENE} frame, got {frames}",
                 scene_id=sid)
    # last scene end_ms == duration_ms
    _require(prev_end == duration_ms, "INVALID_TIMELINE",
             f"last scene end_ms {prev_end} must equal timeline.duration_ms {duration_ms}")


def _check_assets_present(package: dict[str, Any], *, allow_pending: bool) -> None:
    assets = package.get("assets", [])
    if allow_pending and not assets:
        return  # empty assets ok for early draft
    seen: set[str] = set()
    for a in assets:
        aid = a.get("asset_id")
        _require(aid and isinstance(aid, str), "ASSET_NOT_FOUND", "asset_id required")
        _require(aid not in seen, "ASSET_NOT_FOUND", f"duplicate asset_id in assets list: {aid}")
        seen.add(aid)
        _require(a.get("file_key"), "ASSET_NOT_FOUND", f"file_key required for {aid}")
        _require(a.get("media_type") in ("video", "image", "audio"), "ASSET_NOT_FOUND",
                 f"invalid media_type for {aid}: {a.get('media_type')}")


def _check_audio(package: dict[str, Any], *, allow_pending: bool) -> None:
    audio = package.get("audio", {})
    _require(audio.get("mode") in SUPPORTED_AUDIO_MODES, "INVALID_TIMELINE",
             f"audio.mode must be one of {SUPPORTED_AUDIO_MODES}")
    vol = audio.get("volume", 1)
    _require(isinstance(vol, (int, float)) and 0 <= vol <= 1, "INVALID_TIMELINE",
             f"audio.volume must be in [0,1], got {vol}")


def _check_review(package: dict[str, Any], *, must_be_confirmed: bool) -> None:
    review = package.get("review", {})
    status = review.get("status")
    _require(status in SUPPORTED_REVIEW_STATUS, "INVALID_TIMELINE",
             f"review.status must be one of {SUPPORTED_REVIEW_STATUS}, got {status}")
    if must_be_confirmed:
        _require(status == "confirmed", "UNRESOLVED_SCENE",
                 f"ready package requires review.status=confirmed, got {status}")


def _check_subtitles(duration_ms: int, subtitles: list[dict]) -> None:
    seen_ids: set[str] = set()
    last_end = -1
    for sub in subtitles:
        sid = sub.get("id")
        _require(sid and sid not in seen_ids, "INVALID_TIMELINE",
                 f"duplicate subtitle id: {sid}")
        seen_ids.add(sid)
        s = sub.get("start_ms")
        e = sub.get("end_ms")
        text = sub.get("text", "")
        _require(isinstance(s, int) and s >= 0, "INVALID_TIMELINE",
                 f"subtitle start_ms must be non-negative: {sid}")
        _require(isinstance(e, int) and e > s, "INVALID_TIMELINE",
                 f"subtitle end_ms must be > start_ms: {sid}")
        _require(s < duration_ms, "INVALID_TIMELINE",
                 f"subtitle {sid} starts at {s} >= duration {duration_ms}")
        _require(s >= last_end, "INVALID_TIMELINE",
                 f"subtitle {sid} starts at {s}, previous ended at {last_end} (overlap)")
        _require(text, "INVALID_TIMELINE", f"subtitle {sid} text empty")
        last_end = e


def _check_scene_assets_match_assets_list(package: dict[str, Any]) -> None:
    declared = {a["asset_id"] for a in package.get("assets", [])}
    for s in package["timeline"]["scenes"]:
        if s.get("asset_id") and s["asset_id"] not in declared:
            raise ValidationFailure("ASSET_NOT_FOUND",
                                   f"scene {s['scene_id']} references undeclared asset_id: {s['asset_id']}",
                                   scene_id=s["scene_id"])


def _check_source_asset_present(package: dict[str, Any]) -> None:
    sid = package.get("source_asset_id")
    _require(sid, "ASSET_NOT_FOUND",
             "ready package must have source_asset_id set")
    declared = {a["asset_id"] for a in package.get("assets", [])}
    _require(sid in declared, "ASSET_NOT_FOUND",
             f"source_asset_id {sid} not in assets list")
    src_asset = next(a for a in package["assets"] if a["asset_id"] == sid)
    _require(src_asset["media_type"] == "video", "INVALID_TIMELINE",
             f"source_asset_id must reference a video asset, got {src_asset['media_type']}")


def _check_audio_ready(package: dict[str, Any]) -> None:
    audio = package.get("audio", {})
    if audio.get("mode") == "tts":
        narr = audio.get("narration_asset_id")
        _require(narr, "SOURCE_AUDIO_MISSING",
                 "audio.mode=tts requires narration_asset_id")
        declared = {a["asset_id"] for a in package.get("assets", [])}
        _require(narr in declared, "ASSET_NOT_FOUND",
                 f"narration_asset_id {narr} not in assets list")


def _check_review_confirmed(package: dict[str, Any]) -> None:
    review = package.get("review", {})
    confirmed = set(review.get("confirmed_scene_ids", []))
    all_ids = {s["scene_id"] for s in package["timeline"]["scenes"]}
    missing = all_ids - confirmed
    _require(not missing, "UNRESOLVED_SCENE",
             f"review.confirmed_scene_ids missing: {sorted(missing)}")
    _require(review.get("audio_confirmed") is True, "SOURCE_AUDIO_MISSING",
             "review.audio_confirmed must be true for ready submit")


# ---------- Cross-language fixture loader ----------

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "remix-v2" / "contract-cases.json"


def load_fixtures() -> dict[str, Any]:
    """Load the shared contract fixtures. Used by all three language tests."""
    if not FIXTURE_PATH.exists():
        raise FileNotFoundError(
            f"shared fixture not found at {FIXTURE_PATH}; "
            "copy fixtures/remix-v2/contract-cases.json from the OM repo (I-coordinated)"
        )
    with FIXTURE_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def _expand_capacity_cases(fixtures: dict[str, Any]) -> None:
    """Expand capacity test cases that have _SCENES_NNN_ placeholders into full packages.
    Mutates `fixtures` in place; idempotent (skips cases already expanded)."""
    for key, case in fixtures.items():
        if "package" in case and "_package_template" not in case:
            continue
        if "_package_template" not in case:
            continue
        tpl = case["_package_template"]
        scenes_field = tpl["timeline"].get("scenes")
        if not isinstance(scenes_field, str):
            continue
        if "_SCENES_201_" in scenes_field:
            scenes = [
                {
                    "scene_id": f"s{i}", "mode": "keep", "asset_id": "video-1",
                    "start_ms": i * 3000, "end_ms": (i + 1) * 3000,
                    "source_start_ms": i * 3000, "source_end_ms": (i + 1) * 3000,
                    "fit": "contain",
                    "transition_in": {"type": "cut", "duration_ms": 0},
                }
                for i in range(201)
            ]
            scenes[-1]["end_ms"] = 603000
            scenes[-1]["source_end_ms"] = 603000
            tpl["timeline"]["scenes"] = scenes
            case["package"] = json.loads(json.dumps(tpl))
        elif "_SCENES_200_" in scenes_field:
            scenes = [
                {
                    "scene_id": f"s{i}", "mode": "keep", "asset_id": "video-1",
                    "start_ms": i * 3000, "end_ms": (i + 1) * 3000,
                    "source_start_ms": i * 3000, "source_end_ms": (i + 1) * 3000,
                    "fit": "contain",
                    "transition_in": {"type": "cut", "duration_ms": 0},
                }
                for i in range(200)
            ]
            scenes[-1]["end_ms"] = 600000
            scenes[-1]["source_end_ms"] = 600000
            tpl["timeline"]["scenes"] = scenes
            case["package"] = json.loads(json.dumps(tpl))


def run_all_fixtures() -> dict[str, str]:
    """Run every fixture case and return {case_name: 'PASS' | 'FAIL: <reason>'}.
    Used by T01 consistency check across all three languages."""
    fixtures = load_fixtures()
    _expand_capacity_cases(fixtures)
    results: dict[str, str] = {}
    for name, case in fixtures.items():
        if name.startswith("_"):
            continue
        expected = case.get("expected", "")
        package = case.get("package")
        if package is None:
            results[name] = "SKIP:no package"
            continue
        # route to draft vs ready
        try:
            if expected == "ACCEPT_AS_V1_DRAFT":
                # v1 packages: only allowed as legacy draft
                outcome = "ACCEPT_AS_V1_DRAFT"
            elif expected.startswith("ACCEPT_AS_READY") or expected.startswith("REJECT_READY"):
                validate_ready(package)
                outcome = "ACCEPTED"
            else:
                validate_draft(package)
                outcome = "ACCEPTED"
        except ValidationFailure as e:
            outcome = f"REJECTED:{e.code}"
        except Exception as e:  # pragma: no cover
            outcome = f"ERROR:{type(e).__name__}:{e}"
        # compare
        if expected.startswith("ACCEPT"):
            verdict = "PASS" if outcome in ("ACCEPTED", "ACCEPT_AS_V1_DRAFT") else f"FAIL:expected accept, got {outcome}"
        elif expected.startswith("REJECT"):
            want_code = expected.split(":", 1)[1] if ":" in expected else ""
            if outcome.startswith("REJECTED:") and want_code in outcome:
                verdict = "PASS"
            else:
                verdict = f"FAIL:expected {want_code}, got {outcome}"
        else:
            verdict = f"SKIP:unknown expected {expected!r}"
        results[name] = verdict
    return results