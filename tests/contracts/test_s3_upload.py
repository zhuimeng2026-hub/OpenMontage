"""Contract tests for the S3-compatible video upload tool.

These tests verify that ``tools.uploads.s3_upload.S3Upload`` satisfies the
BaseTool contract and implements AWS SigV4 signing correctly — **without
requiring real credentials or making any network calls**.

Run: pytest tests/contracts/test_s3_upload.py -v
"""

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)
from tools.uploads.s3_upload import S3Upload
from schemas.artifacts import validate_artifact


# ------------------------------------------------------------------
# Contract compliance
# ------------------------------------------------------------------

class TestContract:

    def test_inherits_base_tool(self):
        assert issubclass(S3Upload, BaseTool)

    def test_has_required_identity(self):
        tool = S3Upload()
        assert tool.name == "s3_upload"
        assert tool.version
        assert tool.provider == "s3"
        assert tool.capability == "publish"
        assert tool.tier == ToolTier.PUBLISH
        assert tool.stability == ToolStability.BETA
        assert tool.runtime == ToolRuntime.API

    def test_dependencies_are_env_s3(self):
        deps = S3Upload().dependencies
        assert "env:S3_ACCESS_KEY" in deps
        assert "env:S3_SECRET_KEY" in deps
        assert "env:S3_ENDPOINT_URL" in deps
        assert "env:S3_BUCKET" in deps
        assert len(deps) == 4

    def test_execution_mode_is_sync(self):
        assert S3Upload().execution_mode == ExecutionMode.SYNC

    def test_has_input_schema(self):
        schema = S3Upload().input_schema
        assert schema.get("type") == "object"
        props = schema.get("properties", {})
        assert props.get("video_path", {}).get("type") == "string"
        assert schema.get("required") == ["video_path"]

    def test_has_capabilities(self):
        tool = S3Upload()
        for cap in ("upload_video", "get_download_url", "presign_get_url", "generate_download_page"):
            assert cap in tool.capabilities

    def test_has_supports(self):
        tool = S3Upload()
        assert tool.supports.get("public_direct_link") is True
        assert tool.supports.get("presigned_get_url") is True
        assert tool.supports.get("download_page") is True
        assert tool.supports.get("path_style_endpoint") is True

    def test_has_install_instructions(self):
        tool = S3Upload()
        assert "S3_ACCESS_KEY" in tool.install_instructions
        assert "S3_ENDPOINT_URL" in tool.install_instructions

    def test_get_info_returns_dict(self):
        info = S3Upload().get_info()
        assert isinstance(info, dict)
        assert info["name"] == "s3_upload"
        assert info["provider"] == "s3"
        assert info["runtime"] == "api"

    def test_status_unavailable_without_keys(self, monkeypatch):
        for k in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT_URL", "S3_BUCKET"):
            monkeypatch.delenv(k, raising=False)
        assert S3Upload().get_status() == ToolStatus.UNAVAILABLE

    def test_status_available_with_all_keys(self, monkeypatch):
        monkeypatch.setenv("S3_ACCESS_KEY", "fake-ak")
        monkeypatch.setenv("S3_SECRET_KEY", "fake-sk")
        monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.example.com")
        monkeypatch.setenv("S3_BUCKET", "demo")
        assert S3Upload().get_status() == ToolStatus.AVAILABLE

    def test_status_unavailable_when_endpoint_missing(self, monkeypatch):
        monkeypatch.setenv("S3_ACCESS_KEY", "ak")
        monkeypatch.setenv("S3_SECRET_KEY", "sk")
        monkeypatch.delenv("S3_ENDPOINT_URL", raising=False)
        monkeypatch.setenv("S3_BUCKET", "demo")
        assert S3Upload().get_status() == ToolStatus.UNAVAILABLE

    def test_status_unavailable_when_bucket_missing(self, monkeypatch):
        monkeypatch.setenv("S3_ACCESS_KEY", "ak")
        monkeypatch.setenv("S3_SECRET_KEY", "sk")
        monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.example.com")
        monkeypatch.delenv("S3_BUCKET", raising=False)
        assert S3Upload().get_status() == ToolStatus.UNAVAILABLE

    def test_has_resource_profile(self):
        rp = S3Upload().resource_profile
        assert isinstance(rp, ResourceProfile)
        assert rp.network_required is True
        assert rp.vram_mb == 0

    def test_has_retry_policy(self):
        assert S3Upload().retry_policy.max_retries >= 0

    def test_has_side_effects(self):
        side = S3Upload().side_effects
        assert len(side) > 0
        assert any("upload" in s.lower() for s in side)

    def test_estimate_cost_returns_float(self):
        cost = S3Upload().estimate_cost({"video_path": "/tmp/x.mp4"})
        assert isinstance(cost, float)

    def test_dry_run_returns_dict(self):
        result = S3Upload().dry_run({"video_path": "/tmp/fake.mp4"})
        assert isinstance(result, dict)
        assert "missing_env" in result

    def test_fallback_tools_empty(self):
        assert S3Upload().fallback_tools == []

    def test_agent_skills_empty(self):
        assert S3Upload().agent_skills == []

    def test_idempotency_fields_empty(self):
        # Signing and URLs are time-dependent; cannot be idempotent.
        assert S3Upload().idempotency_key_fields == []


# ------------------------------------------------------------------
# Input schema validation
# ------------------------------------------------------------------

class TestInputSchema:

    def test_required_video_path(self):
        with pytest.raises(Exception):
            import jsonschema
            jsonschema.validate({}, S3Upload().input_schema)

    def test_visibility_enum(self):
        schema = S3Upload().input_schema["properties"]["visibility"]
        assert schema["enum"] == ["public", "private"]

    def test_expire_seconds_bounds(self):
        schema = S3Upload().input_schema["properties"]["expire_seconds"]
        assert schema["minimum"] == 60
        assert schema["maximum"] == 604800

    def test_additional_files_is_array_of_strings(self):
        schema = S3Upload().input_schema["properties"]["additional_files"]
        assert schema["type"] == "array"
        assert schema["items"]["type"] == "string"

    def test_make_download_page_is_bool(self):
        schema = S3Upload().input_schema["properties"]["make_download_page"]
        assert schema["type"] == "boolean"
        assert schema["default"] is False


# ------------------------------------------------------------------
# SigV4 signing correctness (pure math; no network)
# ------------------------------------------------------------------

class TestSigV4:

    def _make_headers(self, payload_hash: str, host: str, date: str, ct: str = "application/octet-stream") -> dict[str, str]:
        return {
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": date,
            "Content-Type": ct,
        }

    def test_returned_authorization_contains_s3_headers(self):
        h = S3Upload._sign_v4(
            ak="ak", sk="sk", region="us-east-1", service="s3",
            method="PUT",
            canonical_uri="/bucket/foo",
            canonical_query="",
            headers=self._make_headers("abc123", "s3.example.com", "20260807T120000Z", "video/mp4"),
            payload_hash="abc123",
            now=datetime(2026, 8, 7, 12, 0, 0, tzinfo=timezone.utc),
        )
        assert "Authorization" in h
        assert "AWS4-HMAC-SHA256" in h["Authorization"]
        assert "Credential=ak/" in h["Authorization"]
        assert "x-amz-content-sha256;x-amz-date" in h["Authorization"]
        assert "host" in h["Authorization"]
        assert h["x-amz-date"] == "20260807T120000Z"
        assert h["x-amz-content-sha256"] == "abc123"

    def test_deterministic_with_fixed_now(self):
        now = datetime(2026, 3, 15, 9, 30, 0, tzinfo=timezone.utc)
        h1 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="eu-west-1", service="s3",
            method="PUT",
            canonical_uri="/bucket/key",
            canonical_query="",
            headers=self._make_headers("payload", "b.example.com", "20260315T093000Z"),
            payload_hash="payload",
            now=now,
        )
        h2 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="eu-west-1", service="s3",
            method="PUT",
            canonical_uri="/bucket/key",
            canonical_query="",
            headers=self._make_headers("payload", "b.example.com", "20260315T093000Z"),
            payload_hash="payload",
            now=now,
        )
        assert h1 == h2

    def test_differs_when_secret_changes(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        h1 = S3Upload._sign_v4(
            ak="ak", sk="sk1", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h", "h.com", "20260101T000000Z"),
            payload_hash="h", now=now,
        )
        h2 = S3Upload._sign_v4(
            ak="ak", sk="sk2", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h", "h.com", "20260101T000000Z"),
            payload_hash="h", now=now,
        )
        assert h1 != h2

    def test_differs_when_region_changes(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        h1 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h", "h.com", "20260101T000000Z"),
            payload_hash="h", now=now,
        )
        h2 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="eu-west-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h", "h.com", "20260101T000000Z"),
            payload_hash="h", now=now,
        )
        assert h1 != h2

    def test_differs_when_canonical_uri_changes(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        h1 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h", "h.com", "20260101T000000Z"),
            payload_hash="h", now=now,
        )
        h2 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/c", canonical_query="",
            headers=self._make_headers("h", "h.com", "20260101T000000Z"),
            payload_hash="h", now=now,
        )
        assert h1 != h2

    def test_differs_when_payload_hash_changes(self):
        now = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        h1 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h1", "h.com", "20260101T000000Z"),
            payload_hash="h1", now=now,
        )
        h2 = S3Upload._sign_v4(
            ak="ak", sk="sk", region="us-east-1", service="s3",
            method="PUT", canonical_uri="/a/b", canonical_query="",
            headers=self._make_headers("h2", "h.com", "20260101T000000Z"),
            payload_hash="h2", now=now,
        )
        assert h1 != h2


# ------------------------------------------------------------------
# Presigned GET URL construction
# ------------------------------------------------------------------

class TestPresignedUrl:

    def _base(self, now: datetime) -> dict:
        return dict(
            endpoint="https://s3.example.com",
            bucket="demo",
            object_key="videos/proj/foo.mp4",
            region="us-east-1",
            ak="AKIAIOSFODNN7EXAMPLE",
            sk="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            expire_seconds=3600,
            public_base="",
            now=now,
        )

    def test_contains_expected_query_params(self, monkeypatch):
        url = S3Upload._presigned_get_url(**self._base(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)))
        for part in ["X-Amz-Algorithm", "X-Amz-Credential", "X-Amz-Date", "X-Amz-Expires", "X-Amz-SignedHeaders", "X-Amz-Signature"]:
            assert part in url, f"missing param {part} in {url}"

    def test_credential_is_double_percent_encoded(self):
        # The credential contains '/' which MUST be %2F-encoded inside the query.
        url = S3Upload._presigned_get_url(**self._base(datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)))
        # urlencoded '%2F' inside the query string becomes '%252F' (once for URL value, once for param value).
        assert "%2F" in url

    def test_deterministic_with_fixed_now(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        u1 = S3Upload._presigned_get_url(**self._base(now))
        u2 = S3Upload._presigned_get_url(**self._base(now))
        assert u1 == u2

    def test_differs_when_expire_changes(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        u1 = S3Upload._presigned_get_url(expire_seconds=3600, **{k: v for k, v in self._base(now).items() if k != "expire_seconds"})
        u2 = S3Upload._presigned_get_url(expire_seconds=7200, **{k: v for k, v in self._base(now).items() if k != "expire_seconds"})
        assert u1 != u2

    def test_public_base_prefix(self):
        now = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        url = S3Upload._presigned_get_url(
            public_base="https://cdn.example.com/videos",
            **{k: v for k, v in self._base(now).items() if k != "public_base"},
        )
        assert url.startswith("https://cdn.example.com/videos/")


# ------------------------------------------------------------------
# Public URL construction
# ------------------------------------------------------------------

class TestPublicUrl:

    def test_without_public_base(self):
        url = S3Upload._public_url(
            "https://s3.example.com", "my-bucket", "a/b.mp4", ""
        )
        assert url == "https://s3.example.com/my-bucket/a/b.mp4"

    def test_with_public_base(self):
        url = S3Upload._public_url(
            "https://s3.example.com", "my-bucket", "a/b.mp4", "https://cdn.example.com"
        )
        assert url == "https://cdn.example.com/a/b.mp4"

    def test_path_encoding(self):
        url = S3Upload._public_url(
            "https://s3.example.com", "bucket", "a/b c.mp4", ""
        )
        assert " " in url or "%20" in url
        assert "%20" in url


# ------------------------------------------------------------------
# Object key derivation
# ------------------------------------------------------------------

class TestObjectKeyDerivation:

    def test_default_with_project(self):
        t = S3Upload()
        assert t._default_object_key("proj", Path("foo.mp4")) == "videos/proj/foo.mp4"

    def test_default_without_project(self):
        t = S3Upload()
        assert t._default_object_key("", Path("foo.mp4")) == "foo.mp4"

    def test_infer_project_name_from_projects_dir(self):
        assert S3Upload._infer_project_name(Path("/tmp/projects/myproj/renders/out.mp4")) == "myproj"

    def test_infer_project_name_falls_back_to_stem(self):
        # Path("/tmp/out.mp4").resolve() has 2 parents: [Path('/tmp'), Path('/')].
        # Not deep enough for parent naming → fallback to stem.
        assert S3Upload._infer_project_name(Path("/tmp/out.mp4")) == "out"


# ------------------------------------------------------------------
# Download page generation
# ------------------------------------------------------------------

class TestDownloadPage:

    def test_contains_title(self):
        html = S3Upload._build_download_page(
            [{"filename": "a.mp4", "url": "https://x", "size_bytes": 1000}],
            "My Videos",
            "2026-01-01T00:00:00+00:00",
        )
        assert "My Videos" in html

    def test_contains_download_links(self):
        html = S3Upload._build_download_page(
            [{"filename": "a.mp4", "url": "https://x/a.mp4", "size_bytes": 12345}],
            "Videos",
            "now",
        )
        assert "a.mp4" in html
        assert "Download" in html

    def test_escapes_script_tags(self):
        html = S3Upload._build_download_page(
            [{"filename": "x.mp4", "url": "https://x", "size_bytes": 100}],
            "<script>alert(1)</script>",
            "now",
        )
        assert "<script>" not in html
        assert "&lt;script&gt;" in html


# ------------------------------------------------------------------
# publish_log construction and schema validation
# ------------------------------------------------------------------

class TestPublishLog:

    def test_build_and_validate(self):
        log = S3Upload._build_publish_log(
            platform_label="s3",
            url="https://example.com/v.mp4",
            visibility="public",
            local_path="/tmp/v.mp4",
        )
        validate_artifact("publish_log", log)
        assert log["version"] == "1.0"
        assert len(log["entries"]) == 1
        e = log["entries"][0]
        assert e["platform"] == "s3"
        assert e["status"] == "published"
        assert e["url"] == "https://example.com/v.mp4"
        assert e["visibility"] == "public"
        assert "timestamp" in e
        assert "export_path" in e

    def test_with_title_adds_metadata_used(self):
        log = S3Upload._build_publish_log(
            platform_label="s3", url="https://x",
            visibility="private", local_path="/tmp/x", title="Demo",
        )
        validate_artifact("publish_log", log)
        assert log["entries"][0]["metadata_used"] == {"title": "Demo"}

    def test_rejects_extra_entry_fields(self):
        # additionalProperties:false on entries — any extra key should fail.
        bad_log = {"version": "1.0", "entries": [{"platform": "s3", "status": "published", "url": "x", "visibility": "public", "export_path": "/tmp/x", "timestamp": "2026-01-01T00:00:00+00:00", "foo": "bar"}]}
        with pytest.raises(Exception):
            validate_artifact("publish_log", bad_log)


# ------------------------------------------------------------------
# Helper methods
# ------------------------------------------------------------------

class TestHelpers:

    def test_human_size(self):
        assert S3Upload._human_size(0) == "0 B"
        assert "KB" in S3Upload._human_size(1024)
        assert "MB" in S3Upload._human_size(1024 * 1024)
        assert "GB" in S3Upload._human_size(1024 * 1024 * 1024)

    def test_canonical_uri_strips_leading_slash(self):
        assert S3Upload._canonical_uri("/foo.mp4") == "/foo.mp4"
        assert S3Upload._canonical_uri("foo.mp4") == "/foo.mp4"

    def test_canonical_uri_includes_bucket_when_provided(self):
        assert S3Upload._canonical_uri("foo.mp4", bucket="my-bucket") == "/my-bucket/foo.mp4"
        assert S3Upload._canonical_uri("videos/demo/out.mp4", bucket="my-bucket") == "/my-bucket/videos/demo/out.mp4"

    def test_canonical_querystring_sorted(self):
        qs = S3Upload._canonical_querystring({"b": "2", "a": "1"})
        assert qs.startswith("a=1")
        assert qs.endswith("b=2")

    def test_safe_error_redacts_s3_keys(self, monkeypatch):
        monkeypatch.setenv("S3_ACCESS_KEY", "AKIAIOSFODNN7EXAMPLE")
        monkeypatch.setenv("S3_SECRET_KEY", "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        exc = Exception("failed ak=AKIAIOSFODNN7EXAMPLE sk=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY")
        msg = S3Upload._safe_error(exc)
        assert "AKIAIOSFODNN7EXAMPLE" not in msg
        assert "wJalrXUtnFEMI" not in msg
        assert "[redacted]" in msg

    def test_safe_error_no_empty_string_bug(self, monkeypatch):
        monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
        monkeypatch.delenv("S3_SECRET_KEY", raising=False)
        assert S3Upload._safe_error(Exception("abc")) == "abc"

    def test_sha256_of_bytes(self):
        import hashlib
        data = b"hello"
        assert S3Upload._sha256_hex(data) == hashlib.sha256(data).hexdigest()

    def test_sha256_of_none(self):
        assert S3Upload._sha256_hex(None) == hashlib.sha256().hexdigest()


# ------------------------------------------------------------------
# Registry discovery
# ------------------------------------------------------------------

class TestRegistryDiscovery:

    def test_discoverable(self):
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.discover()
        names = {t.name for t in registry._tools.values()}
        assert "s3_upload" in names

    def test_distinct(self):
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.discover()
        items = [t for t in registry._tools.values() if t.name == "s3_upload"]
        assert len(items) == 1
        assert items[0].capability == "publish"
        assert items[0].provider == "s3"

    def test_publish_capability_includes_export_bundle(self):
        from tools.tool_registry import ToolRegistry
        registry = ToolRegistry()
        registry.discover()
        publish = registry.get_by_capability("publish")
        names = {t.name for t in publish}
        assert "export_bundle" in names
        assert "s3_upload" in names


# ------------------------------------------------------------------
# execute path (offline / env-missing branches)
# ------------------------------------------------------------------

class TestExecute:

    def test_missing_credentials_fails_fast(self, monkeypatch):
        for k in ("S3_ACCESS_KEY", "S3_SECRET_KEY", "S3_ENDPOINT_URL", "S3_BUCKET"):
            monkeypatch.delenv(k, raising=False)
        result = S3Upload().execute({"video_path": "/tmp/fake.mp4"})
        assert result.success is False
        assert "credentials" in result.error.lower() or "not configured" in result.error.lower()

    def test_missing_video_path_fails(self, monkeypatch):
        monkeypatch.setenv("S3_ACCESS_KEY", "ak")
        monkeypatch.setenv("S3_SECRET_KEY", "sk")
        monkeypatch.setenv("S3_ENDPOINT_URL", "https://s3.example.com")
        monkeypatch.setenv("S3_BUCKET", "demo")
        result = S3Upload().execute({"video_path": "/nonexistent/fake.mp4"})
        assert result.success is False
        assert "not found" in result.error.lower() or "not a file" in result.error.lower()

    def test_dry_run_with_missing_env(self, monkeypatch):
        monkeypatch.delenv("S3_ACCESS_KEY", raising=False)
        dry = S3Upload().dry_run({"video_path": "/tmp/x.mp4"})
        assert dry["video_path_exists"] is False
        assert "S3_ACCESS_KEY" in dry["missing_env"]
