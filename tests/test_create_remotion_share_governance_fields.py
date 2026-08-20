"""Tests for the governance-field backfill in create_remotion_video_share.

Background
----------
video_compose._pre_compose_validation BLOCKS the render when
``edit_decisions.renderer_family`` is missing, and silently skips the
delivery_promise contract when ``metadata.delivery_promise`` is absent.

This test suite pins the contract that ``_ensure_governance_fields`` (and by
extension ``create_remotion_video_share`` / ``_run_render_job``) guarantee
both fields are populated before video_compose's pre-compose gate runs.

Why these tests matter
----------------------
A real production batch (``frameflow-batch-batch-6dff71e826e70c440806311d``)
was BLOCKing in 9 ms because the upstream path left ``renderer_family`` empty.
The fix backfills both fields with conservative defaults at the construction
site. These tests guard the defaults, the idempotency contract, and the
delivery_promise_override passthrough.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from lib.delivery_promise import DeliveryPromise
import mcp_server
from tools.base_tool import ToolResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_env(monkeypatch, tmp_path):
    """Mirror tests/test_workbuddy_session_remotion_share.py::_state_env."""
    import lib.workbuddy_session as sessions

    monkeypatch.setattr(sessions, "STATE_DIR", tmp_path / "projects" / ".mcp_sessions")
    monkeypatch.setattr(sessions, "ROOT", tmp_path)


def _image(tmp_path, name="one.jpg"):
    path = tmp_path / "projects" / "demo" / "assets" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"fake-image")
    return path


def _install_capture(monkeypatch, tmp_path):
    """Install a fake video_compose that records the edit_decisions it sees.

    Returns ``(captured_dict, mcp_server_module)`` so the test can introspect
    the edit_decisions that the upstream builder hands to video_compose.
    """
    monkeypatch.setattr(mcp_server, "_PROJECT_ROOT", tmp_path)

    captured: dict = {}

    class FakeCompose:
        def execute(self, inputs):
            captured["edit_decisions"] = json_roundtrip(inputs["edit_decisions"])
            output = Path(inputs["output_path"])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"mp4")
            return ToolResult(True, {"output": str(output)})

    class FakeUpload:
        def execute(self, inputs):
            return ToolResult(True, {"file_id": "file-1"})

    class FakeShare:
        def execute(self, inputs):
            return ToolResult(True, {"short_url": "https://share.weiyun.com/x"})

    tools = {
        "video_compose": FakeCompose(),
        "weiyun_upload": FakeUpload(),
        "weiyun_share_link": FakeShare(),
    }
    monkeypatch.setattr(mcp_server.registry, "get", lambda name: tools.get(name))
    return captured, mcp_server


def json_roundtrip(obj):
    """Round-trip through JSON to defensively deep-copy nested dicts/lists."""
    import json

    return json.loads(json.dumps(obj))


@pytest.fixture
def workflow_session(monkeypatch, tmp_path):
    """Seed a 2-image MCP session using the same pattern as the workbuddy
    share test. Yields (captured_dict, mcp_server_module) for inspection."""
    import lib.workbuddy_session as sessions
    from lib.mcp_session import reset_mcp_session_id, set_mcp_session_id

    _state_env(monkeypatch, tmp_path)
    # _resolve_session_asset_path reads `relative_path` (posix, repo-root
    # relative), not the OS-specific absolute `path`. We monkeypatch
    # _PROJECT_ROOT to tmp_path, so relative_path is relative to tmp_path.
    one_rel = "projects/demo/assets/one.jpg"
    two_rel = "projects/demo/assets/two.jpg"
    sessions.register_image(
        "workflow", "demo",
        {"id": "img-1", "path": str(_image(tmp_path)), "relative_path": one_rel,
         "type": "image", "sha256": "x"},
    )
    sessions.register_image(
        "workflow", "demo",
        {"id": "img-2", "path": str(_image(tmp_path, "two.jpg")), "relative_path": two_rel,
         "type": "image", "sha256": "y"},
    )
    captured, ms = _install_capture(monkeypatch, tmp_path)
    token = set_mcp_session_id("workflow")
    try:
        yield captured, ms
    finally:
        reset_mcp_session_id(token)


# ---------------------------------------------------------------------------
# Pure-function tests for _ensure_governance_fields
# ---------------------------------------------------------------------------


def test_ensure_fills_renderer_family_when_missing():
    """renderer_family absent → default to 'animation-first'."""
    ed: dict = {}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
    )
    assert ed["renderer_family"] == "animation-first"


def test_ensure_keeps_existing_renderer_family():
    """renderer_family set by caller → not overwritten (idempotency)."""
    ed = {"renderer_family": "cinematic-trailer"}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
    )
    assert ed["renderer_family"] == "cinematic-trailer"


def test_ensure_overwrites_empty_string_renderer_family():
    """Empty-string renderer_family is treated as missing (defensive)."""
    ed = {"renderer_family": ""}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="cinematic-trailer",
        script_id="cinematic-montage",
    )
    assert ed["renderer_family"] == "cinematic-trailer"


def test_ensure_fills_delivery_promise_with_hybrid_motion_false():
    """delivery_promise absent → derived as hybrid + motion_required=False."""
    ed = {"renderer_family": "animation-first"}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
    )
    promise = ed["metadata"]["delivery_promise"]
    assert promise["promise_type"] == "hybrid"
    # motion_required must be False — image-only inputs cannot promise motion.
    assert promise["motion_required"] is False
    assert promise["tone_mode"] == "corporate"
    assert promise["quality_floor"] == "presentable"


def test_ensure_keeps_existing_top_level_delivery_promise():
    """Top-level edit_decisions.delivery_promise is also respected."""
    existing = {
        "promise_type": "motion_led",
        "motion_required": True,
        "source_required": False,
        "tone_mode": "energetic",
        "quality_floor": "broadcast",
        "approved_fallback": None,
    }
    ed = {"renderer_family": "animation-first", "delivery_promise": existing}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
    )
    # The metadata.delivery_promise branch should not override the top-level one.
    assert ed["delivery_promise"] == existing


def test_ensure_keeps_existing_metadata_delivery_promise():
    """metadata.delivery_promise wins when caller already set it."""
    existing = {
        "promise_type": "motion_led",
        "motion_required": True,
        "source_required": False,
        "tone_mode": "energetic",
        "quality_floor": "broadcast",
        "approved_fallback": None,
    }
    ed = {"renderer_family": "animation-first", "metadata": {"delivery_promise": existing}}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
    )
    assert ed["metadata"]["delivery_promise"] == existing


def test_ensure_override_passes_through_when_caller_blank():
    """delivery_promise_override is used when caller did not set one."""
    override = {
        "promise_type": "motion_led",
        "motion_required": True,
        "source_required": False,
        "tone_mode": "cinematic",
        "quality_floor": "premium",
        "approved_fallback": None,
    }
    ed = {"renderer_family": "animation-first"}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
        delivery_promise_override=override,
    )
    assert ed["metadata"]["delivery_promise"] == override


def test_ensure_classify_failure_falls_back_to_literal(monkeypatch):
    """classify_from_brief raising → hard-coded literal default is used."""
    import lib.delivery_promise as dp

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated classifier failure")

    monkeypatch.setattr(dp, "classify_from_brief", boom)
    ed: dict = {}
    mcp_server._ensure_governance_fields(
        ed,
        default_renderer_family="animation-first",
        script_id="photo-ken-burns",
    )
    promise = ed["metadata"]["delivery_promise"]
    assert promise == {
        "promise_type": "hybrid",
        "motion_required": False,
        "source_required": False,
        "tone_mode": "corporate",
        "quality_floor": "presentable",
        "approved_fallback": None,
    }


def test_ensure_pipeline_routing_for_known_scripts():
    """script_id → pipeline_type mapping is exercised; output is always hybrid
    + motion_required=False (because intent is fixed)."""
    for script_id in ("photo-ken-burns", "cinematic-montage", "ecommerce-product-demo"):
        ed: dict = {}
        mcp_server._ensure_governance_fields(
            ed,
            default_renderer_family="animation-first",
            script_id=script_id,
        )
        promise = ed["metadata"]["delivery_promise"]
        assert promise["promise_type"] == "hybrid"
        assert promise["motion_required"] is False


# ---------------------------------------------------------------------------
# Integration tests — call create_remotion_video_share end-to-end
# ---------------------------------------------------------------------------


def test_create_share_templated_photo_ken_burns_fills_governance(workflow_session):
    """The default script_id (photo-ken-burns) path must produce edit_decisions
    that carry renderer_family AND metadata.delivery_promise."""
    captured, ms = workflow_session
    result = asyncio.run(ms.create_remotion_video_share())
    assert result["success"] is True, result
    ed = captured["edit_decisions"]
    assert ed["renderer_family"] == "animation-first"
    promise = ed["metadata"]["delivery_promise"]
    assert promise["promise_type"] == "hybrid"
    assert promise["motion_required"] is False


def test_create_share_templated_cinematic_montage_fills_governance(workflow_session):
    """cinematic-montage → renderer_family='cinematic-trailer' + hybrid promise."""
    captured, ms = workflow_session
    result = asyncio.run(ms.create_remotion_video_share(script_id="cinematic-montage"))
    assert result["success"] is True, result
    ed = captured["edit_decisions"]
    assert ed["renderer_family"] == "cinematic-trailer"
    promise = ed["metadata"]["delivery_promise"]
    assert promise["promise_type"] == "hybrid"
    assert promise["motion_required"] is False


def test_create_share_custom_code_path_fills_governance(workflow_session, monkeypatch):
    """code (custom composition) path must not be skipped; delivery_promise
    must still be backfilled even though renderer_family is hardcoded."""
    captured, ms = workflow_session
    monkeypatch.setenv("CUSTOM_COMPOSITION_ENABLED", "true")
    result = asyncio.run(
        ms.create_remotion_video_share(code="export const Composition = () => null;")
    )
    assert result["success"] is True, result
    ed = captured["edit_decisions"]
    assert ed["renderer_family"] == "custom-composition"
    promise = ed["metadata"]["delivery_promise"]
    assert promise["promise_type"] == "hybrid"
    assert promise["motion_required"] is False


def test_create_share_delivery_promise_override_passthrough(workflow_session):
    """An override supplied to create_remotion_video_share flows into
    metadata.delivery_promise without being replaced by the default."""
    captured, ms = workflow_session
    override = {
        "promise_type": "motion_led",
        "motion_required": True,
        "source_required": False,
        "tone_mode": "cinematic",
        "quality_floor": "premium",
        "approved_fallback": None,
    }
    result = asyncio.run(
        ms.create_remotion_video_share(delivery_promise_override=override)
    )
    assert result["success"] is True, result
    ed = captured["edit_decisions"]
    assert ed["metadata"]["delivery_promise"] == override


def test_run_render_job_backfills_legacy_job_kwargs(workflow_session, monkeypatch, tmp_path):
    """_run_render_job is the defense-in-depth site: even if edit_decisions
    lacks both fields (e.g. a job persisted before the fix is restored from
    .mcp_jobs.json and dispatched), the function must backfill them before
    invoking video_compose.execute."""
    captured, ms = workflow_session
    # First, run create_remotion_video_share once to seed the session state.
    result = asyncio.run(ms.create_remotion_video_share())
    assert result["success"] is True, result
    # Now invoke _run_render_job directly with a stripped edit_decisions to
    # simulate the post-restart drain path. _run_render_job is synchronous —
    # it spawns a daemon thread internally — so do NOT asyncio.run() it.
    legacy_ed = {
        "version": "1.0",
        "cuts": [],
        "render_runtime": "remotion",
        "composition_mode": "templated",
        "metadata": {"script_id": "photo-ken-burns", "title": "legacy"},
    }
    out = tmp_path / "renders" / "legacy.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)

    captured2: dict = {}

    class FakeCompose2:
        def execute(self, inputs):
            captured2["edit_decisions"] = json_roundtrip(inputs["edit_decisions"])
            Path(inputs["output_path"]).write_bytes(b"mp4")
            return ToolResult(True, {"output": inputs["output_path"]})

    tools2 = {
        "video_compose": FakeCompose2(),
        "weiyun_upload": type("U", (), {"execute": lambda s, i: ToolResult(True, {"file_id": "x"})})(),
        "weiyun_share_link": type("S", (), {"execute": lambda s, i: ToolResult(True, {"short_url": "https://x"})})(),
    }
    monkeypatch.setattr(mcp_server.registry, "get", lambda name: tools2.get(name))

    mcp_server._run_render_job(
        sid="workflow",
        request_id="drain-1",
        project="demo",
        batch_id="legacy-batch",
        job_id="legacy-job",
        safe_assets=[],
        edit_decisions=legacy_ed,
        asset_manifest={
            "version": "1.0",
            "assets": [],
            "metadata": {"project_id": "demo", "batch_id": "legacy-batch"},
        },
        scene_plan=[],
        profile="tiktok",
        output=str(out),
        title="legacy",
        asset_count=0,
    )
    # _run_render_job spawns a daemon thread; poll briefly for the captured
    # edit_decisions to appear.
    deadline = time.monotonic() + 5.0
    while "edit_decisions" not in captured2 and time.monotonic() < deadline:
        time.sleep(0.05)
    assert "edit_decisions" in captured2, "render thread never invoked video_compose"
    ed = captured2["edit_decisions"]
    assert ed["renderer_family"] == "animation-first"
    promise = ed["metadata"]["delivery_promise"]
    assert promise["promise_type"] == "hybrid"
    assert promise["motion_required"] is False