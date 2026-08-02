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
    staged = tmp_path / "_staged"

    result = _make_staged(src.as_posix(), 3, staged)

    assert result.startswith("_staged/")
    target = staged / Path(result).name
    assert target.exists()
    assert target.read_bytes() == b"fake-mp4-bytes"


def test_file_uri_normalized_to_path(tmp_path):
    src = tmp_path / "narr.mp3"
    src.write_bytes(b"fake-mp3")
    staged = tmp_path / "_staged"

    result = _make_staged(src.as_uri(), 0, staged)

    assert result.startswith("_staged/")
    assert (staged / Path(result).name).exists()


def test_same_content_reused_without_recopy(tmp_path):
    src = tmp_path / "bg.png"
    src.write_bytes(b"png")
    staged = tmp_path / "_staged"

    first = _make_staged(src.as_posix(), 1, staged)
    second = _make_staged(src.as_posix(), 1, staged)

    assert first == second


def test_colliding_basenames_get_distinct_targets(tmp_path):
    a = tmp_path / "shot.jpg"
    b = tmp_path / "other" / "shot.jpg"
    b.parent.mkdir()
    a.write_bytes(b"AAA")
    b.write_bytes(b"BBB")
    staged = tmp_path / "_staged"

    ra = _make_staged(a.as_posix(), 0, staged)
    rb = _make_staged(b.as_posix(), 0, staged)

    assert ra != rb
    assert Path(ra).name != Path(rb).name
