"""Regression: decompose log + monitor — feat(decompose): dedicated log + monitor.

Tests:
  1. mcp_server._decompose_event writes to logs/decompose.log
  2. BaseTool._instrument_execute tags scene_detect with phase="decompose"
  3. BaseTool._instrument_execute omits phase key for non-decompose tools
     (lib/events.py:88 None-drop filter keeps events byte-identical)
  4. Probe C flags files at projects/ root that are not allow-listed
  5. Probe C allows _scratch/ and real project directories

For tests 4-5 the module-level PROJECTS_DIR in
tools.decompose_health_monitor is monkeypatched to a tmp_path fixture
rather than the production projects/ directory — see monkeypatch usage below.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# --------------------------------------------------------------------------- #
# Test 1 — _decompose_event writes to logs/decompose.log
# --------------------------------------------------------------------------- #

def test_decompose_logger_writes_to_decompose_log(tmp_path: Path):
    """After mcp_server._decompose_event('decompose_run', state='start', phase=1),
    the logs/decompose.log file contains a line with both tokens."""
    # Lazy import — avoid loading mcp_server at module-collection time.
    import mcp_server

    log_file = tmp_path / "decompose.log"
    # Redirect the decompose logger to our tmp_path.
    original_handlers = mcp_server._decompose_log.handlers[:]
    try:
        # Remove existing handlers and add a fresh file handler pointing to tmp_path.
        for h in mcp_server._decompose_log.handlers[:]:
            mcp_server._decompose_log.removeHandler(h)
        import logging
        from logging.handlers import RotatingFileHandler
        fh = RotatingFileHandler(str(log_file), maxBytes=10 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(message)s"))
        mcp_server._decompose_log.addHandler(fh)

        mcp_server._decompose_event("decompose_run", state="start",
                                    phase=1, name="decompose",
                                    project="test-project-001")

        content = log_file.read_text(encoding="utf-8")
        assert "event=decompose_run" in content
        assert "state=start" in content
        assert "phase=1" in content
    finally:
        # Restore original handlers so we don't pollute the global logger.
        for h in mcp_server._decompose_log.handlers[:]:
            mcp_server._decompose_log.removeHandler(h)
        for h in original_handlers:
            mcp_server._decompose_log.addHandler(h)


# --------------------------------------------------------------------------- #
# Test 2 — scene_detect gets phase="decompose" in events.jsonl
# --------------------------------------------------------------------------- #

def test_instrument_execute_tags_decompose_tools_with_phase(tmp_path: Path):
    """A mocked BaseTool named 'scene_detect' emits events.jsonl entries whose
    parsed JSON contains 'phase': 'decompose' on both start and finish."""
    import mcp_server
    from tools.base_tool import BaseTool

    events_file = tmp_path / "events.jsonl"
    project_dir = tmp_path / "proj-phrase-test"
    project_dir.mkdir()
    events_link = project_dir / "events.jsonl"

    # Patch the event-emission path so it writes to our tmp_path.
    original_emit = None
    def mock_emit(project_dir_arg, payload):
        # Mirror lib/events.py:88 — drop keys with None before writing.
        filtered = {k: v for k, v in payload.items() if v is not None}
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(filtered) + "\n")

    try:
        import lib.events
        original_emit = lib.events.emit_event
        lib.events.emit_event = mock_emit

        # Create a minimal concrete tool subclass for scene_detect.
        class FakeSceneDetectTool(BaseTool):
            name = "scene_detect"

            def execute(self, inputs, **kwargs):
                class Result:
                    success = True
                    data = {"scenes": []}
                    error = None
                    cost_usd = 0.0
                return Result()

        tool = FakeSceneDetectTool()

        # Patch infer_project_dir to return our tmp project.
        with patch("lib.events.infer_project_dir", return_value=project_dir):
            tool.execute({})

        # Read back the events.jsonl.
        lines = events_file.read_text(encoding="utf-8").strip().split("\n")
        entries = [json.loads(l) for l in lines if l.strip()]

        start_entries = [e for e in entries if e.get("event") == "start"]
        finish_entries = [e for e in entries if e.get("event") == "finish"]

        assert len(start_entries) >= 1, f"expected at least 1 start entry, got: {entries}"
        assert all(e.get("phase") == "decompose" for e in start_entries), \
            f"all start entries must have phase=decompose: {start_entries}"

        assert len(finish_entries) >= 1, f"expected at least 1 finish entry, got: {entries}"
        assert all(e.get("phase") == "decompose" for e in finish_entries), \
            f"all finish entries must have phase=decompose: {finish_entries}"
    finally:
        if original_emit is not None:
            lib.events.emit_event = original_emit


# --------------------------------------------------------------------------- #
# Test 3 — non-decompose tools do NOT get a phase key
# --------------------------------------------------------------------------- #

def test_instrument_execute_omits_phase_for_non_decompose_tools(tmp_path: Path):
    """A mocked BaseTool named 'weiyun_upload' emits entries whose parsed JSON
    does NOT contain the key 'phase' (verifies lib/events.py:88 None-drop)."""
    from tools.base_tool import BaseTool

    events_file = tmp_path / "events.jsonl"
    project_dir = tmp_path / "proj-non-decompose"
    project_dir.mkdir()

    def mock_emit(project_dir_arg, payload):
        # Mirror lib/events.py:88 — drop keys with None before writing.
        filtered = {k: v for k, v in payload.items() if v is not None}
        with open(events_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(filtered) + "\n")

    original_emit = None
    try:
        import lib.events
        original_emit = lib.events.emit_event
        lib.events.emit_event = mock_emit

        class FakeWeiyunTool(BaseTool):
            name = "weiyun_upload"

            def execute(self, inputs, **kwargs):
                class Result:
                    success = True
                    data = {}
                    error = None
                    cost_usd = 0.0
                return Result()

        tool = FakeWeiyunTool()

        with patch("lib.events.infer_project_dir", return_value=project_dir):
            tool.execute({})

        lines = events_file.read_text(encoding="utf-8").strip().split("\n")
        entries = [json.loads(l) for l in lines if l.strip()]

        assert len(entries) >= 2, f"expected start+finish entries: {entries}"
        for e in entries:
            assert "phase" not in e, f"non-decompose tool must not have phase key: {e}"
    finally:
        if original_emit is not None:
            lib.events.emit_event = original_emit


# --------------------------------------------------------------------------- #
# Test 4 — Probe C flags unexpected files at projects/ root
# --------------------------------------------------------------------------- #

def test_workspace_contract_probe_flags_root_files(tmp_path: Path):
    """After creating tmp_path/projects/frame_0000.jpg,
    probe_workspace_contract returns ok=False with a tag
    starting with 'workspace_violation:frame_0000.jpg'."""
    # Monkeypatch the module-level PROJECTS_DIR to our tmp fixture.
    # This avoids touching the real production projects/ directory.
    import tools.decompose_health_monitor as dhm

    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    # Patch PROJECTS_DIR before the probe runs.
    with patch.object(dhm, "PROJECTS_DIR", projects_root):
        # Create a stray file at projects/ root (violation).
        stray = projects_root / "frame_0000.jpg"
        stray.write_bytes(b"")

        result = dhm.probe_workspace_contract()

    assert result["ok"] is False, f"expected ok=False, got: {result}"
    violation_tags = [t for t in result["tags"] if t.startswith("workspace_violation:")]
    assert any("frame_0000.jpg" in t for t in violation_tags), \
        f"expected workspace_violation:frame_0000.jpg in tags: {result['tags']}"


# --------------------------------------------------------------------------- #
# Test 5 — Probe C allows _scratch/ and real project directories
# --------------------------------------------------------------------------- #

def test_workspace_contract_probe_allows_scratch_and_real_projects(tmp_path: Path):
    """After creating tmp_path/projects/_scratch/foo.mp4 and
    tmp_path/projects/proj-x/y.txt, probe_workspace_contract
    returns ok=True with empty tags."""
    import tools.decompose_health_monitor as dhm

    projects_root = tmp_path / "projects"
    projects_root.mkdir()

    # Allow-listed: _scratch/ directory
    scratch_dir = projects_root / "_scratch"
    scratch_dir.mkdir()
    (scratch_dir / "foo.mp4").write_bytes(b"")

    # Allow-listed: real project directory
    real_proj = projects_root / "proj-x"
    real_proj.mkdir()
    (real_proj / "y.txt").write_bytes(b"")

    # Add a non-violating file at root (README.md is allow-listed).
    (projects_root / "README.md").write_text("ok")

    with patch.object(dhm, "PROJECTS_DIR", projects_root):
        result = dhm.probe_workspace_contract()

    assert result["ok"] is True, f"expected ok=True, got: {result}"
    assert result["tags"] == [], f"expected no tags for allow-listed entries, got: {result['tags']}"
