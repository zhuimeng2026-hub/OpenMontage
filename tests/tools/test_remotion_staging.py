"""Unit tests for Remotion asset staging in video_compose.

These test the pure-Python `_stage_remotion_asset` helper in isolation
(no Remotion render / headless Chrome needed). They lock in the contract
that every local-resource field (cut sources, background image/video, anime
images, narration/music) is staged into `public/_staged/` and rewritten to a
staticFile()-loadable relative path — the fix for the `file://`-blocked
"Not allowed to load local resource" failure in headless Chrome.
"""
from __future__ import annotations

from pathlib import Path

from tools.video.video_compose import VideoCompose


def _make_staged(path: str, idx: int, staged_dir: Path) -> str:
    return VideoCompose()._stage_remotion_asset(path, idx, staged_dir)


def _job_staged(tmp_path: Path) -> Path:
    """New contract: staged_dir is the per-job subdir `_staged/<job_id>`.

    The helper derives the public-relative return path from staged_dir's
    parent name + its own name, so a per-job subdir under `_staged` yields
    ``_staged/<job_id>/<name>`` (loadable via staticFile()).
    """
    return tmp_path / "_staged" / "job-1"


def test_remote_uris_pass_through_unstaged(tmp_path):
    for uri in (
        "http://example.com/a.mp4",
        "https://cdn.example.com/img.jpg",
        "data:image/png;base64,iVBORw0KGgo=",
    ):
        assert _make_staged(uri, 0, tmp_path) == uri


def test_missing_file_passes_through(tmp_path):
    assert _make_staged("/no/such/file.mp4", 0, tmp_path) == "/no/such/file.mp4"


def test_local_file_staged_to_public_relative(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fake-mp4-bytes")
    staged = _job_staged(tmp_path)

    result = _make_staged(src.as_posix(), 3, staged)

    assert result.startswith("_staged/")
    target = staged / Path(result).name
    assert target.exists()
    assert target.read_bytes() == b"fake-mp4-bytes"


def test_return_path_includes_job_subdir(tmp_path):
    src = tmp_path / "clip.mp4"
    src.write_bytes(b"fake-mp4-bytes")
    staged = _job_staged(tmp_path)

    result = _make_staged(src.as_posix(), 3, staged)

    # 契约：返回 `_staged/<job_id>/<name>`，staticFile() 才能解析到子目录。
    assert result.startswith("_staged/job-1/")
    assert (staged / Path(result).name).exists()


def test_file_uri_normalized_to_path(tmp_path):
    src = tmp_path / "narr.mp3"
    src.write_bytes(b"fake-mp3")
    staged = _job_staged(tmp_path)

    result = _make_staged(src.as_uri(), 0, staged)

    assert result.startswith("_staged/")
    assert (staged / Path(result).name).exists()


def test_same_content_reused_without_recopy(tmp_path):
    src = tmp_path / "bg.png"
    src.write_bytes(b"png")
    staged = _job_staged(tmp_path)

    first = _make_staged(src.as_posix(), 1, staged)
    second = _make_staged(src.as_posix(), 1, staged)

    assert first == second


def test_colliding_basenames_get_distinct_targets(tmp_path):
    a = tmp_path / "shot.jpg"
    b = tmp_path / "other" / "shot.jpg"
    b.parent.mkdir()
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    staged = _job_staged(tmp_path)

    ra = _make_staged(a.as_posix(), 0, staged)
    rb = _make_staged(b.as_posix(), 0, staged)

    assert ra != rb
    assert Path(ra).name != Path(rb).name
