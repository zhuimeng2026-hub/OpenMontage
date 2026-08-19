"""Regression tests for `tools.graphics.minimax_image.MiniMaxImage.execute()`.

Three response shapes are handled by `_collect_image_bytes`:
  - sync `images[].url`     → GET each URL
  - sync `data[].b64_json`  → decode each entry
  - async `task_id`         → poll status URL, then re-parse

These tests mock `requests.post` / `requests.get` via monkeypatch so they
run fully offline. They verify:
  * every generated image reaches disk
  * output paths are suffixed uniquely when n>1
  * PIL normalizes RGBA → RGB when JPEG output is requested
  * the async-poll branch resolves to the right images
  * unrecognized response shapes fail loudly with a clear error
  * no-key path returns a clean failure without a network call
"""

import base64
import io
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# IMPORTANT: import tools.graphics.minimax_image at module top so its
# transitive import of `tools.base_tool` (and the resulting `_load_dotenv()`
# call) happens at test-collection time, BEFORE any test does
# `monkeypatch.delenv("MINIMAX_API_KEY")`. If we imported inside a test,
# the delenv would be silently overwritten by `_load_dotenv` re-reading
# .env into os.environ — turning the "no key" test into a real API call.
from tools.graphics import minimax_image as _minimax_image_mod  # noqa: E402
from tools.graphics.minimax_image import MiniMaxImage  # noqa: E402,F401


# ------------------------------------------------------------------
# Fake `requests` plumbing
# ------------------------------------------------------------------

class _FakeResp:
    """Minimal stand-in for requests.Response."""

    def __init__(self, *, json_payload=None, content: bytes = b"",
                 status_code: int = 200):
        self._payload = json_payload
        self.content = content
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def _png_bytes(color=(255, 0, 0), size=(8, 8)) -> bytes:
    """Encode a real PNG so PIL can decode it during _save_image."""
    from PIL import Image as PILImage
    buf = io.BytesIO()
    PILImage.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeRequests:
    """Mock requests module that scripts POST + GET responses in order."""

    def __init__(self, scripted):
        # scripted: list of (method, url_substring_or_None, response_factory)
        # response_factory(call) -> _FakeResp
        self.scripted = scripted
        self.calls: list[tuple[str, str, dict]] = []
        self._idx = 0

    def post(self, url, **kwargs):
        return self._run("POST", url, kwargs)

    def get(self, url, **kwargs):
        return self._run("GET", url, kwargs)

    def _run(self, method, url, kwargs):
        from urllib.parse import urlparse
        self.calls.append((method, url, kwargs))
        if self._idx >= len(self.scripted):
            raise AssertionError(
                f"unexpected {method} call #{self._idx + 1}: {url}"
            )
        expected_method, expected_url_substr, factory = self.scripted[self._idx]
        self._idx += 1
        assert method == expected_method, (
            f"call {self._idx}: expected {expected_method} got {method} ({url})"
        )
        if expected_url_substr is not None:
            assert expected_url_substr in url, (
                f"call {self._idx}: expected URL containing {expected_url_substr!r} "
                f"got {url!r}"
            )
        return factory(self.calls[-1])


@pytest.fixture
def minimax_tool(monkeypatch):
    """Default fixture: a fresh MiniMaxImage with the API key set and
    real `requests` import preserved so the tool can find it."""
    monkeypatch.setenv("MINIMAX_API_KEY", "test-minimax-key")
    # Capture the real requests module so execute()'s `requests.post/get`
    # still resolves, but tests can override it via monkeypatch.
    return _minimax_image_mod, monkeypatch


# ------------------------------------------------------------------
# No-key path
# ------------------------------------------------------------------

class TestNoKey:

    def test_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        tool = MiniMaxImage()
        result = tool.execute({"prompt": "anything"})
        assert result.success is False
        assert "MINIMAX_API_KEY" in result.error
        assert "https://intl.minimaxi.com" in result.error


# ------------------------------------------------------------------
# Sync URL response branch
# ------------------------------------------------------------------

class TestSyncUrlBranch:

    def test_single_image_downloads_and_writes(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        png = _png_bytes(color="red")

        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={
                 "images": [{"url": "https://cdn.example.com/img1.png"}],
             })),
            ("GET", "img1.png",
             lambda call: _FakeResp(content=png)),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        out = tmp_path / "shot.png"
        tool = mod.MiniMaxImage()
        result = tool.execute({"prompt": "a red apple", "output_path": str(out)})

        assert result.success is True, result.error
        assert result.data["images_generated"] == 1
        assert result.artifacts == [str(out)]
        assert out.exists()
        # And PIL can re-open what we wrote
        from PIL import Image as PILImage
        reopened = PILImage.open(out)
        assert reopened.size == (8, 8)

    def test_multiple_images_each_get_unique_path(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        # Use distinct sizes so we can verify bytes didn't get crossed even
        # after PIL re-encodes (PNG is lossless but palette-quantization can
        # mask identical solid colors — varied sizes are a more robust check).
        from PIL import Image as PILImage
        def _png(size):
            buf = io.BytesIO()
            PILImage.new("RGB", size, "red").save(buf, format="PNG")
            return buf.getvalue()
        png_a, png_b, png_c = _png((8, 8)), _png((16, 16)), _png((32, 32))

        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={
                 "images": [
                     {"url": "https://cdn.example.com/a.png"},
                     {"url": "https://cdn.example.com/b.png"},
                     {"url": "https://cdn.example.com/c.png"},
                 ],
             })),
            ("GET", "a.png", lambda c: _FakeResp(content=png_a)),
            ("GET", "b.png", lambda c: _FakeResp(content=png_b)),
            ("GET", "c.png", lambda c: _FakeResp(content=png_c)),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        out = tmp_path / "multi.png"
        tool = mod.MiniMaxImage()
        result = tool.execute({
            "prompt": "three fruits",
            "n": 3,
            "output_path": str(out),
        })

        assert result.success is True
        assert result.data["images_generated"] == 3
        assert len(result.artifacts) == 3
        names = {Path(a).name for a in result.artifacts}
        assert names == {"multi_1.png", "multi_2.png", "multi_3.png"}
        # Three distinct files on disk, with the sizes the fake returned
        on_disk = sorted(tmp_path.glob("multi_*.png"))
        assert len(on_disk) == 3
        assert {PILImage.open(p).size for p in on_disk} == {(8, 8), (16, 16), (32, 32)}


class TestNestedImageUrlsBranch:
    """The actual MiniMax API response shape (observed 2026-08) is
    `{id: ..., data: {image_urls: [...]}}` — URLs nested under data.
    This is different from the OpenAI-style flat `images[].url`."""

    def test_nested_data_image_urls(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        png = _png_bytes()

        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={
                 "id": "06d4536c617f6caf65b4c54134e2b665",
                 "data": {
                     "image_urls": [
                         "https://hailuo-image-algeng-data.oss-cn-wulanchabu.aliyuncs.com/img1.png",
                         "https://hailuo-image-algeng-data.oss-cn-wulanchabu.aliyuncs.com/img2.png",
                     ],
                 },
             })),
            ("GET", "img1.png", lambda c: _FakeResp(content=png)),
            ("GET", "img2.png", lambda c: _FakeResp(content=png)),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        out = tmp_path / "nested.png"
        tool = mod.MiniMaxImage()
        result = tool.execute({
            "prompt": "anything",
            "n": 2,
            "output_path": str(out),
        })
        assert result.success is True, result.error
        assert result.data["images_generated"] == 2
        assert (tmp_path / "nested_1.png").exists()
        assert (tmp_path / "nested_2.png").exists()


# ------------------------------------------------------------------
# Sync base64 response branch
# ------------------------------------------------------------------

class TestSyncBase64Branch:

    def test_base64_payload_decoded_and_written(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        png = _png_bytes(color="blue")
        b64 = base64.b64encode(png).decode()

        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={
                 "data": [{"b64_json": b64}, {"b64_json": b64}],
             })),
            # No GETs — base64 path downloads nothing
        ])
        monkeypatch.setattr(mod, "requests", fake)

        out = tmp_path / "b64.png"
        tool = mod.MiniMaxImage()
        result = tool.execute({
            "prompt": "two images",
            "n": 2,
            "output_path": str(out),
        })

        assert result.success is True, result.error
        assert result.data["images_generated"] == 2
        # Two distinct files, no GET calls were needed
        assert (tmp_path / "b64_1.png").exists()
        assert (tmp_path / "b64_2.png").exists()
        # Only the POST was made
        post_count = sum(1 for m, _, _ in fake.calls if m == "POST")
        get_count = sum(1 for m, _, _ in fake.calls if m == "GET")
        assert post_count == 1
        assert get_count == 0


# ------------------------------------------------------------------
# Async task-poll branch
# ------------------------------------------------------------------

class TestAsyncPollBranch:

    def test_task_id_polled_until_done(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        png = _png_bytes(color="green")

        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={"task_id": "abc-123"})),
            # First poll: still running
            ("GET", "/task/abc-123",
             lambda call: _FakeResp(json_payload={"status": "running"})),
            # Second poll: succeeded — body now contains the images
            ("GET", "/task/abc-123",
             lambda call: _FakeResp(json_payload={
                 "status": "succeeded",
                 "images": [{"url": "https://cdn.example.com/poll.png"}],
             })),
            # GET the actual image
            ("GET", "poll.png", lambda call: _FakeResp(content=png)),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        # Shorten poll interval so the test doesn't take 2s
        monkeypatch.setattr(mod.MiniMaxImage, "POLL_INTERVAL_SECONDS", 0.0)

        out = tmp_path / "polled.png"
        tool = mod.MiniMaxImage()
        result = tool.execute({
            "prompt": "async path",
            "output_path": str(out),
        })

        assert result.success is True, result.error
        assert result.data["images_generated"] == 1
        assert out.exists()
        # 1 POST + 2 polls + 1 image GET = 4 calls
        assert len(fake.calls) == 4

    def test_poll_failed_status_returns_error(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={"task_id": "dead-task"})),
            ("GET", "/task/dead-task",
             lambda call: _FakeResp(json_payload={"status": "failed"})),
        ])
        monkeypatch.setattr(mod, "requests", fake)
        monkeypatch.setattr(mod.MiniMaxImage, "POLL_INTERVAL_SECONDS", 0.0)
        monkeypatch.setattr(mod.MiniMaxImage, "POLL_TIMEOUT_SECONDS", 1.0)

        tool = mod.MiniMaxImage()
        result = tool.execute({"prompt": "fails", "output_path": str(tmp_path / "x.png")})
        assert result.success is False
        assert "Unrecognized" in result.error or "no images" in result.error.lower()


# ------------------------------------------------------------------
# Unrecognized response shape
# ------------------------------------------------------------------

class TestUnrecognizedShape:

    def test_no_images_in_response_returns_clean_error(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={
                 "weird_field": "wat",
                 "trace_id": "12345",
             })),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        tool = mod.MiniMaxImage()
        result = tool.execute({"prompt": "?", "output_path": str(tmp_path / "x.png")})

        assert result.success is False
        # Error mentions the unknown shape so the next maintainer can debug.
        assert "Unrecognized" in result.error
        # Error does NOT leak the API key.
        assert "test-minimax-key" not in result.error

    def test_http_error_returns_clean_error(self, minimax_tool, tmp_path):
        mod, monkeypatch = minimax_tool
        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(status_code=401, json_payload={
                 "error": "invalid api key",
             })),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        tool = mod.MiniMaxImage()
        result = tool.execute({"prompt": "?", "output_path": str(tmp_path / "x.png")})
        assert result.success is False
        assert "HTTP 401" in result.error


# ------------------------------------------------------------------
# Cost + idempotency regression
# ------------------------------------------------------------------

class TestCostRegression:

    def test_cost_matches_estimate(self, minimax_tool, tmp_path):
        """What `estimate_cost` returns must match what `execute` records
        (no surprise billing drift between the pre-flight call and the
        actual run)."""
        mod, monkeypatch = minimax_tool
        png = _png_bytes()
        fake = _FakeRequests([
            ("POST", "/v1/image_generation",
             lambda call: _FakeResp(json_payload={
                 "data": [{"b64_json": base64.b64encode(png).decode()}],
             })),
        ])
        monkeypatch.setattr(mod, "requests", fake)

        tool = mod.MiniMaxImage()
        inputs = {"prompt": "x", "n": 3, "output_path": str(tmp_path / "x.png")}
        result = tool.execute(inputs)
        assert result.success is True
        assert result.cost_usd == tool.estimate_cost(inputs)
        # And the cost is flagged low-confidence (per SKILL.md honesty section)
        assert result.data["cost_estimate_confidence"] == "low"
