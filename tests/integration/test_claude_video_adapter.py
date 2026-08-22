"""Integration tests for ``claude_video.compose`` adapter.

These tests exercise the **real** execute() body — they redirect
``PROJECTS_DIR`` to a tmp_path and assert that:

  * whitelist + user_not_found + assets_copy_failed + video_id_unknown
    all return the right envelope codes,
  * a successful submit actually creates the project tree and copies
    artifacts into the assets/ subdirs,
  * source_meta.json + source_media_review.json + checkpoint_idea.json
    are written with the expected shape,
  * the project-id defaults to ``source.video_id`` when not provided.

If any of these tests fail, the adapter contract has drifted from
``docs/claude-video-integration.md`` §4-5,
``docs/claude-video-whitelist-audit.md``, or — since 2026-08-23 — the
claude-video-side docs:
``/opt/claude-video/docs/{BFF_API_CONTRACT,OAUTH_TRUST_MODEL,
OPENMONTAGE_NAME_MAP,MCP_SERVER_PRD}.md`` (specifically §2.6.3's
6-code error envelope).

Run:
    python -m pytest tests/integration/test_claude_video_adapter.py -v

The tests do NOT need live claude-video or an MCP server. They DO need
filesystem I/O (so PROJECTS_DIR must be writable); the autouse
``_patch_projects_dir`` fixture handles that by monkey-patching
``lib.paths.PROJECTS_DIR`` to a tmp_path.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from tools.base_tool import BaseTool, ToolResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def projects_dir(monkeypatch, tmp_path: Path) -> Path:
    """Redirect lib.paths.PROJECTS_DIR to a tmp dir so execute()'s side
    effects land in an ephemeral location. Mirrors the testability
    pattern in lib/paths.PROJECTS_DIR's docstring (``OPENMONTAGE_PROJECTS_DIR``
    env override)."""
    from lib import paths

    proj = tmp_path / "projects"
    proj.mkdir()
    monkeypatch.setattr(paths, "PROJECTS_DIR", proj)
    return proj


@pytest.fixture(scope="module")
def registry():
    """Singleton tool registry, lazy-discovered."""
    from tools.tool_registry import registry as _registry
    _registry.discover()
    return _registry


@pytest.fixture(scope="module")
def adapter(registry) -> BaseTool:
    """The ClaudeVideoComposeTool instance, freshly registered."""
    tool = registry.get("claude_video.compose")
    assert tool is not None, (
        "claude_video.compose is not registered. Did tools/external/__init__.py "
        "get imported by tool_registry.discover()?"
    )
    return tool


@pytest.fixture
def fake_runresult_dir(tmp_path: Path) -> dict:
    """Construct a fake claude-video watch output on disk and return a
    complete `ClaudeVideoInputs.source`-shaped dict pointing at it.

    Frame files are intentionally small (one-byte .jpg) so the test does
    not allocate gigabytes. They satisfy the path-shape contract; the
    semantic content of each frame is irrelevant — no rendering tool
    reads them in this test (the smoke path only checks the copy).
    """
    frames = tmp_path / "frames"
    frames.mkdir()
    for n in (1, 2, 3):
        (frames / f"frame_{n:04d}.jpg").write_bytes(b"\xff\xd8\xff\xe0")

    masks = tmp_path / "masks"
    masks.mkdir()
    (masks / "mask_0001.png").write_bytes(b"\x89PNG")

    video = tmp_path / "source.mp4"
    video.write_bytes(b"\x00\x00\x00\x18ftypmp42")

    vtt = tmp_path / "transcript.en.vtt"
    vtt.write_text(
        "WEBVTT\n\n"
        "00:00:00.000 --> 00:00:02.840\n"
        "Big Buck Bunny is a short animated film.\n",
        encoding="utf-8",
    )

    return {
        "video_id": "v_abc123def456",
        "frames_dir": str(frames),
        "masks_dir": str(masks),
        "vtt_path": str(vtt),
        "video_path": str(video),
        "duration_seconds": 10.04,
        "transcript_segments": [
            {"start": 0.00, "end": 2.84, "text": "Big Buck Bunny is a short animated film."},
            {"start": 2.84, "end": 5.92, "text": "It is made using free and open source software."},
        ],
    }


@pytest.fixture
def valid_inputs(fake_runresult_dir) -> dict:
    """A minimal valid input dict (no STUB gate in this version — the real
    execute() runs immediately)."""
    return {
        "user_openid": "test_user_openid_xyz",
        "source": fake_runresult_dir,
        "pipeline": "documentary-montage",
        "style": "clean-professional",
        "extra": {"backlot_base_url": "http://stub:0"},
    }


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


def test_adapter_registered(registry) -> None:
    """``claude_video.compose`` must auto-register on ``registry.discover()``."""
    assert "claude_video.compose" in registry._tools


def test_adapter_class_decorator(adapter) -> None:
    """Concrete BaseTool subclass (not abstract) — catches the failure
    mode where ``execute()`` is missing and tool_registry's
    ``register_module`` skips the class."""
    cls = type(adapter)
    assert issubclass(cls, BaseTool)
    assert not inspect.isabstract(cls), (
        f"{cls.__name__} is abstract — BaseTool.execute() must be defined."
    )


def test_adapter_identity(adapter) -> None:
    """Identity fields are part of the contract."""
    assert adapter.name == "claude_video.compose"
    assert adapter.tier.value == "generate"
    assert adapter.capability == "external_recompose"
    assert adapter.provider == "claude_video"
    assert adapter.stability.value == "beta"
    assert adapter.execution_mode.value == "async"


def test_adapter_status(adapter) -> None:
    assert adapter.get_status().value == "available"


def test_adapter_is_not_gpu_required(registry) -> None:
    assert "claude_video.compose" not in registry.gpu_required_tools()


# ---------------------------------------------------------------------------
# Whitelist enforcement (defense in depth first layer)
# ---------------------------------------------------------------------------


ALL_ALLOWED = sorted({
    "clip-factory",
    "documentary-montage",
    "podcast-repurpose",
    "localization-dub",
    "hybrid",
    "screen-demo",
})

SHOULD_REJECT = [
    "local_diffusion", "wan_video", "hunyuan_video", "cogvideo_video",
    "ltx_video_local", "talking_head", "upscale", "face_restore",
    "video_understand", "nllb_translator", "comfyui_image", "comfyui_video",
    "podcast-reproduce",   # cross-repo typo (should be `podcast-repurpose`)
    "Clip-Factory",        # case-sensitive whitelist
    "clip factory",        # whitespace-sensitive whitelist
    "",                    # empty string
]


@pytest.mark.parametrize("pipeline_name", ALL_ALLOWED)
def test_whitelist_accepts_each_allowed(adapter, valid_inputs, pipeline_name) -> None:
    """Each allowed pipeline must NOT trigger the whitelist error."""
    inputs = dict(valid_inputs)
    inputs["pipeline"] = pipeline_name
    # valid_inputs has a real frames dir; submit will succeed — but if it
    # DOES fail, the failure must NOT be whitelist-related.
    result = adapter.execute(inputs)
    if not result.success:
        assert (result.data or {}).get("code") != "pipeline_not_in_whitelist", (
            f"allowed pipeline {pipeline_name!r} was rejected by whitelist: "
            f"{result.error}"
        )


@pytest.mark.parametrize("pipeline_name", SHOULD_REJECT)
def test_whitelist_rejects_each_disallowed(adapter, valid_inputs, pipeline_name) -> None:
    """Anything not in ALLOWED_PIPELINES must return
    ``code="pipeline_not_in_whitelist"`` per MCP_SERVER_PRD §2.6.3."""
    from tools.external.claude_video import ERROR_PIPELINE_NOT_IN_WHITELIST
    inputs = dict(valid_inputs)
    inputs["pipeline"] = pipeline_name
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_PIPELINE_NOT_IN_WHITELIST, (
        f"expected code={ERROR_PIPELINE_NOT_IN_WHITELIST!r}, "
        f"got code={(result.data or {}).get('code')!r}"
    )
    # message must enumerate the allowed set so callers can fix their input
    for name in ALL_ALLOWED:
        assert name in (result.error or ""), (
            f"missing allowed name {name!r} in: {result.error}"
        )


# ---------------------------------------------------------------------------
# Error codes — 1:1 with MCP_SERVER_PRD.md §2.6.3
# ---------------------------------------------------------------------------


def test_user_not_found_when_user_openid_empty(adapter, valid_inputs) -> None:
    from tools.external.claude_video import ERROR_USER_NOT_FOUND
    inputs = dict(valid_inputs)
    inputs["user_openid"] = ""
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_USER_NOT_FOUND


def test_user_not_found_when_user_openid_missing(adapter, valid_inputs) -> None:
    """Missing user_openid (treated like empty) must also surface
    user_not_found, NOT a generic KeyError."""
    from tools.external.claude_video import ERROR_USER_NOT_FOUND
    inputs = dict(valid_inputs)
    inputs.pop("user_openid", None)
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_USER_NOT_FOUND


def test_user_not_found_when_user_openid_whitespace(adapter, valid_inputs) -> None:
    from tools.external.claude_video import ERROR_USER_NOT_FOUND
    inputs = dict(valid_inputs)
    inputs["user_openid"] = "   "
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_USER_NOT_FOUND


def test_assets_copy_failed_on_malformed_source(adapter, valid_inputs) -> None:
    """A source that fails the `_validate_inputs` runtime check returns
    assets_copy_failed — NOT a generic KeyError later under shutil."""
    from tools.external.claude_video import ERROR_ASSETS_COPY_FAILED
    inputs = dict(valid_inputs)
    inputs["source"] = dict(inputs["source"])
    inputs["source"]["transcript_segments"] = "not-a-list"
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_ASSETS_COPY_FAILED


def test_assets_copy_failed_on_non_dir_frames_dir(adapter, valid_inputs, tmp_path) -> None:
    """If frames_dir exists but is a file (not a directory), copy fails."""
    from tools.external.claude_video import ERROR_ASSETS_COPY_FAILED
    inputs = dict(valid_inputs)
    bad_frames = tmp_path / "not_a_dir.jpg"
    bad_frames.write_bytes(b"\xff\xd8\xff\xe0")
    inputs["source"] = dict(inputs["source"])
    inputs["source"]["frames_dir"] = str(bad_frames)
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_ASSETS_COPY_FAILED


def test_video_id_unknown_when_frames_dir_gone(adapter, valid_inputs, tmp_path) -> None:
    """If frames_dir doesn't exist at all, it's a session-GC situation on
    the claude-video side — surface as video_id_unknown so the caller
    knows to re-run /watch."""
    from tools.external.claude_video import ERROR_VIDEO_ID_UNKNOWN
    inputs = dict(valid_inputs)
    inputs["source"] = dict(inputs["source"])
    inputs["source"]["frames_dir"] = str(tmp_path / "never_existed_xyz")
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_VIDEO_ID_UNKNOWN
    assert "video_id" in (result.error or "")


def test_assets_copy_failed_on_non_file_video_path(adapter, valid_inputs, tmp_path) -> None:
    """video_path supplied but missing/non-file should fail before write."""
    from tools.external.claude_video import ERROR_ASSETS_COPY_FAILED
    inputs = dict(valid_inputs)
    inputs["source"] = dict(inputs["source"])
    inputs["source"]["video_path"] = str(tmp_path / "missing.mp4")
    result = adapter.execute(inputs)
    assert result.success is False
    assert (result.data or {}).get("code") == ERROR_ASSETS_COPY_FAILED


# ---------------------------------------------------------------------------
# lip_sync hardening (defense in depth second layer)
# ---------------------------------------------------------------------------


def test_localization_dub_lip_sync_warning(adapter, projects_dir, valid_inputs) -> None:
    """localization-dub without allow_gpu_optional_tools must emit a
    lip_sync warning. Successful submit + warns."""
    inputs = dict(valid_inputs)
    inputs["pipeline"] = "localization-dub"
    result = adapter.execute(inputs)
    assert result.success is True
    warnings = (result.data or {}).get("warnings") or []
    assert any("lip_sync" in str(w) for w in warnings), (
        f"expected lip_sync warning, got: {warnings}"
    )


def test_localization_dub_with_opt_in_clean(adapter, projects_dir, valid_inputs) -> None:
    """allow_gpu_optional_tools=True suppresses the lip_sync warning."""
    inputs = dict(valid_inputs)
    inputs["pipeline"] = "localization-dub"
    inputs["extra"] = dict(inputs.get("extra") or {})
    inputs["extra"]["allow_gpu_optional_tools"] = True
    result = adapter.execute(inputs)
    assert result.success is True
    warnings = (result.data or {}).get("warnings") or []
    assert not any("lip_sync" in str(w) for w in warnings)


def test_other_pipelines_no_lip_sync_warning(adapter, projects_dir, valid_inputs) -> None:
    inputs = dict(valid_inputs)
    inputs["pipeline"] = "clip-factory"
    result = adapter.execute(inputs)
    assert result.success is True
    warnings = (result.data or {}).get("warnings") or []
    assert not any("lip_sync" in str(w) for w in warnings)


# ---------------------------------------------------------------------------
# End-to-end submit (smoke)
# ---------------------------------------------------------------------------


def test_smoke_submits_project_with_assets_copied(
    adapter, projects_dir, valid_inputs
) -> None:
    """Happy path: real execute() against tmp_path-based PROJECTS_DIR.

    Asserts the full set of side effects:
      - projects/users/<openid>/<id>/ created with the standard subdirs
      - assets/{frames,masks,video,audio} populated with copied files
      - artifacts/source_meta.json exists + carries transcript_segments
      - artifacts/source_media_review.json exists
      - checkpoint_idea.json written with status=in_progress
      - ToolResult.success=True, code='ok', status='submitted'
    """
    result = adapter.execute(valid_inputs)
    assert result.success is True, f"submit failed: {result.error}"
    assert (result.data or {}).get("status") == "submitted"
    assert (result.data or {}).get("code") == "ok"

    expected_dir = projects_dir / "users" / valid_inputs["user_openid"] / (
        valid_inputs.get("project_id") or valid_inputs["source"]["video_id"]
    )
    assert expected_dir.is_dir(), f"project dir not created: {expected_dir}"

    # Asset subdirs
    frames_dst = expected_dir / "assets" / "frames"
    assert frames_dst.is_dir()
    assert (frames_dst / "frame_0001.jpg").exists()

    masks_dst = expected_dir / "assets" / "masks"
    assert masks_dst.is_dir()
    assert (masks_dst / "mask_0001.png").exists()

    video_dst = expected_dir / "assets" / "video"
    assert video_dst.is_dir()
    assert (video_dst / "source.mp4").exists()

    audio_dst = expected_dir / "assets" / "audio"
    assert audio_dst.is_dir()
    assert (audio_dst / "transcript.en.vtt").exists()

    # Artifacts
    meta = json.loads(
        (expected_dir / "artifacts" / "source_meta.json").read_text(encoding="utf-8")
    )
    assert meta["video_id"] == valid_inputs["source"]["video_id"]
    assert meta["duration_seconds"] == 10.04
    assert meta["transcript_segments"] == valid_inputs["source"]["transcript_segments"]
    assert meta["claude_video_provenance"]["pipeline"] == "documentary-montage"
    # Paths stored in source_meta should be the COPIED paths, not the
    # fake_runresult_dir originals.
    assert meta["frames_dir"] == str(frames_dst)

    review = json.loads(
        (expected_dir / "artifacts" / "source_media_review.json").read_text(encoding="utf-8")
    )
    assert review["transcript_segments_count"] == 2

    # Checkpoint
    cp = json.loads(
        (expected_dir / "checkpoint_idea.json").read_text(encoding="utf-8")
    )
    assert cp["status"] == "in_progress"
    assert cp["pipeline_type"] == "documentary-montage"
    assert cp["stage"] == "idea"
    assert cp["metadata"]["submitted_via"] == "claude_video.compose"
    assert cp["metadata"]["video_id"] == "v_abc123def456"


def test_smoke_path_defaults_project_id_to_video_id(
    adapter, projects_dir, valid_inputs
) -> None:
    """Without explicit project_id, the directory name = source.video_id."""
    inputs = dict(valid_inputs)
    inputs.pop("project_id", None)
    result = adapter.execute(inputs)
    assert result.success is True
    assert result.data["project_id"] == valid_inputs["source"]["video_id"]


def test_smoke_path_honors_explicit_project_id(
    adapter, projects_dir, valid_inputs
) -> None:
    """Explicit project_id wins."""
    inputs = dict(valid_inputs)
    inputs["project_id"] = "custom-2026"
    result = adapter.execute(inputs)
    assert result.success is True
    assert result.data["project_id"] == "custom-2026"
    assert (
        projects_dir / "users" / valid_inputs["user_openid"] / "custom-2026"
    ).is_dir()


def test_smoke_backlot_url_uses_extra_override(adapter, projects_dir, valid_inputs) -> None:
    """``extra.backlot_base_url`` overrides the default Backlot board URL —
    lets CI / dev hosts aim at a non-`:8900` port."""
    inputs = dict(valid_inputs)
    inputs["extra"] = {"backlot_base_url": "http://my.test:12345"}
    result = adapter.execute(inputs)
    assert result.success is True
    assert result.data["backlot_url"].startswith("http://my.test:12345/backlot/")


def test_smoke_is_idempotent(adapter, projects_dir, valid_inputs) -> None:
    """Re-running on the same inputs must be safe — init_project is
    idempotent; asset copy uses dirs_exist_ok=True + files copied
    overwrite by name."""
    first = adapter.execute(valid_inputs)
    second = adapter.execute(valid_inputs)
    assert first.success is True
    assert second.success is True
    # project_id the same in both runs
    assert first.data["project_id"] == second.data["project_id"]


# ---------------------------------------------------------------------------
# Schema contract
# ---------------------------------------------------------------------------


def test_input_schema_pipeline_enum_matches(adapter) -> None:
    """JSON Schema enum for pipeline must reflect ALLOWED_PIPELINES."""
    from tools.external.claude_video import ALLOWED_PIPELINES
    info = adapter.get_info()
    enum = info["input_schema"]["properties"]["pipeline"]["enum"]
    assert sorted(enum) == sorted(ALLOWED_PIPELINES)


def test_input_schema_disallows_unknown_top_level_keys(adapter) -> None:
    info = adapter.get_info()
    assert info["input_schema"]["additionalProperties"] is False


def test_output_schema_carries_code_field(adapter) -> None:
    """Output schema must include a `code` field (per MCP_SERVER_PRD §2.6.3
    machine-readable code contract)."""
    info = adapter.get_info()
    assert "code" in info["output_schema"]["properties"]


def test_output_schema_status_enum_drops_stubbed(adapter) -> None:
    """The skeleton-only ``stubbed`` status must NOT be in the production
    output schema — would mislead anyone consuming ToolResult.data.status."""
    info = adapter.get_info()
    status_enum = info["output_schema"]["properties"]["status"]["enum"]
    assert "stubbed" not in status_enum, (
        "skeleton-only `stubbed` status leaked into the production schema"
    )
    assert "submitted" in status_enum


# ---------------------------------------------------------------------------
# Module-level helpers — exported so agents can pre-flight without
# instantiating the tool class.
# ---------------------------------------------------------------------------


def test_check_pipeline_whitelist_module_helper() -> None:
    from tools.external.claude_video import check_pipeline_whitelist
    for p in ALL_ALLOWED:
        ok, _ = check_pipeline_whitelist(p)
        assert ok is True
    for p in SHOULD_REJECT:
        ok, msg = check_pipeline_whitelist(p)
        assert ok is False
        assert "allowed whitelist" in msg


def test_compute_disabled_tools_module_helper() -> None:
    from tools.external.claude_video import (
        HARD_DISABLED_OPTIONAL_TOOLS,
        compute_disabled_tools,
    )
    assert compute_disabled_tools({}) == sorted(HARD_DISABLED_OPTIONAL_TOOLS)
    assert compute_disabled_tools({}, allow_gpu_optional_tools=True) == []
    assert "lip_sync" in compute_disabled_tools({})


def test_project_dir_for_module_helper() -> None:
    from tools.external.claude_video import project_dir_for
    assert project_dir_for("alice", "demo-1") == Path("projects/users/alice/demo-1")


def test_error_codes_match_mcp_server_prd() -> None:
    """Module-level error code constants must stay 1:1 with the codes in
    /opt/claude-video/docs/MCP_SERVER_PRD.md §2.6.3. Drift here breaks
    the BFF's translation table — the BFF maps MCP ToolError.code to its
    HTTP error field directly."""
    from tools.external.claude_video import (
        ERROR_PIPELINE_NOT_IN_WHITELIST,
        ERROR_VIDEO_ID_UNKNOWN,
        ERROR_USER_NOT_FOUND,
        ERROR_ASSETS_COPY_FAILED,
        ERROR_PIPELINE_STAGE_FAILED,
        ERROR_GPU_REQUIRED,
    )
    expected = {
        "pipeline_not_in_whitelist",
        "video_id_unknown",
        "user_not_found",
        "assets_copy_failed",
        "pipeline_stage_failed",
        "gpu_required",
    }
    actual = {
        ERROR_PIPELINE_NOT_IN_WHITELIST,
        ERROR_VIDEO_ID_UNKNOWN,
        ERROR_USER_NOT_FOUND,
        ERROR_ASSETS_COPY_FAILED,
        ERROR_PIPELINE_STAGE_FAILED,
        ERROR_GPU_REQUIRED,
    }
    assert actual == expected, (
        f"error code drift! actual={sorted(actual)} expected={sorted(expected)}"
    )
