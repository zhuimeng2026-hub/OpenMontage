"""Asset upload/list/delete for project directories.

MIME whitelists per subdir, size limits, safe-filename handling.
Storage: projects/<project_id>/assets/<subdir>/<safe_filename>
"""
from __future__ import annotations

import logging
import mimetypes
import re
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

_log = logging.getLogger("tweak_server.assets")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PROJECTS_DIR = PROJECT_ROOT / "projects"

# Whitelist per subdir
ALLOWED_MIMES: dict[str, set[str]] = {
    "images": {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"},
    "video": {"video/mp4", "video/webm", "video/quicktime"},
    "audio": {"audio/mpeg", "audio/wav", "audio/ogg", "audio/x-wav", "audio/mp3"},
    "music": {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp3"},
}

# Size limits per subdir (bytes)
SIZE_LIMITS: dict[str, int] = {
    "images": 50 * 1024 * 1024,    # 50 MB
    "video": 500 * 1024 * 1024,    # 500 MB
    "audio": 200 * 1024 * 1024,    # 200 MB
    "music": 200 * 1024 * 1024,    # 200 MB
}

SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _safe_filename(name: str) -> str:
    """Strip path components, normalize, ensure not empty."""
    name = Path(name).name  # strip any path
    name = SAFE_FILENAME_RE.sub("_", name)
    name = name.strip("._-") or "upload"
    return name[:200]


def _project_dir(project_id: str) -> Path:
    if "/" in project_id or "\\" in project_id or ".." in project_id:
        raise HTTPException(status_code=400, detail="invalid project_id")
    d = PROJECTS_DIR / project_id
    if not d.is_dir():
        raise HTTPException(status_code=404, detail=f"project {project_id!r} not found")
    return d


def _validate_subdir(subdir: str) -> str:
    if subdir not in ALLOWED_MIMES:
        raise HTTPException(
            status_code=400,
            detail=f"subdir must be one of {list(ALLOWED_MIMES)}, got {subdir!r}",
        )
    return subdir


async def save_asset(
    *, project_id: str, subdir: str, file: UploadFile
) -> dict[str, Any]:
    subdir = _validate_subdir(subdir)
    project_dir = _project_dir(project_id)
    target_dir = project_dir / "assets" / subdir
    target_dir.mkdir(parents=True, exist_ok=True)

    mime = (file.content_type or "").lower()
    if mime not in ALLOWED_MIMES[subdir]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "mime_not_allowed",
                "subdir": subdir,
                "got_mime": mime,
                "allowed": sorted(ALLOWED_MIMES[subdir]),
            },
        )

    # Stream-read to enforce size limit
    safe_name = _safe_filename(file.filename or "upload")
    target = target_dir / safe_name
    if target.exists():
        # Avoid clobbering: suffix with an incrementing counter
        stem, suf = target.stem, target.suffix
        i = 1
        while target.exists():
            target = target_dir / f"{stem}_{i}{suf}"
            i += 1

    size = 0
    limit = SIZE_LIMITS[subdir]
    chunk_size = 1024 * 1024  # 1 MB
    try:
        with open(target, "wb") as f:
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    f.close()
                    target.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=413,
                        detail={
                            "error": "file_too_large",
                            "limit_bytes": limit,
                            "subdir": subdir,
                        },
                    )
                f.write(chunk)
    finally:
        await file.close()

    _log.info(
        "asset saved: project=%s subdir=%s filename=%s size=%d",
        project_id, subdir, target.name, size,
    )
    return {
        "project_id": project_id,
        "subdir": subdir,
        "filename": target.name,
        "bytes": size,
        "mime": mime,
        "path": str(target.relative_to(project_dir)),
    }


def list_assets(project_id: str) -> dict[str, list[dict[str, Any]]]:
    project_dir = _project_dir(project_id)
    out: dict[str, list[dict[str, Any]]] = {sd: [] for sd in ALLOWED_MIMES}
    assets_root = project_dir / "assets"
    if not assets_root.is_dir():
        return out
    for subdir in ALLOWED_MIMES:
        d = assets_root / subdir
        if not d.is_dir():
            continue
        for f in sorted(d.iterdir()):
            if f.is_file():
                out[subdir].append({
                    "filename": f.name,
                    "bytes": f.stat().st_size,
                    "mime": _guess_mime(f),
                })
    return out


def _guess_mime(p: Path) -> str:
    mime, _ = mimetypes.guess_type(p.name)
    return mime or "application/octet-stream"


def delete_asset(*, project_id: str, subdir: str, filename: str) -> bool:
    subdir = _validate_subdir(subdir)
    project_dir = _project_dir(project_id)
    target = (project_dir / "assets" / subdir / _safe_filename(filename)).resolve()
    assets_dir = (project_dir / "assets" / subdir).resolve()
    # Prevent path traversal
    if not str(target).startswith(str(assets_dir)):
        raise HTTPException(status_code=400, detail="path traversal blocked")
    if not target.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    target.unlink()
    _log.info(
        "asset deleted: project=%s subdir=%s filename=%s",
        project_id, subdir, target.name,
    )
    return True
