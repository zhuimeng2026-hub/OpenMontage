"""Regression: frame_sampler output_dir workspace-contract guard.

Bug: the Tauri / mclaw-demo pipeline passed ``output_dir="projects"`` to
``frame_sampler``, and the tool accepted the bare path. FFmpeg then dumped
24 frames directly into ``<repo>/projects/`` (the repo's top-level projects
directory, NOT inside any project subdirectory). The Backlot board could
not attribute those frames to any project, and the
``decompose_health_monitor`` workspace-contract probe flagged them as 24
``workspace_violation`` entries — turning the decompose path's status into
FAULT and making the front-end board show "abnormal" / missing artifacts
for ``mclaw-demo``.

Fix: ``tools/analysis/frame_sampler.py:_validate_output_dir`` now refuses
to write outside ``projects/<project-id>/...`` or
``projects/_scratch/<category>/...``. These tests pin the rule.

Each test monkeypatches the module-level ``_WORKSPACE_PROJECT_ROOT`` to a
tmp_path fixture so it does not touch the real repo.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _patch_workspace(monkeypatch, tmp_path: Path, projects_root: Path) -> None:
    """Redirect frame_sampler's workspace anchor to a tmp projects/ root."""
    monkeypatch.setattr(
        "tools.analysis.frame_sampler._WORKSPACE_PROJECT_ROOT",
        projects_root,
    )


# --------------------------------------------------------------------------- #
# Test 1 — unit: bare "projects" is rejected
# --------------------------------------------------------------------------- #


def test_validate_output_dir_rejects_bare_projects_root(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An output_dir that resolves to the bare projects/ root is rejected.

    This is the exact shape of the regression:
    ``output_dir="projects"`` from the Tauri client (CWD = repo root) landed
    at ``<repo>/projects/`` and dumped 24 stray frames there.
    """
    import tools.analysis.frame_sampler as fs

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _patch_workspace(monkeypatch, tmp_path, projects_root)

    # Caller passes a path that resolves to the bare projects/ root.
    bad = projects_root  # output_dir="projects" → Path("projects").resolve() == projects_root

    err = fs._validate_output_dir(bad)
    assert err is not None, "bare projects/ root must be rejected"
    assert "workspace contract" in err
    assert "projects/<project-id>" in err


# --------------------------------------------------------------------------- #
# Test 2 — unit: output_dir outside projects/ is rejected
# --------------------------------------------------------------------------- #


def test_validate_output_dir_rejects_path_outside_projects(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An output_dir that resolves outside projects/ is rejected.

    Anything outside projects/ is invisible to the Backlot board (CLAUDE.md
    invariant 5), so the guard rejects it outright.
    """
    import tools.analysis.frame_sampler as fs

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _patch_workspace(monkeypatch, tmp_path, projects_root)

    bad = tmp_path / "elsewhere" / "frames"
    bad.mkdir(parents=True)

    err = fs._validate_output_dir(bad)
    assert err is not None
    assert "workspace contract" in err
    assert "outside" in err


# --------------------------------------------------------------------------- #
# Test 3 — unit: projects/<project-id>/... is allowed
# --------------------------------------------------------------------------- #


def test_validate_output_dir_allows_real_project(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An output_dir under projects/<project-id>/ passes the guard."""
    import tools.analysis.frame_sampler as fs

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _patch_workspace(monkeypatch, tmp_path, projects_root)

    ok_path = projects_root / "mclaw-demo" / "artifacts" / "keyframes"
    ok_path.mkdir(parents=True)

    err = fs._validate_output_dir(ok_path)
    assert err is None, f"real project path must be allowed, got: {err}"


# --------------------------------------------------------------------------- #
# Test 4 — unit: projects/_scratch/<category>/... is allowed
# --------------------------------------------------------------------------- #


def test_validate_output_dir_allows_scratch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """An output_dir under projects/_scratch/<category>/ passes the guard.

    CLAUDE.md invariant 5 explicitly blesses ``projects/_scratch/<category>/``
    for outputs with no real project (smoke-test TTS, ad-hoc renders, etc.).
    """
    import tools.analysis.frame_sampler as fs

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _patch_workspace(monkeypatch, tmp_path, projects_root)

    ok_path = projects_root / "_scratch" / "smoke" / "frames"
    ok_path.mkdir(parents=True)

    err = fs._validate_output_dir(ok_path)
    assert err is None, f"_scratch path must be allowed, got: {err}"


# --------------------------------------------------------------------------- #
# Test 5 — integration: FrameSampler.execute rejects bad output_dir early
# --------------------------------------------------------------------------- #


def test_execute_rejects_bare_projects_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Calling FrameSampler().execute() with output_dir='projects' returns
    a ToolResult(success=False) and the message mentions the workspace
    contract. No ffmpeg invocation is attempted.
    """
    import tools.analysis.frame_sampler as fs

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _patch_workspace(monkeypatch, tmp_path, projects_root)

    # A real input file (the existence check fires before the guard).
    input_file = projects_root / "mclaw-demo" / "source.mp4"
    input_file.parent.mkdir(parents=True)
    input_file.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70")  # 8-byte fake MP4

    tool = fs.FrameSampler()
    result = tool.execute({
        "input_path": str(input_file),
        "strategy": "interval",
        "interval_seconds": 5.0,
        "output_dir": "projects",  # ← the regression: bare root
    })

    assert result.success is False
    assert "workspace contract" in (result.error or "")
    # The bad path must NOT have been created on disk.
    assert not projects_root.iterdir() or all(
        p.name in {"mclaw-demo"} for p in projects_root.iterdir()
    )


# --------------------------------------------------------------------------- #
# Test 6 — integration: FrameSampler.execute accepts a real-project output_dir
# --------------------------------------------------------------------------- #


def test_execute_accepts_real_project_output_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Calling FrameSampler().execute() with output_dir under a real
    project passes the workspace guard. (ffmpeg may then fail because the
    input is 8 bytes of fake MP4 — that's fine; the guard accepted.)
    """
    import tools.analysis.frame_sampler as fs

    projects_root = tmp_path / "projects"
    projects_root.mkdir()
    _patch_workspace(monkeypatch, tmp_path, projects_root)

    input_file = projects_root / "mclaw-demo" / "source.mp4"
    input_file.parent.mkdir(parents=True)
    input_file.write_bytes(b"\x00\x00\x00\x1c\x66\x74\x79\x70")

    out_dir = projects_root / "mclaw-demo" / "artifacts" / "keyframes"
    out_dir.mkdir(parents=True)

    tool = fs.FrameSampler()
    # We don't assert success=True here — the fake 8-byte input will cause
    # ffmpeg to fail. We DO assert that the failure is NOT the workspace
    # contract error, i.e. the guard let it through.
    result = tool.execute({
        "input_path": str(input_file),
        "strategy": "interval",
        "interval_seconds": 5.0,
        "output_dir": str(out_dir),
    })

    assert result.success is False  # fake mp4 → ffmpeg fails
    assert "workspace contract" not in (result.error or ""), (
        f"guard should have accepted real project path, got: {result.error}"
    )