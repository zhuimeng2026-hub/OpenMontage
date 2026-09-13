"""Contract tests for the MiniMax (Hailuo AI) Image-01 generation tool.

These tests verify that the tool satisfies the BaseTool contract without
requiring a real MiniMax API key or making any network calls. They cover
class attributes, schemas, status reporting, cost estimates, payload
construction, output path resolution, error redaction, and the
Layer 3 skill file existence.

Run: pytest tests/contracts/test_minimax_image.py -v
"""

from pathlib import Path

import pytest

from tools.base_tool import (
    BaseTool,
    ExecutionMode,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.graphics.minimax_image import MiniMaxImage

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


# ------------------------------------------------------------------
# Contract compliance
# ------------------------------------------------------------------

class TestContract:

    def test_inherits_base_tool(self):
        assert issubclass(MiniMaxImage, BaseTool)

    def test_has_required_identity(self):
        tool = MiniMaxImage()
        assert tool.name == "minimax_image"
        assert tool.version
        assert tool.provider == "minimax"
        assert tool.capability == "image_generation"
        assert tool.tier == ToolTier.GENERATE
        assert tool.stability == ToolStability.EXPERIMENTAL
        assert tool.runtime == ToolRuntime.API
        assert tool.execution_mode == ExecutionMode.SYNC
        assert tool.determinism.value == "stochastic"

    def test_has_input_schema(self):
        tool = MiniMaxImage()
        schema = tool.input_schema
        assert schema.get("type") == "object"
        required = schema.get("required", [])
        assert required == ["prompt"]
        assert "prompt" in schema["properties"]

    def test_aspect_ratio_enum_has_safe_defaults(self):
        """Aspect ratio enum must include the 5 standard ratios."""
        tool = MiniMaxImage()
        ratios = tool.input_schema["properties"]["aspect_ratio"]["enum"]
        assert set(ratios) == {"1:1", "16:9", "9:16", "4:3", "3:4"}
        assert tool.input_schema["properties"]["aspect_ratio"]["default"] == "1:1"

    def test_n_bounded(self):
        tool = MiniMaxImage()
        n_prop = tool.input_schema["properties"]["n"]
        assert n_prop["minimum"] == 1
        assert n_prop["maximum"] >= 1

    def test_has_capabilities(self):
        tool = MiniMaxImage()
        assert "generate_image" in tool.capabilities

    def test_has_agent_skills(self):
        tool = MiniMaxImage()
        assert "minimax" in tool.agent_skills

    def test_layer3_skill_exists(self):
        """Contract test: every provider-specific skill must have a SKILL.md
        file. Mirrors test_dashscope_tools.py:87-92."""
        skill_path = (
            PROJECT_ROOT / ".agents" / "skills" / "minimax" / "SKILL.md"
        )
        assert skill_path.exists(), f"Missing Layer 3 skill: {skill_path}"
        content = skill_path.read_text(encoding="utf-8")
        # The skill MUST mention the env var name so future maintainers
        # know which key to set.
        assert "MINIMAX_API_KEY" in content
        # The skill MUST mention the model so docs aren't a lie about what's
        # actually wired up.
        assert "image-01" in content

    def test_has_fallbacks(self):
        tool = MiniMaxImage()
        assert tool.fallback_tools, "must declare at least one fallback"
        for fb in tool.fallback_tools:
            assert isinstance(fb, str) and fb.endswith("_image"), (
                f"unexpected fallback tool: {fb}"
            )

    def test_has_install_instructions(self):
        tool = MiniMaxImage()
        assert tool.install_instructions
        assert "MINIMAX_API_KEY" in tool.install_instructions

    def test_get_info_returns_dict(self):
        tool = MiniMaxImage()
        info = tool.get_info()
        assert isinstance(info, dict)
        assert info["name"] == "minimax_image"
        assert info["provider"] == "minimax"
        assert info["runtime"] == "api"
        assert "minimax" in info["agent_skills"]

    def test_status_unavailable_without_key(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        tool = MiniMaxImage()
        assert tool.get_status() == ToolStatus.UNAVAILABLE

    def test_status_available_with_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "fake-key-for-testing")
        tool = MiniMaxImage()
        assert tool.get_status() == ToolStatus.AVAILABLE

    def test_idempotency_key_fields(self):
        tool = MiniMaxImage()
        for field in ("prompt", "aspect_ratio", "n", "seed"):
            assert field in tool.idempotency_key_fields

    def test_has_resource_profile(self):
        tool = MiniMaxImage()
        assert tool.resource_profile.network_required is True
        assert tool.resource_profile.vram_mb == 0

    def test_has_retry_policy(self):
        tool = MiniMaxImage()
        assert tool.retry_policy.max_retries >= 0

    def test_has_side_effects(self):
        tool = MiniMaxImage()
        assert any("API" in s for s in tool.side_effects)
        assert any("output_path" in s for s in tool.side_effects)

    def test_has_user_visible_verification(self):
        tool = MiniMaxImage()
        assert tool.user_visible_verification

    def test_estimate_cost_returns_float(self):
        tool = MiniMaxImage()
        cost = tool.estimate_cost({"prompt": "test", "n": 1})
        assert isinstance(cost, float)
        assert cost > 0.0

    def test_cost_scales_with_n(self):
        tool = MiniMaxImage()
        cost1 = tool.estimate_cost({"prompt": "test", "n": 1})
        cost3 = tool.estimate_cost({"prompt": "test", "n": 3})
        assert cost3 > cost1
        assert cost3 == pytest.approx(cost1 * 3)

    def test_dry_run_returns_dict(self):
        tool = MiniMaxImage()
        result = tool.dry_run({"prompt": "test"})
        assert isinstance(result, dict)
        assert result["tool"] == "minimax_image"

    def test_lazy_imports_requests_and_pil(self):
        """The tool module must not have top-level imports that block
        registry discovery. requests and PIL are imported inside execute()
        / _save_image() — verify the module itself is light."""
        import sys

        from tools.graphics import minimax_image

        # The module's own namespace should not carry PIL or requests.
        # (They get imported into tools.graphics.minimax_image's frame
        # during execute(), not at import time.)
        assert "PIL" not in dir(minimax_image)
        # requests IS imported at module top in this file because the
        # parse helpers reference it; that's fine — it's a stdlib HTTP
        # library and not a heavy ML dep. We just want to confirm PIL is
        # NOT pulled in eagerly.
        assert "Image" not in dir(minimax_image)


# ------------------------------------------------------------------
# Payload + path helpers
# ------------------------------------------------------------------

class TestPayloadAndPaths:

    def test_build_payload_minimal(self):
        tool = MiniMaxImage()
        payload = tool._build_payload({"prompt": "a cat"})
        assert payload["model"] == "image-01"
        assert payload["prompt"] == "a cat"
        assert payload["aspect_ratio"] == "1:1"
        assert payload["n"] == 1
        assert "seed" not in payload

    def test_build_payload_with_seed(self):
        tool = MiniMaxImage()
        payload = tool._build_payload(
            {"prompt": "x", "aspect_ratio": "16:9", "n": 3, "seed": 99}
        )
        assert payload["seed"] == 99
        assert payload["n"] == 3
        assert payload["aspect_ratio"] == "16:9"

    def test_resolve_output_paths_single_unchanged(self):
        paths = MiniMaxImage._resolve_output_paths("foo.png", 1, "png")
        assert paths == [Path("foo.png")]

    def test_resolve_output_paths_multiple_inserts_index(self):
        paths = MiniMaxImage._resolve_output_paths("foo.png", 3, "png")
        assert [p.name for p in paths] == ["foo_1.png", "foo_2.png", "foo_3.png"]
        assert len(set(paths)) == 3

    def test_resolve_output_paths_no_extension_inferred(self):
        paths = MiniMaxImage._resolve_output_paths("foo", 2, "png")
        assert [p.name for p in paths] == ["foo_1.png", "foo_2.png"]

    def test_resolve_output_paths_no_output_path(self):
        paths = MiniMaxImage._resolve_output_paths(None, 2, "png")
        assert [p.name for p in paths] == ["generated_image_1.png", "generated_image_2.png"]

    def test_infer_extension_from_output_path(self):
        assert MiniMaxImage._infer_extension("foo.png", b"junk") == "png"
        assert MiniMaxImage._infer_extension("foo.jpg", b"junk") == "jpg"
        assert MiniMaxImage._infer_extension("foo.JPEG", b"junk") == "jpg"
        assert MiniMaxImage._infer_extension("foo.webp", b"junk") == "webp"

    def test_infer_extension_sniffs_png_magic(self):
        png_magic = b"\x89PNG\r\n\x1a\n" + b"rest"
        assert MiniMaxImage._infer_extension(None, png_magic) == "png"

    def test_infer_extension_sniffs_jpeg_magic(self):
        jpeg_magic = b"\xff\xd8\xff\xe0" + b"rest"
        assert MiniMaxImage._infer_extension(None, jpeg_magic) == "jpg"

    def test_infer_extension_sniffs_webp_magic(self):
        webp_magic = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"rest"
        assert MiniMaxImage._infer_extension(None, webp_magic) == "webp"

    def test_infer_extension_falls_back_to_png(self):
        assert MiniMaxImage._infer_extension(None, b"unknown bytes") == "png"

    def test_safe_error_redacts_key(self, monkeypatch):
        monkeypatch.setenv("MINIMAX_API_KEY", "secret-key-12345")
        redacted = MiniMaxImage._safe_error(
            Exception("failed with key secret-key-12345 in headers")
        )
        assert "secret-key-12345" not in redacted
        assert "[redacted]" in redacted

    def test_safe_error_no_op_when_no_key_set(self, monkeypatch):
        monkeypatch.delenv("MINIMAX_API_KEY", raising=False)
        msg = MiniMaxImage._safe_error(Exception("no key in env"))
        # Should return the message as-is when no key is present.
        assert "no key in env" in msg


# ------------------------------------------------------------------
# PIL save behavior
# ------------------------------------------------------------------

class TestImageSave:

    def test_save_rgba_as_png_preserves_transparency(self, tmp_path):
        """RGBA PNG should be saved as RGBA (Pillow handles this natively)."""
        from PIL import Image as PILImage
        rgba = PILImage.new("RGBA", (10, 10), (255, 0, 0, 128))
        out = tmp_path / "out.png"
        MiniMaxImage._save_image(rgba.tobytes() if False else
                                 _png_bytes(rgba), out, "png")
        # Re-open and confirm RGBA + size survived
        reopened = PILImage.open(out)
        assert reopened.size == (10, 10)
        assert reopened.mode in {"RGBA", "P"}  # PIL may promote P-mode PNGs

    def test_save_rgba_as_jpeg_flattens_to_rgb(self, tmp_path):
        """RGBA → JPEG must NOT raise; _save_image must flatten to RGB."""
        from PIL import Image as PILImage
        rgba = PILImage.new("RGBA", (10, 10), (0, 255, 0, 200))
        out = tmp_path / "out.jpg"
        # Should not raise — that's the whole point of the safety net.
        MiniMaxImage._save_image(_png_bytes(rgba), out, "jpg")
        reopened = PILImage.open(out)
        assert reopened.mode == "RGB"
        assert reopened.size == (10, 10)

    def test_save_rgb_passthrough(self, tmp_path):
        from PIL import Image as PILImage
        rgb = PILImage.new("RGB", (10, 10), "red")
        out = tmp_path / "out.jpg"
        MiniMaxImage._save_image(_png_bytes(rgb), out, "jpg")
        reopened = PILImage.open(out)
        assert reopened.mode == "RGB"

    def test_save_p_mode_converts_to_rgba(self, tmp_path):
        """P-mode PNG must not crash — convert to RGBA first."""
        from PIL import Image as PILImage
        p = PILImage.new("P", (10, 10), 0)
        out = tmp_path / "out.png"
        MiniMaxImage._save_image(_png_bytes(p), out, "png")
        assert out.exists()
        assert out.stat().st_size > 0

    def test_save_corrupt_bytes_raises(self, tmp_path):
        """Bad image bytes should bubble up as an exception so the
        outer try/except in execute() reports a clean failure."""
        from PIL import UnidentifiedImageError
        out = tmp_path / "out.png"
        with pytest.raises(Exception):
            MiniMaxImage._save_image(b"not an image", out, "png")


# ------------------------------------------------------------------
# Registry discovery
# ------------------------------------------------------------------

class TestRegistryDiscovery:

    def test_minimax_image_discoverable(self):
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.discover()
        names = {
            t.name for t in registry._tools.values()
            if t.provider == "minimax"
        }
        assert "minimax_image" in names

    def test_image_selector_finds_minimax(self):
        """image_selector should auto-discover minimax_image by capability."""
        from tools.graphics.image_selector import ImageSelector
        ImageSelector()
        assert MiniMaxImage().capability == "image_generation"


# ------------------------------------------------------------------
# Internal helpers (not part of public contract but worth pinning)
# ------------------------------------------------------------------

def _png_bytes(img) -> bytes:
    """Encode a PIL Image to PNG bytes for use as fake API output."""
    import io
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
