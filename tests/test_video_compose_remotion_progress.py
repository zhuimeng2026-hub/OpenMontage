"""Tests for Remotion progress parsing + streaming run_command callback."""

import sys

from tools.video.video_compose import VideoCompose


def test_parse_remotion_frame_progress():
    pct = VideoCompose._parse_remotion_progress("Rendering frame 150/300")
    assert pct == 50.0


def test_parse_remotion_rendered_frame():
    pct = VideoCompose._parse_remotion_progress("Rendered frame 1/4")
    assert pct == 25.0


def test_parse_remotion_bare_percent():
    pct = VideoCompose._parse_remotion_progress("progress: 73%")
    assert pct == 73.0


def test_parse_remotion_no_progress_returns_none():
    assert VideoCompose._parse_remotion_progress("some unrelated log line") is None
    assert VideoCompose._parse_remotion_progress("") is None


def test_parse_remotion_clamps_over_100():
    pct = VideoCompose._parse_remotion_progress("Rendering frame 999/300")
    assert pct == 100.0


def test_run_command_streaming_fires_callback():
    vc = VideoCompose()
    seen = []
    # Use the current interpreter to emit two lines, exercising the streaming
    # path (on_output) end-to-end without depending on npx/Remotion.
    vc.run_command(
        [sys.executable, "-c", "print('line_one'); print('line_two')"],
        on_output=seen.append,
    )
    assert "line_one" in seen
    assert "line_two" in seen


def test_run_command_streaming_reports_failure():
    import subprocess

    vc = VideoCompose()
    raised = False
    try:
        vc.run_command(
            [sys.executable, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"],
            on_output=lambda _: None,
        )
    except subprocess.CalledProcessError as exc:
        raised = True
        assert exc.returncode == 3
        assert "boom" in (exc.detail or "")
    assert raised
