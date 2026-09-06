"""Shared relay (new-api compatible 中转站) client regression coverage.

Tests ``tools/video/_relay.py`` — ``generate_via_relay`` and the ``RelayError``
exception — entirely with mocked ``requests.post`` / ``requests.get``. No
network. The relay module imports ``requests`` lazily inside each function
(``import requests``), which returns the same module object, so patching the
``requests`` module attributes directly intercepts every HTTP call.

Covered behaviors:
  - validation gating (base_url / api_key / prompt)
  - submit -> poll -> download happy path, including the format default
  - poll failure (task ``failed``), submit HTTP error, poll timeout
  - succeeded-but-missing-url
  - image_to_video payload (``image`` key) and metadata passthrough
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from tools.video._relay import RelayError, generate_via_relay

BASE = "https://relay.example.com"
API_KEY = "sk-test"
MODEL = "kling"
PROMPT = "a cat surfing"


def _json_response(payload, status_code: int = 200, text: str | None = None) -> MagicMock:
    """Build a fake requests.Response whose .json() returns ``payload``."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    resp.text = text if text is not None else str(payload)
    return resp


def _fake_get(video_url: str = "https://cdn.example.com/x.mp4"):
    """Return a requests.get fake: first call = poll, second call = download.

    The poll endpoint URL contains ``video/generations`` (the relay poll URL is
    ``{base}/v1/video/generations/{task_id}``); the download URL does not. The
    poll response deliberately omits ``format`` so the ``mp4`` default is
    exercised.
    """

    def fake_get(url, **kwargs):
        if "video/generations" in url:  # poll
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "task_id": "t1",
                "status": "succeeded",
                "url": video_url,
            }
            return resp
        # video download
        resp = MagicMock()
        resp.status_code = 200
        resp.content = b"fakevideo"
        resp.text = "fakevideo"
        return resp

    return fake_get


# ---------------------------------------------------------------------------
# Validation gating
# ---------------------------------------------------------------------------


def test_missing_base_url(tmp_path):
    with pytest.raises(RelayError, match="VIDEO_RELAY_BASE_URL"):
        generate_via_relay(
            base_url="",
            api_key=API_KEY,
            model=MODEL,
            prompt=PROMPT,
            output_path=tmp_path / "out.mp4",
        )


def test_missing_api_key(tmp_path):
    with pytest.raises(RelayError, match="VIDEO_RELAY_API_KEY"):
        generate_via_relay(
            base_url=BASE,
            api_key="",
            model=MODEL,
            prompt=PROMPT,
            output_path=tmp_path / "out.mp4",
        )


def test_empty_prompt(tmp_path):
    with pytest.raises(RelayError, match="prompt is required"):
        generate_via_relay(
            base_url=BASE,
            api_key=API_KEY,
            model=MODEL,
            prompt="   ",
            output_path=tmp_path / "out.mp4",
        )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_downloads_video(tmp_path):
    output_path = tmp_path / "out" / "video.mp4"
    post = _json_response({"task_id": "t1", "status": "queued"})

    with patch("requests.post", return_value=post), patch(
        "requests.get", side_effect=_fake_get()
    ):
        result = generate_via_relay(
            base_url=BASE,
            api_key=API_KEY,
            model=MODEL,
            prompt=PROMPT,
            output_path=output_path,
        )

    assert result["gateway"] == "new-api"
    assert result["task_id"] == "t1"
    assert result["model"] == MODEL
    assert result["output_path"] == str(output_path)
    # poll response omitted "format" -> default must be "mp4"
    assert result["format"] == "mp4"
    assert output_path.exists()
    assert output_path.read_bytes() == b"fakevideo"


# ---------------------------------------------------------------------------
# Poll / submit failures
# ---------------------------------------------------------------------------


def test_poll_failed_raises_error_with_message(tmp_path):
    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "task_id": "t1",
            "status": "failed",
            "error": {"message": "boom"},
        }
        return resp

    post = _json_response({"task_id": "t1", "status": "queued"})
    with patch("requests.post", return_value=post), patch(
        "requests.get", side_effect=fake_get
    ):
        with pytest.raises(RelayError, match="boom"):
            generate_via_relay(
                base_url=BASE,
                api_key=API_KEY,
                model=MODEL,
                prompt=PROMPT,
                output_path=tmp_path / "out.mp4",
            )


def test_submit_rejected_on_400(tmp_path):
    post = _json_response({}, status_code=400, text="bad request body")
    with patch("requests.post", return_value=post):
        with pytest.raises(RelayError, match="submit rejected"):
            generate_via_relay(
                base_url=BASE,
                api_key=API_KEY,
                model=MODEL,
                prompt=PROMPT,
                output_path=tmp_path / "out.mp4",
            )


def test_poll_times_out(tmp_path):
    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"task_id": "t1", "status": "queued"}
        return resp

    post = _json_response({"task_id": "t1", "status": "queued"})
    with patch("requests.post", return_value=post), patch(
        "requests.get", side_effect=fake_get
    ):
        with pytest.raises(RelayError, match="timed out"):
            generate_via_relay(
                base_url=BASE,
                api_key=API_KEY,
                model=MODEL,
                prompt=PROMPT,
                output_path=tmp_path / "out.mp4",
                poll_timeout=0.1,
                poll_interval=0.01,
            )


def test_succeeded_without_url_raises_error(tmp_path):
    def fake_get(url, **kwargs):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"task_id": "t1", "status": "succeeded"}
        return resp

    post = _json_response({"task_id": "t1", "status": "queued"})
    with patch("requests.post", return_value=post), patch(
        "requests.get", side_effect=fake_get
    ):
        with pytest.raises(RelayError, match="no url"):
            generate_via_relay(
                base_url=BASE,
                api_key=API_KEY,
                model=MODEL,
                prompt=PROMPT,
                output_path=tmp_path / "out.mp4",
            )


# ---------------------------------------------------------------------------
# Payload construction
# ---------------------------------------------------------------------------


def test_image_to_video_without_image_url_raises(tmp_path):
    with pytest.raises(RelayError, match="image_url"):
        generate_via_relay(
            base_url=BASE,
            api_key=API_KEY,
            model=MODEL,
            prompt=PROMPT,
            operation="image_to_video",
            output_path=tmp_path / "out.mp4",
        )


def test_image_to_video_posts_image_key(tmp_path):
    image_url = "https://img.example.com/x.png"
    post = _json_response({"task_id": "t1", "status": "queued"})

    with patch("requests.post", return_value=post) as mock_post, patch(
        "requests.get", side_effect=_fake_get()
    ):
        generate_via_relay(
            base_url=BASE,
            api_key=API_KEY,
            model=MODEL,
            prompt=PROMPT,
            operation="image_to_video",
            image_url=image_url,
            output_path=tmp_path / "out.mp4",
        )

    submitted = mock_post.call_args.kwargs["json"]
    assert submitted["image"] == image_url


def test_metadata_passed_through(tmp_path):
    metadata = {"prompt_tags": ["unit"], "source": "test_relay_shared"}
    post = _json_response({"task_id": "t1", "status": "queued"})

    with patch("requests.post", return_value=post) as mock_post, patch(
        "requests.get", side_effect=_fake_get()
    ):
        generate_via_relay(
            base_url=BASE,
            api_key=API_KEY,
            model=MODEL,
            prompt=PROMPT,
            metadata=metadata,
            output_path=tmp_path / "out.mp4",
        )

    submitted = mock_post.call_args.kwargs["json"]
    assert submitted["metadata"] == metadata
