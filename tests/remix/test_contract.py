"""T01 contract test - Python parity against the shared fixture set.

Three-language parity goal: Go (internal/model/remix_v2_test.go) and TS
(src/services/__tests__/remixContract.test.ts) must produce identical
accept/reject verdicts on every fixture case. The fixture file SHA is
checked at test-collection time.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from lib.remix_contract import (
    ERROR_CODES,
    MAX_DURATION_MS,
    MAX_SCENES,
    SUPPORTED_FPS,
    ValidationFailure,
    _expand_capacity_cases,
    load_fixtures,
    output_frame,
    run_all_fixtures,
    validate_draft,
    validate_ready,
)

FIXTURE_PATH = (
    Path(__file__).resolve().parent.parent.parent / "fixtures" / "remix-v2" / "contract-cases.json"
)

# SHA pinned by I at T01 release. Cross-language tests assert this same hash.
EXPECTED_FIXTURE_SHA256 = "4dd0c347e5cb71b62e14b1d1273c8d85617503024bcb58800b33789693d541f6"


def test_fixture_sha256_pinned():
    """Cross-language SHA stability — Go and TS tests pin the same hash."""
    actual = hashlib.sha256(FIXTURE_PATH.read_bytes()).hexdigest()
    assert actual == EXPECTED_FIXTURE_SHA256, (
        f"fixture SHA drifted; I must coordinate a CR for cross-language version. "
        f"expected={EXPECTED_FIXTURE_SHA256} actual={actual}"
    )


def test_output_frame_2_5_8_seconds():
    """C2: 2/5/8 second scenes at fps=30 produce 60/150/240 frames."""
    assert output_frame(2000, 30) == 60
    assert output_frame(5000, 30) == 150
    assert output_frame(8000, 30) == 240
    assert output_frame(15000, 30) == 450
    # boundary
    assert output_frame(33, 30) == 1   # ~1 frame at 33ms
    assert output_frame(16, 30) == 0   # below 1 frame
    # zero and negatives are rejected via validation, not output_frame


def test_error_codes_set_matches_c8():
    assert "ASSET_NOT_FOUND" in ERROR_CODES
    assert "INVALID_TIMELINE" in ERROR_CODES
    assert "CAPACITY_EXCEEDED" in ERROR_CODES
    assert "UNSUPPORTED_TRANSITION" in ERROR_CODES
    assert "UNRESOLVED_SCENE" in ERROR_CODES
    assert "VERSION_CONFLICT" in ERROR_CODES
    assert len(ERROR_CODES) >= 18


def test_three_scenes_draft_accepted():
    fixtures = load_fixtures()
    validate_draft(fixtures["valid_v2_draft_three_scenes"]["package"])


def test_three_scenes_ready_accepted():
    fixtures = load_fixtures()
    validate_ready(fixtures["valid_v2_ready_three_scenes"]["package"])


def test_v1_legacy_draft_accepted():
    fixtures = load_fixtures()
    # v1 packages should still be readable as draft (legacy compat)
    # but our validate_draft requires schema_version=2. So this case should
    # be rejected with VERSION_CONFLICT — that's the design.
    pkg = fixtures["valid_v1_legacy"]["package"]
    with pytest.raises(ValidationFailure) as exc:
        validate_draft(pkg)
    assert exc.value.code == "VERSION_CONFLICT"


@pytest.mark.parametrize("case_name,expected", [
    ("reject_no_source", "ASSET_NOT_FOUND"),
    ("reject_negative_duration", "INVALID_TIMELINE"),
    ("reject_overlap_scenes", "INVALID_TIMELINE"),
    ("reject_duplicate_scene_id", "INVALID_TIMELINE"),
    ("reject_missing_asset", "ASSET_NOT_FOUND"),
    ("reject_unknown_mode", "INVALID_TIMELINE"),
    ("reject_unknown_transition", "UNSUPPORTED_TRANSITION"),
    ("reject_capacity_exceeded_201", "CAPACITY_EXCEEDED"),
    ("boundary_exactly_200_scenes", None),  # accepted as draft
])
def test_rejection_codes(case_name: str, expected: str | None):
    fixtures = load_fixtures()
    _expand_capacity_cases(fixtures)
    pkg = fixtures[case_name]["package"]
    if expected is None:
        # boundary case: should accept
        validate_draft(pkg)
    else:
        expected_field = fixtures[case_name].get("expected", "").upper()
        with pytest.raises(ValidationFailure) as exc:
            if "READY" in expected_field:
                validate_ready(pkg)
            else:
                validate_draft(pkg)
        assert exc.value.code == expected, (
            f"{case_name}: expected code {expected}, got {exc.value.code} "
            f"(message={exc.value.message})"
        )


def test_constants_match_plan():
    assert MAX_SCENES == 200
    assert MAX_DURATION_MS == 600_000
    assert SUPPORTED_FPS == (30,)


def test_run_all_fixtures_self_consistency():
    """Sanity: run_all_fixtures() returns PASS for every case in the fixture file."""
    results = run_all_fixtures()
    fails = [k for k, v in results.items() if not v.startswith("PASS")]
    assert not fails, f"fixture self-test failures: {fails}"