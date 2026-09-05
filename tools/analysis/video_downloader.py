"""Video downloader tool wrapping yt-dlp.

Downloads video, audio, or subtitles from YouTube, Shorts, Instagram Reels,
TikTok, and 1000+ other sites. Designed for reference video analysis — downloads
at analysis quality (720p), not production quality.

Per-user isolation: every call MUST supply ``userid`` and an ``output_dir``
that resolves under ``projects/<userid>/``. Rejects arbitrary filesystem
writes — see ``_validate_output_dir`` and the workspace contract in
``docs/todo.md`` ("video_downloader 工作区隔离 + 命名冲突防护").
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
from lib import paths as _paths  # imported for runtime PROJECTS_DIR lookup


class VideoDownloader(BaseTool):
    name = "video_downloader"
    version = "0.1.0"
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
        "required": ["url", "userid", "output_dir"],
        "properties": {
            "url": {"type": "string", "description": "Video URL to download"},
            "userid": {
                "type": "string",
                "pattern": "^[a-zA-Z0-9_-]{1,64}$",
                "description": "Caller user id (must match the MCP session's "
                               "X-VClaw-User-Id). Used as the workspace root for "
                               "the downloaded artifact — enforces per-user "
                               "isolation. Path-traversal characters are rejected "
                               "by the regex.",
            },
            "output_dir": {
                "type": "string",
                "description": "Directory for downloaded files. Must resolve "
                               "under projects/<userid>/ — anything outside is "
                               "rejected to enforce the workspace contract.",
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
        },
    }

    resource_profile = ResourceProfile(
        cpu_cores=1, ram_mb=512, vram_mb=0, disk_mb=2000,
        network_required=True,
    )
    idempotency_key_fields = ["url", "format", "max_resolution"]
    side_effects = ["downloads media files to output_dir"]
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

    @classmethod
    def _validate_output_dir(cls, output_dir: Path, userid: str) -> Path:
        """Resolve ``output_dir`` and verify it lives under ``projects/<userid>/``.

        Per-user isolation: a caller can never write into another user's
        workspace, into ``projects/_scratch/`` (now ``projects/<userid>/_scratch/``),
        or anywhere outside the projects tree.  Returns the absolute resolved
        Path on success; raises ``ValueError`` with a caller-actionable message
        on rejection.
        """
        # Path.resolve() also follows symlinks — necessary so a malicious caller
        # can't bypass the check by symlinking projects/<them>/ → /etc.
        resolved = output_dir.expanduser().resolve()
        user_root = (_paths.PROJECTS_DIR / userid).resolve()
        try:
            resolved.relative_to(user_root)
        except ValueError as e:
            raise ValueError(
                f"output_dir must resolve under {user_root} "
                f"(resolved to {resolved}; userid={userid!r})"
            ) from e
        return resolved

    @staticmethod
    def _url_hash(url: str) -> str:
        """Stable short hash of the URL — used in outtmpl to keep two
        different URLs in the same output_dir from clobbering each other.

        Same URL → same hash → yt-dlp overwrites the same file (idempotent).
        Different URLs → different hashes → no collision.
        """
        return hashlib.sha1(url.encode("utf-8")).hexdigest()[:8]

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

    def execute(self, inputs: dict[str, Any]) -> ToolResult:
        url = inputs["url"]
        # --- Per-user isolation: validate userid + output_dir before any
        # filesystem side effect. Both errors are loud and non-fatal to the
        # caller (ToolResult) so the MCP layer can surface them cleanly.
        try:
            userid = self._validate_userid(inputs.get("userid"))
            output_dir = self._validate_output_dir(Path(inputs["output_dir"]), userid)
        except ValueError as exc:
            return ToolResult(success=False, error=str(exc))

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
                data={"metadata": metadata, "platform": platform},
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
                data={"metadata": metadata, "platform": platform},
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
            },
            artifacts=artifacts,
            duration_seconds=round(elapsed, 2),
        )

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
