"""Video downloader tool wrapping yt-dlp.

Downloads video, audio, or subtitles from YouTube, Shorts, Instagram Reels,
TikTok, and 1000+ other sites. Designed for reference video analysis — downloads
at analysis quality (720p), not production quality.

Per-user isolation mirrors the ``upload_asset`` pattern: the caller supplies
``project_id`` (default ``"_scratch"``) and the tool resolves the per-user
workspace root via ``ProjectWorkspace.for_current_principal(project_id)``.
The user-id is auto-injected from the MCP session — set up the
``X-VClaw-User-Id`` header (which originates from WeChat via the vclaw auth
chain) and the ``_user_id_ctx`` ContextVar carries it into every tool call.
For non-MCP callers (tests, scripts), passing ``userid`` explicitly falls
back to ``ProjectWorkspace.for_principal(Principal(...), project_id)``.

The workspace always lands under ``projects/users/<namespace_key>/<project_id>/``
— ``ProjectWorkspace.resolve()`` enforces the boundary (no symlink escapes,
no path traversal), so a malicious caller can never write outside the
authenticated user's namespace.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Any

from tools.base_tool import (
    BaseTool,
    Determinism,
    ExecutionMode,
    ResourceProfile,
    ToolResult,
    ToolStability,
    ToolStatus,
    ToolTier,
    ToolRuntime,
)


class VideoDownloader(BaseTool):
    name = "video_downloader"
    version = "0.2.0"
    tier = ToolTier.SOURCE
    capability = "source_ingest"
    provider = "yt-dlp"
    stability = ToolStability.PRODUCTION
    execution_mode = ExecutionMode.SYNC
    determinism = Determinism.DETERMINISTIC
    runtime = ToolRuntime.LOCAL

    dependencies = ["python:yt_dlp"]
    install_instructions = (
        "Install yt-dlp: pip install yt-dlp\n"
        "For YouTube support, also install Deno (JS runtime): "
        "https://deno.land/#installation\n"
        "Without Deno, YouTube downloads may fail but other platforms still work."
    )
    agent_skills = ["video-download"]

    capabilities = [
        "download_video",
        "download_audio",
        "download_subtitles",
        "extract_metadata",
    ]

    best_for = [
        "downloading reference video from URL",
        "extracting audio from online video",
        "downloading subtitles from YouTube",
        "getting video metadata without downloading",
    ]

    not_good_for = [
        "downloading entire playlists",
        "downloading DRM-protected content",
    ]

    input_schema = {
        "type": "object",
        "required": ["url"],
        "properties": {
            "url": {"type": "string", "description": "Video URL to download"},
            "project_id": {
                "type": "string",
                "default": "references",
                "pattern": "^[a-zA-Z0-9._-]{1,64}$",
                "description": "Project-scoped subdirectory under the "
                               "caller's principal namespace. The download "
                               "lands at projects/users/<userid>/<project_id>/. "
                               "Auto-injected from the MCP session via "
                               "ProjectWorkspace.for_current_principal() — "
                               "the same pattern as upload_asset. Defaults "
                               "to 'references' for ad-hoc reference fetches.",
            },
            "userid": {
                "type": "string",
                "pattern": "^[a-zA-Z0-9_-]{1,64}$",
                "description": "OPTIONAL fallback for non-MCP callers "
                               "(tests, scripts). When the MCP session's "
                               "X-VClaw-User-Id is set, the workspace is "
                               "auto-resolved and this input is ignored. "
                               "Path-traversal characters are rejected.",
            },
            "output_dir": {
                "type": "string",
                "description": "OPTIONAL sub-path relative to the "
                               "resolved workspace root. Default = the "
                               "workspace root itself (URL becomes the "
                               "filename via the URL hash). Forwarded "
                               "through ProjectWorkspace.resolve() which "
                               "rejects any path that escapes the user's "
                               "namespace.",
            },
            "format": {
                "type": "string",
                "enum": ["video", "audio_only", "subtitles_only", "metadata_only"],
                "default": "video",
                "description": "What to download",
            },
            "max_resolution": {
                "type": "string",
                "enum": ["360p", "480p", "720p", "1080p"],
                "default": "720p",
                "description": "Maximum video resolution (for analysis, 720p is sufficient)",
            },
            "max_duration_seconds": {
                "type": "integer",
                "default": 600,
                "description": "Reject videos longer than this (safety limit)",
            },
        },
    }

    output_schema = {
        "type": "object",
        "properties": {
            "video_path": {"type": ["string", "null"]},
            "audio_path": {"type": ["string", "null"]},
            "subtitle_path": {"type": ["string", "null"]},
            "metadata": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "duration": {"type": "number"},
                    "uploader": {"type": "string"},
                    "upload_date": {"type": "string"},
                    "description": {"type": "string"},
                    "view_count": {"type": "integer"},
                    "like_count": {"type": "integer"},
                },
            },
            "platform": {"type": "string"},
            "workspace_root": {
                "type": "string",
                "description": "Resolved per-user workspace root "
                               "(projects/users/<namespace_key>/<project_id>/). "
                               "Echoed back so callers can navigate to "
                               "downstream artifacts without re-resolving.",
            },
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=2000,
        network_required=True,
    )
    idempotency_key_fields = ["url", "format", "max_resolution"]
    side_effects = ["downloads media files under projects/users/<userid>/<project_id>/"]
    resume_support_value = "from_start"
    user_visible_verification = [
        "Check downloaded file plays correctly",
        "Verify resolution matches requested max",
    ]

    # --- Resolution mapping ---
    _RES_MAP = {
        "360p": 360,
        "480p": 480,
        "720p": 720,
        "1080p": 1080,
    }

    # userid charset: alphanumeric + dash/underscore, 1-64 chars. Stricter than
    # a typical UUID regex on purpose — anything fancier (dots, slashes,
    # whitespace, NUL, unicode) is rejected to prevent path injection.
    _USERID_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

    @classmethod
    def _validate_userid(cls, userid: Any) -> str:
        """Reject path-traversal / NUL / non-ASCII userids."""
        if not isinstance(userid, str) or not cls._USERID_RE.match(userid):
            raise ValueError(
                f"userid must match {cls._USERID_RE.pattern!r} "
                f"(got {type(userid).__name__}: {userid!r})"
            )
        return userid

    @staticmethod
    def _url_hash(url: str) -> str:
        """Stable short hash of the URL — used in outtmpl to keep two
        different URLs in the same output_dir from clobbering each other.

        Same URL → same hash → yt-dlp overwrites the same file (idempotent).
        Different URLs → different hashes → no collision.
        """
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]

    # ------------------------------------------------------------------ #
    # Workspace resolution — mirrors upload_asset's pattern.             #
    # ------------------------------------------------------------------ #

    @classmethod
    def _resolve_workspace(
        cls, inputs: dict[str, Any]
    ) -> tuple[Any, Path]:
        """Resolve (workspace, output_dir) for the current call.

        Mirrors ``upload_asset``'s pattern exactly:

        1. MCP path — ``ProjectWorkspace.for_current_principal(project_id)``
           reads the authenticated principal from the session ContextVar
           (populated by ``BearerTokenAuthMiddleware`` from the
           ``X-VClaw-User-Id`` header, which originates in the WeChat
           OAuth chain). User-id is auto-injected — the caller doesn't
           supply it.

        2. Fallback — when there's no MCP session (tests, scripts), the
           caller may pass ``userid`` explicitly. We construct a
           ``Principal`` and call ``ProjectWorkspace.for_principal(...)``.

        3. ``output_dir`` (optional) — when supplied, it's a sub-path
           relative to the resolved workspace root. We forward it through
           ``workspace.resolve(output_dir)`` which enforces the boundary
           (no path traversal, no symlink escapes). If omitted, the
           output lands directly at the workspace root.

        Returns ``(workspace, output_dir)`` on success or ``(ToolResult,
        None)`` carrying the failure. Two return shapes avoid an import
        cycle with ``mcp_server`` for the success case.
        """
        project_id = inputs.get("project_id") or "references"
        # Lazy imports — ``lib.project_workspace`` triggers a deferred
        # ``mcp_server`` import at call time, which would crash tools
        # loaded outside a FastMCP context (tests, scripts). Loading them
        # here keeps module import side-effect-free.
        from lib.project_workspace import ProjectWorkspace, WorkspaceErrorError
        from lib.principal_registry import (
            Principal,
            PrincipalNotFound,
        )

        try:
            explicit_userid = inputs.get("userid")
            if explicit_userid:
                cls._validate_userid(explicit_userid)
                principal = Principal(kind="user", principal_id=explicit_userid)
                workspace = ProjectWorkspace.for_principal(principal, project_id)
            else:
                # MCP path — auto-inject from session ContextVar.
                workspace = ProjectWorkspace.for_current_principal(project_id)
        except PrincipalNotFound as e:
            return (
                ToolResult(
                    success=False,
                    error=(
                        "no authenticated principal bound to this call. "
                        "Either send X-VClaw-User-Id on the MCP request, "
                        f"or pass userid explicitly. ({e})"
                    ),
                ),
                None,
            )
        except ValueError as e:
            return (
                ToolResult(success=False, error=f"workspace resolution failed: {e}"),
                None,
            )

        sub = inputs.get("output_dir")
        if sub:
            try:
                output_dir = workspace.resolve(sub)
            except (WorkspaceErrorError, ValueError) as e:
                return (
                    ToolResult(
                        success=False,
                        error=(
                            f"output_dir {sub!r} escapes the workspace root "
                            f"({workspace.root}): {e}"
                        ),
                    ),
                    None,
                )
        else:
            output_dir = workspace.root

        return workspace, output_dir

    # ------------------------------------------------------------------ #
    # Detection + metadata (unchanged)                                   #
    # ------------------------------------------------------------------ #

    def _detect_platform(self, url: str) -> str:
        """Detect platform from URL."""
        url_lower = url.lower()
        if "youtube.com/shorts" in url_lower or "youtu.be" in url_lower and "/shorts" in url_lower:
            return "shorts"
        if "youtube.com" in url_lower or "youtu.be" in url_lower:
            return "youtube"
        if "instagram.com" in url_lower:
            return "instagram"
        if "tiktok.com" in url_lower:
            return "tiktok"
        if "vimeo.com" in url_lower:
            return "vimeo"
        if "twitter.com" in url_lower or "x.com" in url_lower:
            return "twitter"
        return "other_url"

    def _extract_metadata(self, url: str) -> dict:
        """Extract metadata without downloading."""
        import yt_dlp

        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if info is None:
                    return {"error": "No info extracted", "title": "", "duration": 0}
                return {
                    "title": info.get("title", ""),
                    "duration": info.get("duration", 0),
                    "uploader": info.get("uploader", info.get("channel", "")),
                    "upload_date": info.get("upload_date", ""),
                    "description": (info.get("description", "") or "")[:500],
                    "view_count": info.get("view_count", 0),
                    "like_count": info.get("like_count", 0),
                    "resolution": f"{info.get('width', 0)}x{info.get('height', 0)}",
                    "fps": info.get("fps", 0),
                }
        except Exception as e:
            return {"error": str(e), "title": "", "duration": 0}

    # ------------------------------------------------------------------ #
    # Execute                                                            #
    # ------------------------------------------------------------------ #

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        url = inputs["url"]
        # --- Per-user isolation: resolve the workspace BEFORE any filesystem
        # side effect. The MCP session's X-VClaw-User-Id (WeChat-derived) is
        # auto-injected here via ProjectWorkspace — caller doesn't need to
        # think about it. Non-MCP callers can pass userid explicitly.
        workspace, output_dir = self._resolve_workspace(inputs)
        if isinstance(workspace, ToolResult):
            return workspace  # failure already wrapped

        dl_format = inputs.get("format", "video")
        max_res = inputs.get("max_resolution", "720p")
        max_duration = inputs.get("max_duration_seconds", 600)

        output_dir.mkdir(parents=True, exist_ok=True)
        platform = self._detect_platform(url)
        start = time.time()

        # Step 1: Always get metadata first
        metadata = self._extract_metadata(url)

        # Check duration limit
        duration = metadata.get("duration", 0)
        if duration and duration > max_duration:
            return ToolResult(
                success=False,
                error=(
                    f"Video is {duration}s, exceeds max_duration_seconds={max_duration}. "
                    f"Increase the limit or use a shorter video."
                ),
                data={
                    "metadata": metadata,
                    "platform": platform,
                    "workspace_root": str(workspace.root),
                },
            )

        if dl_format == "metadata_only":
            return ToolResult(
                success=True,
                data={
                    "video_path": None,
                    "audio_path": None,
                    "subtitle_path": None,
                    "metadata": metadata,
                    "platform": platform,
                    "workspace_root": str(workspace.root),
                },
                duration_seconds=round(time.time() - start, 2),
            )

        video_path = None
        audio_path = None
        subtitle_path = None

        try:
            if dl_format == "video":
                video_path, audio_path = self._download_video(
                    url, output_dir, max_res
                )
            elif dl_format == "audio_only":
                audio_path = self._download_audio(url, output_dir)
            elif dl_format == "subtitles_only":
                subtitle_path = self._download_subtitles(url, output_dir)
        except Exception as e:
            elapsed = time.time() - start
            return ToolResult(
                success=False,
                error=f"Download failed: {e}",
                data={
                    "metadata": metadata,
                    "platform": platform,
                    "workspace_root": str(workspace.root),
                },
                duration_seconds=round(elapsed, 2),
            )

        elapsed = time.time() - start
        artifacts = [p for p in [video_path, audio_path, subtitle_path] if p]

        return ToolResult(
            success=True,
            data={
                "video_path": video_path,
                "audio_path": audio_path,
                "subtitle_path": subtitle_path,
                "metadata": metadata,
                "platform": platform,
                "workspace_root": str(workspace.root),
            },
            artifacts=artifacts,
            duration_seconds=round(elapsed, 2),
        )

    # ------------------------------------------------------------------ #
    # yt-dlp subroutines                                                 #
    # ------------------------------------------------------------------ #

    def _download_video(
        self, url: str, output_dir: Path, max_res: str
    ) -> tuple[str | None, str | None]:
        """Download video + extract audio track."""
        import yt_dlp

        height = self._RES_MAP.get(max_res, 720)
        # URL hash → distinct filenames per URL in the same output_dir, so
        # two concurrent downloads (different URLs, same dir) don't clobber
        # each other. Same URL twice → same hash → yt-dlp overwrites
        # idempotently.
        url_hash = self._url_hash(url)
        video_out = str(output_dir / f"reference_video_{url_hash}.%(ext)s")

        ydl_opts = {
            "format": f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best",
            "merge_output_format": "mp4",
            "outtmpl": video_out,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        # Find the downloaded video file
        video_path = self._find_downloaded(output_dir, "reference_video", ["mp4", "mkv", "webm"])

        # Extract audio separately for transcription
        audio_path = None
        if video_path:
            audio_out = output_dir / "reference_audio.wav"
            try:
                audio_cmd = [
                    "ffmpeg", "-y",
                    "-i", video_path,
                    "-vn",
                    "-acodec", "pcm_s16le",
                    "-ar", "16000",
                    "-ac", "1",
                    str(audio_out),
                ]
                self.run_command(audio_cmd, timeout=120)
                if audio_out.exists():
                    audio_path = str(audio_out)
            except Exception:
                pass  # Audio extraction is optional

        return video_path, audio_path

    def _download_audio(self, url: str, output_dir: Path) -> str | None:
        """Download audio only."""
        import yt_dlp

        audio_out = str(output_dir / "reference_audio.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "wav",
                "preferredquality": "0",
            }],
            "outtmpl": audio_out,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
        return self._find_downloaded(output_dir, "reference_audio", ["wav", "mp3", "m4a", "opus"])

    def _download_subtitles(self, url: str, output_dir: Path) -> str | None:
        """Download subtitles only."""
        import yt_dlp

        sub_out = str(output_dir / "reference_subs.%(ext)s")
        ydl_opts = {
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": ["en"],
            "subtitlesformat": "srt",
            "skip_download": True,
            "outtmpl": sub_out,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])
        except Exception:
            pass
        return self._find_downloaded(output_dir, "reference_subs", ["srt", "vtt", "ass"])

    def _find_downloaded(
        self, output_dir: Path, prefix: str, extensions: list[str]
    ) -> str | None:
        """Find a downloaded file by prefix and possible extensions."""
        for ext in extensions:
            candidates = list(output_dir.glob(f"{prefix}*.{ext}"))
            if candidates:
                return str(candidates[0])
        return None
