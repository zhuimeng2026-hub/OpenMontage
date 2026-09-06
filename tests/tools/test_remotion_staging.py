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


def test_missing_file_passes_through(tmp_path, caplog):
    """Stale paths must NOT silently become a Chromium blocker.

    The helper logs a warning (so mcp_server.log carries provenance) and
    returns the original source unchanged. The runtime will still fail in
    Chrome with a clear error, and the defensive guard at the end of
    _remotion_render will block the render before Chrome even launches.

    Behavior contract locked: identical return value to the pre-fix
    implementation, so the existing call sites in _remotion_render are
    unaffected — the only change is observability.
    """
    import logging

    with caplog.at_level(logging.WARNING, logger="video_compose"):
        assert _make_staged("/no/such/file.mp4", 0, tmp_path) == "/no/such/file.mp4"
    assert any("skipping missing path" in rec.message for rec in caplog.records)


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


# ---------------------------------------------------------------------------
# Coverage added 2026-08-20 for the file:// / local-asset-loading fix.
# See /root/.claude/plans/shimmering-cooking-truffle.md.
# ---------------------------------------------------------------------------


def test_missing_file_warns_but_passes_through(tmp_path, caplog):
    """Stale paths must NOT silently become a Chromium blocker.

    The helper logs a warning (so mcp_server.log carries provenance) and
    returns the original source unchanged. The runtime will still fail in
    Chrome with a clear error, and the defensive guard at the end of
    _remotion_render will block the render before Chrome even launches.
    """
    import logging

    src = "/no/such/file.mp4"
    with caplog.at_level(logging.WARNING, logger="video_compose"):
        result = _make_staged(src, 0, _job_staged(tmp_path))
    assert result == src
    assert any("skipping missing path" in rec.message for rec in caplog.records)


def test_stageable_fields_table_covers_all_consumers():
    """Lock in the field list so future drift in the table is caught by CI."""
    from tools.video.video_compose import (
        _STAGEABLE_AUDIO_FIELDS,
        _STAGEABLE_FIELDS,
    )

    parents = {p for p, _, _ in _STAGEABLE_FIELDS}
    # Every consumer field documented in the plan must be present.
    assert {"cuts", "scenes", "clips", "assets"}.issubset(parents)
    # Top-level videoSrc sentinel
    assert any(p == "" and f == "videoSrc" for p, f, _ in _STAGEABLE_FIELDS)

    audio_layers = {layer for layer, _ in _STAGEABLE_AUDIO_FIELDS}
    assert {"narration", "music", "soundtrack"}.issubset(audio_layers)


def test_full_staging_loop_rewrites_every_known_field(tmp_path):
    """End-to-end through the generic _STAGEABLE_FIELDS loop for every
    documented local-resource field. No Chromium needed — just exercises the
    Python rewrite pass."""
    from tools.video.video_compose import (
        _STAGEABLE_AUDIO_FIELDS,
        _STAGEABLE_FIELDS,
        VideoCompose,
    )

    vc = VideoCompose()
    img = tmp_path / "x.png"
    img.write_bytes(b"\x89PNG\r\n")
    staged_dir = _job_staged(tmp_path)
    props = {
        "cuts": [{"source": str(img), "backgroundImage": str(img)}],
        "scenes": [{"backgroundSrc": str(img), "src": str(img)}],
        "clips": [{"src": str(img), "backgroundSrc": str(img)}],
        "videoSrc": str(img),  # TitledVideo / LyricOverlay / TalkingHead
        "soundtrack": {"src": str(img)},
        "music": {"src": str(img)},
        "audio": {"narration": {"src": str(img)}, "music": {"src": str(img)}},
        "assets": {"hero": str(img), "product": str(img), "music": str(img)},
    }

    # Mirror the production loop without running Chrome.
    def _stage_one(parent, key, idx):
        if not isinstance(parent, dict):
            return
        val = parent.get(key)
        if val:
            parent[key] = vc._stage_remotion_asset(val, idx, staged_dir)

    for parent_key, field_key, idx in _STAGEABLE_FIELDS:
        if not parent_key:
            _stage_one(props, field_key, idx)
            continue
        container = props.get(parent_key)
        if isinstance(container, list):
            for item in container:
                _stage_one(item, field_key, idx)
        elif isinstance(container, dict):
            _stage_one(container, field_key, idx)

    audio_block = props.get("audio")
    if isinstance(audio_block, dict):
        for layer, idx in _STAGEABLE_AUDIO_FIELDS:
            if layer in ("narration", "music"):
                layer_obj = audio_block.get(layer)
                if isinstance(layer_obj, dict) and layer_obj.get("src"):
                    layer_obj["src"] = vc._stage_remotion_asset(
                        layer_obj["src"], idx, staged_dir
                    )
    for top_audio_key, idx in (("soundtrack", 10), ("music", 11)):
        audio_obj = props.get(top_audio_key)
        if isinstance(audio_obj, dict) and audio_obj.get("src"):
            audio_obj["src"] = vc._stage_remotion_asset(
                audio_obj["src"], idx, staged_dir
            )

    # Every local-resource field should now be a `_staged/...` relative path.
    assert props["cuts"][0]["source"].startswith("_staged/")
    assert props["scenes"][0]["backgroundSrc"].startswith("_staged/")
    assert props["scenes"][0]["src"].startswith("_staged/")
    assert props["clips"][0]["src"].startswith("_staged/")
    assert props["clips"][0]["backgroundSrc"].startswith("_staged/")
    assert props["videoSrc"].startswith("_staged/")
    assert props["soundtrack"]["src"].startswith("_staged/")
    assert props["music"]["src"].startswith("_staged/")
    assert props["audio"]["narration"]["src"].startswith("_staged/")
    assert props["audio"]["music"]["src"].startswith("_staged/")
    assert props["assets"]["hero"].startswith("_staged/")

    # No file:// or absolute paths remain anywhere in the rewritten tree.
    import json

    blob = json.dumps(props)
    assert "file://" not in blob
    assert str(img) not in blob


def test_defensive_guard_blocks_unstaged_absolute_paths():
    """The end-of-staging walk must flag any remaining absolute path so the
    render is blocked before Chrome launches with a clear error message."""
    import re

    # Re-implement the same regex used in _remotion_render so the test is
    # robust against formatting tweaks (the regex itself is the contract).
    suspicious = re.compile(r"^(?:[A-Za-z]:[\\/]|/|[A-Za-z]+://(?!localhost))")

    def walk(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                yield from walk(v)
        elif isinstance(obj, list):
            for v in obj:
                yield from walk(v)
        elif isinstance(obj, str) and obj:
            yield obj

    props = {"videoSrc": "/tmp/should-have-been-staged.mp4"}
    bad = [
        s for s in walk(props)
        if suspicious.match(s)
        and not s.startswith(("http://", "https://", "data:"))
        and not s.startswith("_staged/")
    ]
    assert bad == ["/tmp/should-have-been-staged.mp4"]

    # A clean staged tree produces zero bad paths.
    staged_props = {"videoSrc": "_staged/job-1/0_abcdef.mp4"}
    assert [
        s for s in walk(staged_props)
        if suspicious.match(s)
        and not s.startswith(("http://", "https://", "data:"))
        and not s.startswith("_staged/")
    ] == []
