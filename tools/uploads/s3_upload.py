"""S3-compatible video upload — AWS SigV4 via pure requests.

A PUBLISH-tier provider tool for OpenMontage. Uploads rendered video files to
any AWS S3-compatible object store (AWS S3, MinIO, Cloudflare R2, Aliyun OSS,
Tencent COS, Qiniu, …) using hand-rolled AWS Signature V4 (``AWS4-HMAC-SHA256``)
over a plain ``requests.put``. No boto3 / minio / oss2 dependency required —
only ``requests`` (already a core dependency) plus stdlib ``hashlib``, ``hmac``,
``urllib.parse``.

Supported delivery modes (chosen at runtime; see input_schema):

* ``visibility="public"`` — uploads as a public object and returns a permanent
  direct download URL. The URL is built from ``S3_PUBLIC_BASE_URL`` when set,
  otherwise from the endpoint + bucket.
* ``visibility="private"`` — returns a time-limited pre-signed GET URL
  (AWS ``X-Amz-Signature`` query param). The bucket must be private; the
  presigned URL is self-contained and needs no extra auth.

Both modes can be combined with ``make_download_page=True``: in that case a
standalone HTML download page listing all uploaded files is also built and
uploaded, and its URL is returned in ``data["download_page_url"]``.

The tool emits a schema-valid ``publish_log`` entry (status: ``published``)
validated against ``schemas/artifacts/publish_log.schema.json``.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    RetryPolicy,
    ToolResult,
    ToolRuntime,
    ToolStability,
    ToolStatus,
    ToolTier,
)


class S3Upload(BaseTool):
    """Upload rendered video to any S3-compatible object store."""

    name = "s3_upload"
    version = "0.1.0"
    tier = ToolTier.PUBLISH
    capability = "publish"
    provider = "s3"
    stability = ToolStability.BETA
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.API

    dependencies = [
        "env:S3_ACCESS_KEY",
        "env:S3_SECRET_KEY",
        "env:S3_ENDPOINT_URL",
        "env:S3_BUCKET",
    ]
    install_instructions = (
        "Set the following env vars (in your ``.env``) to use the S3-compatible "
        "video uploader:\n"
        "  S3_ENDPOINT_URL=<base URL without bucket, e.g. https://s3.us-east-1.amazonaws.com>\n"
        "  S3_REGION=<region for SigV4 scope; defaults to us-east-1>\n"
        "  S3_ACCESS_KEY=<access key id>\n"
        "  S3_SECRET_KEY=<secret access key>\n"
        "  S3_BUCKET=<bucket name, must already exist>\n"
        "  S3_PUBLIC_BASE_URL=<optional CDN / public prefix for direct links>\n"
        "  S3_DEFAULT_VISIBILITY=public|private\n"
    )

    capabilities = [
        "upload_video",
        "get_download_url",
        "presign_get_url",
        "generate_download_page",
    ]
    supports = {
        "public_direct_link": True,
        "presigned_get_url": True,
        "download_page": True,
        "multi_file_page": True,
        "path_style_endpoint": True,
    }
    best_for = [
        "uploading rendered videos to an S3-compatible object store",
        "handing a client a public permanent link or a time-limited pre-signed GET URL",
        "generating a standalone HTML download page listing multiple videos",
    ]
    not_good_for = [
        "uploading to video platforms (YouTube/TikTok) — use a dedicated platform publisher",
        "multipart uploads of very large files (>5 GB single-PUT limit)",
        "bucket provisioning (create the bucket out-of-band)",
    ]
    fallback_tools = []
    agent_skills = []

    input_schema = {
        "type": "object",
        "required": ["video_path"],
        "properties": {
            "video_path": {
                "type": "string",
                "description": "Path to the rendered video to upload (from render_report.outputs[].path).",
            },
            "project_id": {
                "type": "string",
                "description": "Project id; namespaces the object key. Defaults to the parent-of-parent dir name.",
            },
            "object_key": {
                "type": "string",
                "description": "Explicit S3 object key. Defaults to videos/<project_id>/<filename> (or <filename> when no project).",
            },
            "visibility": {
                "type": "string",
                "enum": ["public", "private"],
                "default": "public",
                "description": "public -> permanent direct link (bucket must allow public reads); private -> time-limited pre-signed GET URL.",
            },
            "expire_seconds": {
                "type": "integer",
                "minimum": 60,
                "maximum": 604800,
                "default": 604800,
                "description": "Pre-signed URL lifetime when visibility=private (AWS max 604800 s = 7 days).",
            },
            "make_download_page": {
                "type": "boolean",
                "default": False,
                "description": "Also build and upload a standalone HTML download page listing all uploaded files.",
            },
            "additional_files": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Extra local file paths to upload and list on the download page.",
            },
            "page_title": {
                "type": "string",
                "description": "Title of the generated download page.",
            },
            "platform_label": {
                "type": "string",
                "default": "s3",
                "description": "platform value for the publish_log entry.",
            },
        },
    }
    output_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string"},
            "object_key": {"type": "string"},
            "bucket": {"type": "string"},
            "visibility": {"type": "string"},
            "expires_at": {"type": "string"},
            "download_page_url": {"type": "string"},
            "uploaded_files": {"type": "array", "items": {"type": "object"}},
            "publish_log": {"type": "object"},
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=0.1,
        ram_mb=256,
        vram_mb=0,
        disk_mb=0,
        network_required=True,
    )
    retry_policy = RetryPolicy(
        max_retries=2,
        backoff_seconds=2.0,
        retryable_errors=["timeout", "connection_error"],
    )
    idempotency_key_fields = []
    side_effects = [
        "uploads video files to an S3-compatible object store",
        "generates public or pre-signed GET download URLs",
        "optionally uploads a standalone HTML download page",
    ]

    # ------------------------------------------------------------------ #
    # Env / config accessors
    # ------------------------------------------------------------------ #

    @staticmethod
    def _env(key: str, default: str | None = None) -> str:
        v = os.environ.get(key)
        if v is None:
            return default or ""
        v = v.strip()
        # Treat lines starting with '#' as comments (mirrors base_tool._load_dotenv).
        if v.startswith("#"):
            return default or ""
        return v

    def _ak(self) -> str:
        return self._env("S3_ACCESS_KEY")

    def _sk(self) -> str:
        return self._env("S3_SECRET_KEY")

    def _endpoint(self) -> str:
        return (self._env("S3_ENDPOINT_URL") or "").rstrip("/")

    def _bucket(self) -> str:
        return self._env("S3_BUCKET")

    def _region(self) -> str:
        return self._env("S3_REGION") or "us-east-1"

    def _public_base(self) -> str:
        return (self._env("S3_PUBLIC_BASE_URL") or "").rstrip("/")

    def _default_visibility(self) -> str:
        return (self._env("S3_DEFAULT_VISIBILITY") or "public").lower()

    def get_status(self) -> ToolStatus:
        if (
            self._ak()
            and self._sk()
            and self._endpoint()
            and self._bucket()
        ):
            return ToolStatus.AVAILABLE
        return ToolStatus.UNAVAILABLE

    # ------------------------------------------------------------------ #
    # SigV4 core
    # ------------------------------------------------------------------ #

    @staticmethod
    def _sha256_hex(data: bytes | None) -> str:
        h = hashlib.sha256()
        if data is not None:
            h.update(data)
        return h.hexdigest()

    @staticmethod
    def _sha256_of_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _canonical_uri(object_key: str, bucket: str | None = None) -> str:
        # S3 canonical URI is "/" + percent-encoded key (keep "/" unescaped).
        # For path-style endpoints (most S3-compatible stores including MinIO,
        # OSS, COS) the bucket is part of the path, so the canonical URI must
        # include it: "/bucket/key". This matches what boto3's
        # normalize_url_path produces from the full request URL.
        key = object_key.lstrip("/")
        if bucket:
            return "/" + urllib.parse.quote(bucket, safe="") + "/" + urllib.parse.quote(key, safe="/")
        return "/" + urllib.parse.quote(key, safe="/")

    @staticmethod
    def _canonical_querystring(params: dict) -> str:
        # Sort by decoded key; encode key AND value; no unescaped safe chars.
        pairs = []
        for k in sorted(params):
            vk = urllib.parse.quote(str(k), safe="")
            vv = urllib.parse.quote(str(params[k]), safe="")
            pairs.append(f"{vk}={vv}")
        return "&".join(pairs)

    @staticmethod
    def _signing_key(secret_key: str, date_stamp: str, region: str, service: str = "s3") -> bytes:
        # AWS SigV4 spec requires prefixing the secret key with "AWS4" for the first HMAC
        k_date = hmac.new(
            ("AWS4" + secret_key).encode("utf-8"), date_stamp.encode("utf-8"), hashlib.sha256
        ).digest()
        k_region = hmac.new(k_date, region.encode("utf-8"), hashlib.sha256).digest()
        k_service = hmac.new(k_region, service.encode("utf-8"), hashlib.sha256).digest()
        k_signing = hmac.new(k_service, b"aws4_request", hashlib.sha256).digest()
        return k_signing

    @classmethod
    def _sign_v4(
        cls,
        *,
        ak: str,
        sk: str,
        region: str,
        service: str,
        method: str,
        canonical_uri: str,
        canonical_query: str,
        headers: dict[str, str],
        payload_hash: str,
        now: datetime | None = None,
    ) -> dict[str, str]:
        now = now or datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")

        lower = {k.lower(): str(v).strip() for k, v in headers.items()}
        signed_names = sorted(lower)
        canonical_headers = "".join(f"{k}:{lower[k]}\n" for k in signed_names)
        signed_str = ";".join(signed_names)

        canonical_request = "\n".join([
            method.upper(),
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_str,
            payload_hash,
        ])

        credential_scope = f"{date_stamp}/{region}/{service}/aws4_request"
        request_hash = cls._sha256_hex(canonical_request.encode("utf-8"))
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            request_hash,
        ])

        k_signing = cls._signing_key(sk, date_stamp, region, service)
        signature = hmac.new(
            k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        authorization = (
            f"AWS4-HMAC-SHA256 Credential={ak}/{credential_scope}, "
            f"SignedHeaders={signed_str}, Signature={signature}"
        )
        return {
            "Authorization": authorization,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
        }

    @classmethod
    def _presigned_get_url(
        cls,
        endpoint: str,
        bucket: str,
        object_key: str,
        region: str,
        ak: str,
        sk: str,
        expire_seconds: int,
        public_base: str = "",
        now: datetime | None = None,
    ) -> str:
        now = now or datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        credential_scope = f"{date_stamp}/{region}/s3/aws4_request"
        credential = f"{ak}/{credential_scope}"

        query_params: dict[str, str] = {
            "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
            "X-Amz-Credential": credential,
            "X-Amz-Date": amz_date,
            "X-Amz-Expires": str(expire_seconds),
            "X-Amz-SignedHeaders": "host",
        }
        canonical_query = cls._canonical_querystring(query_params)
        canonical_uri = cls._canonical_uri(object_key, bucket=bucket)
        host = urllib.parse.urlparse(endpoint).netloc

        canonical_headers = f"host:{host}\n"
        canonical_request = "\n".join([
            "GET",
            canonical_uri,
            canonical_query,
            canonical_headers,
            "host",
            "UNSIGNED-PAYLOAD",
        ])
        request_hash = cls._sha256_hex(canonical_request.encode("utf-8"))
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            request_hash,
        ])

        k_signing = cls._signing_key(sk, date_stamp, region, "s3")
        signature = hmac.new(
            k_signing, string_to_sign.encode("utf-8"), hashlib.sha256
        ).hexdigest()
        query_params["X-Amz-Signature"] = signature

        # Rebuild the final canonical query AFTER adding Signature so the
        # signature covers the final query (this is the canonical AWS recipe).
        final_query = cls._canonical_querystring(query_params)
        path = urllib.parse.quote(object_key.lstrip("/"), safe="/")
        if public_base:
            return f"{public_base}/{path}?{final_query}"
        return f"{endpoint}/{bucket}/{path}?{final_query}"

    @classmethod
    def _public_url(
        cls,
        endpoint: str,
        bucket: str,
        object_key: str,
        public_base: str,
    ) -> str:
        path = urllib.parse.quote(object_key.lstrip("/"), safe="/")
        if public_base:
            return f"{public_base}/{path}"
        return f"{endpoint}/{bucket}/{path}"

    @staticmethod
    def _host(endpoint: str) -> str:
        return urllib.parse.urlparse(endpoint).netloc

    # ------------------------------------------------------------------ #
    # Upload primitives
    # ------------------------------------------------------------------ #

    def _put_file(
        self,
        local_path: Path,
        object_key: str,
        content_type: str = "application/octet-stream",
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ak = self._ak()
        sk = self._sk()
        endpoint = self._endpoint()
        bucket = self._bucket()
        region = self._region()
        public_base = self._public_base()

        canonical_uri = self._canonical_uri(object_key, bucket=bucket)
        host = self._host(endpoint)
        size = local_path.stat().st_size
        payload_hash = self._sha256_of_file(local_path)

        now_iso = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": now_iso,
            "Content-Type": content_type,
        }
        sig_headers = self._sign_v4(
            ak=ak, sk=sk, region=region, service="s3",
            method="PUT",
            canonical_uri=canonical_uri,
            canonical_query="",
            headers=headers,
            payload_hash=payload_hash,
            now=now,
        )

        url = f"{endpoint}{canonical_uri}"
        resp = requests.put(
            url,
            data=local_path.open("rb"),
            headers={
                **sig_headers,
                "Content-Type": content_type,
            },
            timeout=300,
        )
        resp.raise_for_status()

        url_out = self._public_url(endpoint, bucket, object_key, public_base)
        return {
            "object_key": object_key,
            "url": url_out,
            "size_bytes": size,
            "sha256": payload_hash,
            "filename": local_path.name,
            "visibility": "public",
            "http_status": resp.status_code,
        }

    def _put_bytes(
        self,
        payload: bytes,
        object_key: str,
        content_type: str = "text/plain",
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        ak = self._ak()
        sk = self._sk()
        endpoint = self._endpoint()
        bucket = self._bucket()
        region = self._region()
        public_base = self._public_base()

        canonical_uri = self._canonical_uri(object_key, bucket=bucket)
        host = self._host(endpoint)
        payload_hash = self._sha256_hex(payload)

        now_iso = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        headers = {
            "Host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": now_iso,
            "Content-Type": content_type,
        }
        sig_headers = self._sign_v4(
            ak=ak, sk=sk, region=region, service="s3",
            method="PUT",
            canonical_uri=canonical_uri,
            canonical_query="",
            headers=headers,
            payload_hash=payload_hash,
            now=now,
        )

        url = f"{endpoint}{canonical_uri}"
        resp = requests.put(
            url,
            data=payload,
            headers={
                **sig_headers,
                "Content-Type": content_type,
            },
            timeout=120,
        )
        resp.raise_for_status()

        url_out = self._public_url(endpoint, bucket, object_key, public_base)
        return {
            "object_key": object_key,
            "url": url_out,
            "size_bytes": len(payload),
            "sha256": payload_hash,
            "filename": object_key.rsplit("/", 1)[-1],
            "visibility": "public",
            "http_status": resp.status_code,
        }

    # ------------------------------------------------------------------ #
    # Download page + helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _build_download_page(
        items: list[dict[str, Any]],
        title: str,
        generated_at: str,
    ) -> str:
        import html as _html
        safe_title = _html.escape(str(title))
        rows = []
        for it in items:
            fname = _html.escape(str(it.get("filename", it.get("object_key", ""))))
            url = _html.escape(str(it.get("url", "")))
            size = S3Upload._human_size(it.get("size_bytes", 0))
            rows.append(
                "      <tr>"
                f"<td>{fname}</td>"
                f"<td>{size}</td>"
                f'<td><a href="{url}" download="{fname}">{_html.escape("Download")}</a></td>'
                f"</tr>\n"
            )
        body_rows = "".join(rows)
        return (
            "<!doctype html>\n"
            "<html lang=en>\n"
            "<head>\n"
            "  <meta charset=utf-8>\n"
            f"  <title>{safe_title} — Video Delivery</title>\n"
            "  <style>\n"
            "    body { font-family: system-ui, sans-serif; padding: 2rem; max-width: 800px; margin: 0 auto; }\n"
            "    h1 { color: #1a1a1a; }\n"
            "    table { border-collapse: collapse; width: 100%; }\n"
            "    th, td { border: 1px solid #ddd; padding: 8px; text-align: left; }\n"
            "    th { background: #f5f5f5; }\n"
            "    a { color: #0366d6; }\n"
            "    .meta { color: #666; margin-bottom: 1rem; }\n"
            "  </style>\n"
            "</head>\n"
            "<body>\n"
            f"  <h1>{safe_title}</h1>\n"
            f"  <p class=meta>Generated {_html.escape(generated_at)}</p>\n"
            "  <table>\n"
            "    <thead><tr><th>Filename</th><th>Size</th><th>Download</th></tr></thead>\n"
            f"    <tbody>\n{body_rows}    </tbody>\n"
            "  </table>\n"
            "</body>\n"
            "</html>\n"
        )

    @staticmethod
    def _human_size(n: int) -> str:
        if n < 1024:
            return f"{n} B"
        elif n < 1024 * 1024:
            return f"{n / 1024:.1f} KB"
        elif n < 1024 * 1024 * 1024:
            return f"{n / (1024 * 1024):.1f} MB"
        return f"{n / (1024 * 1024 * 1024):.2f} GB"

    @staticmethod
    def _build_publish_log(
        platform_label: str,
        url: str,
        visibility: str,
        local_path: str,
        *,
        title: str | None = None,
        timestamp: str | None = None,
    ) -> dict[str, Any]:
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        entry: dict[str, Any] = {
            "platform": platform_label,
            "status": "published",
            "url": url,
            "visibility": visibility,
            "export_path": local_path,
            "timestamp": ts,
        }
        if title:
            entry["metadata_used"] = {"title": title}
        return {"version": "1.0", "entries": [entry]}

    # ------------------------------------------------------------------ #
    # Project / key helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _infer_project_name(video_path: Path) -> str:
        # projects/<name>/renders/final.mp4 -> <name>; fall back to file stem.
        # We require >= 3 parents (e.g. root, name, renders, file) so that
        # paths like /tmp/foo.mp4 (2 parents) don't accidentally return ''.
        parents = video_path.resolve().parents
        if len(parents) >= 3:
            return parents[1].name
        return video_path.stem

    def _default_object_key(self, project_id: str, path: Path) -> str:
        stem = path.name
        if project_id:
            return f"videos/{project_id}/{stem}"
        return stem

    def _default_page_key(self, project_id: str) -> str:
        if project_id:
            return f"pages/{project_id}/delivery.html"
        return "pages/delivery.html"

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        msg = str(exc)
        for var in ("S3_ACCESS_KEY", "S3_SECRET_KEY"):
            val = os.environ.get(var, "")
            if val:
                msg = msg.replace(val, "[redacted]")
        return msg

    # ------------------------------------------------------------------ #
    # BaseTool overrides
    # ------------------------------------------------------------------ #

    def estimate_cost(self, inputs: dict[str, Any]) -> float:
        # S3 egress/upload pricing varies per provider; surface 0 and let
        # the cost tracker log it as "provider-billed".
        return 0.0

    def dry_run(self, inputs: dict[str, Any]) -> dict[str, Any]:
        # Light check: does the file exist + are the required env vars set?
        path = Path(inputs.get("video_path", "")).expanduser()
        ok = path.is_file()
        deps = []
        for dep in self.dependencies:
            if dep.startswith("env:"):
                deps.append(dep[4:])
        missing = [d for d in deps if not os.environ.get(d)]
        return {
            "ready": ok and not missing,
            "video_path_exists": ok,
            "missing_env": missing,
            "estimated_upload_mb": (path.stat().st_size / (1024 * 1024)) if ok else 0,
        }

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        start = time.time()

        if self.get_status() != ToolStatus.AVAILABLE:
            return ToolResult(
                success=False,
                error=(
                    "S3 credentials not configured. "
                    + self.install_instructions
                ),
            )

        video_path = Path(inputs.get("video_path", "")).expanduser()
        if not video_path.is_file():
            return ToolResult(
                success=False,
                error=f"video_path not found or not a file: {video_path}",
            )

        project_id = inputs.get("project_id") or self._infer_project_name(video_path)
        object_key = inputs.get("object_key") or self._default_object_key(project_id, video_path)
        visibility = (
            (inputs.get("visibility") or self._default_visibility() or "public")
        ).lower()
        if visibility not in ("public", "private"):
            visibility = "public"
        expire_seconds = int(inputs.get("expire_seconds") or 604800)
        expire_seconds = max(60, min(604800, expire_seconds))
        make_page = bool(inputs.get("make_download_page", False))
        additional_files: list[str] = inputs.get("additional_files") or []
        page_title: str = inputs.get("page_title") or f"Video Delivery — {project_id}"
        platform_label: str = inputs.get("platform_label") or "s3"

        uploaded: list[dict[str, Any]] = []
        try:
            uploaded.append(self._put_file(video_path, object_key))
        except Exception as exc:
            return ToolResult(
                success=False,
                error=f"video upload failed: {self._safe_error(exc)}",
                duration_seconds=round(time.time() - start, 2),
            )

        # Additional files (typically for the download page).
        for extra in additional_files:
            p = Path(extra).expanduser()
            if not p.is_file():
                return ToolResult(
                    success=False,
                    error=f"additional_files entry not found: {p}",
                    duration_seconds=round(time.time() - start, 2),
                )
            ek = self._default_object_key(project_id, p)
            try:
                uploaded.append(self._put_file(p, ek))
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"additional file upload failed ({p.name}): {self._safe_error(exc)}",
                    duration_seconds=round(time.time() - start, 2),
                )

        # Decide the primary delivery URL (the one the agent exposes to the
        # client — prefer the main video file, not the download page).
        primary = uploaded[0]
        if visibility == "public":
            final_url = primary["url"]
        else:
            final_url = self._presigned_get_url(
                self._endpoint(),
                self._bucket(),
                primary["object_key"],
                self._region(),
                self._ak(),
                self._sk(),
                expire_seconds,
                self._public_base(),
            )

        # Build and upload the download page if requested.
        download_page_url: str | None = None
        if make_page:
            page_items: list[dict[str, Any]] = []
            for item in uploaded:
                url = item["url"] if visibility == "public" else self._presigned_get_url(
                    self._endpoint(),
                    self._bucket(),
                    item["object_key"],
                    self._region(),
                    self._ak(),
                    self._sk(),
                    expire_seconds,
                    self._public_base(),
                )
                page_items.append({**item, "url": url})
            generated_at = datetime.now(timezone.utc).isoformat()
            html = self._build_download_page(page_items, page_title, generated_at)
            page_key = self._default_page_key(project_id)
            try:
                page_meta = self._put_bytes(
                    html.encode("utf-8"),
                    page_key,
                    "text/html; charset=utf-8",
                )
            except Exception as exc:
                return ToolResult(
                    success=False,
                    error=f"download page upload failed: {self._safe_error(exc)}",
                    duration_seconds=round(time.time() - start, 2),
                )
            download_page_url = page_meta["url"] if visibility == "public" else self._presigned_get_url(
                self._endpoint(),
                self._bucket(),
                page_key,
                self._region(),
                self._ak(),
                self._sk(),
                expire_seconds,
                self._public_base(),
            )

        publish_log = self._build_publish_log(
            platform_label,
            final_url,
            visibility,
            str(video_path.resolve()),
            title=page_title if make_page else None,
        )
        try:
            from schemas.artifacts import validate_artifact
            validate_artifact("publish_log", publish_log)
        except Exception as exc:  # pragma: no cover - defensive
            return ToolResult(
                success=False,
                error=f"publish_log failed schema validation: {exc}",
                duration_seconds=round(time.time() - start, 2),
            )

        data: dict[str, Any] = {
            "url": final_url,
            "object_key": primary["object_key"],
            "bucket": self._bucket(),
            "visibility": visibility,
            "download_page_url": download_page_url,
            "uploaded_files": [
                {
                    "object_key": it["object_key"],
                    "filename": it["filename"],
                    "size_bytes": it["size_bytes"],
                    "url": it["url"],
                }
                for it in uploaded
            ],
            "publish_log": publish_log,
        }
        if visibility == "private":
            data["expires_at"] = (
                datetime.now(timezone.utc) + timedelta(seconds=expire_seconds)
            ).isoformat()

        return ToolResult(
            success=True,
            data=data,
            artifacts=[str(video_path.resolve())],
            duration_seconds=round(time.time() - start, 2),
        )
