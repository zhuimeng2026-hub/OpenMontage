"""Export a self-contained render bundle for a project.

Given an OM project id, gather every artifact that contributed to the rendered
output (input images, session asset metadata, the rendered mp4, review
frames, project.json) and emit a single directory that can be re-rendered
on any host with the required toolchain.

Default ``flavor="remotion"`` produces a Remotion 4.0.508 bundle — chosen as
the future primary business surface (per project decision 2026-09-08). The
``flavor="ffmpeg"`` shortcut is intentionally absent: ``flavor="remotion"``
is the supported path now, and we want a single canonical bundle shape.

Why we keep it OM-side (not vclaw-side)
----------------------------------------
vclaw only knows SQLite rows + the binary protocol; it doesn't have a
filesystem view of the per-user assets directory the way OM does. The
on-disk layout under ``projects/users/<ns>/<id>/`` is OM's domain, so the
exporter lives here and vclaw can call it through the MCP ``tools/call``
dispatcher if it ever needs to.

CLI
---
    python -m tools.export_render_bundle <project_id> [--out /tmp/bundles]

Library
-------
    from tools.export_render_bundle import export_render_bundle
    result = export_render_bundle("20260908-b50a93fde047",
                                  output_dir="/tmp/bundles",
                                  flavor="remotion")
    print(result["bundle_path"], result["file_count"])
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Iterable

# --- Paths (locked at import time so the CLI behavior is reproducible) ---
PROJECTS_ROOT = Path("/opt/OpenMontage_Voicebox/projects")
REMOTION_VERSION = "4.0.508"
REACT_VERSION = "^18.2.0"

# Bundle layout constants
_BUNDLE_NAME_TMPL = "{project_id}-bundle"
_IMAGES_DIR = "images"
_PUBLIC_STAGED = "_staged"  # remotion staticFile() reads from public/
_OUTPUT_MP4 = "output.mp4"
_MANIFEST = "manifest.json"
_INPUTS_JSON = "inputs.json"
_README = "README.md"
_REVIEW_DIR = "review"
_COMPOSITION_DIR = "composition"

# Asset-naming patterns we look for
_REMIX_RE = re.compile(r"^remix-(\d+)\.png$", re.IGNORECASE)


def _find_user_namespaces(project_id: str) -> list[str]:
    """Return the list of user namespaces that hold assets for this project.

    Scans ``projects/users/*/<project_id>`` and returns the matching
    ``<namespace>`` directories. Multiple namespaces are common because
    different MCP sessions under different users can upload to the same
    project id (especially for shared / debug projects).
    """
    users_root = PROJECTS_ROOT / "users"
    if not users_root.is_dir():
        return []
    found = []
    for ns in sorted(users_root.iterdir()):
        if (ns / project_id).is_dir():
            found.append(ns.name)
    return found


def _gather_session_assets(project_id: str) -> list[Path]:
    """Collect AI-generated / user-uploaded image files for the project.

    Walks ``assets/_sessions/<session>/remix-*.png`` under every user
    namespace that owns the project. Returns absolute Paths sorted by the
    numeric suffix on the filename (remix-001, remix-002, ...).
    """
    images: list[Path] = []
    for ns in _find_user_namespaces(project_id):
        sess_root = PROJECTS_ROOT / "users" / ns / project_id / "assets" / "_sessions"
        if not sess_root.is_dir():
            continue
        for session_dir in sess_root.iterdir():
            if not session_dir.is_dir():
                continue
            for f in session_dir.iterdir():
                if _REMIX_RE.match(f.name):
                    images.append(f.resolve())
    images.sort(key=lambda p: int(_REMIX_RE.match(p.name).group(1)))
    return images


def _gather_render_outputs(project_id: str) -> list[Path]:
    """Return rendered mp4 files for the project, newest first."""
    outputs: list[Path] = []
    for ns in _find_user_namespaces(project_id):
        rend = PROJECTS_ROOT / "users" / ns / project_id / "renders"
        if not rend.is_dir():
            continue
        outputs.extend(p for p in rend.glob("*.mp4"))
    outputs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return outputs


def _gather_review_frames(project_id: str) -> list[Path]:
    """Return review-frame PNGs from ``renders/.final_review_frames/``."""
    frames: list[Path] = []
    for ns in _find_user_namespaces(project_id):
        rev = PROJECTS_ROOT / "users" / ns / project_id / "renders" / ".final_review_frames"
        if not rev.is_dir():
            continue
        frames.extend(p for p in rev.glob("*.png"))
    return sorted(frames, key=lambda p: p.name)


def _gather_session_metadata(project_id: str) -> list[dict]:
    """Read ``projects/.mcp_sessions/<hash>.json`` for any session that
    touched this project. Returns the parsed JSON dicts (with project_id
    filter applied to skip unrelated sessions that happen to share a hash).
    """
    meta_dir = PROJECTS_ROOT / ".mcp_sessions"
    if not meta_dir.is_dir():
        return []
    out = []
    for p in meta_dir.glob("*.json"):
        try:
            d = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if d.get("project_id") == project_id:
            out.append(d)
    return out


def _read_root_project_json(project_id: str) -> dict | None:
    p = PROJECTS_ROOT / project_id / "project.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _copy_or_link(src: Path, dst: Path, *, link: bool = False) -> None:
    """Copy (or symlink) ``src`` to ``dst``. Symlinks are cheaper but
    break if the original is moved; we default to copy for portability."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if link:
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        os.symlink(src.resolve(), dst)
    else:
        shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# Remotion bundle generation
# ---------------------------------------------------------------------------

_REMOTION_PKG_JSON = """\
{
  "name": "{bundle_name}-composition",
  "version": "0.1.0",
  "private": true,
  "scripts": {
    "build": "remotion render Composition out.mp4",
    "start": "remotion studio"
  },
  "dependencies": {
    "@remotion/cli": "{remotion_version}",
    "react": "{react_version}",
    "react-dom": "{react_version}",
    "remotion": "{remotion_version}"
  },
  "devDependencies": {
    "@types/react": "^18.2.0",
    "@types/web": "^0.0.100",
    "typescript": "^5.4.5"
  }
}
"""

_REMOTION_TSCONFIG = """\
{
  "compilerOptions": {
    "target": "ES2020",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "jsx": "react-jsx",
    "strict": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "isolatedModules": true,
    "resolveJsonModule": true
  },
  "include": ["src"]
}
"""

_REMOTION_INDEX_TSX = """\
import {registerRoot} from 'remotion';
import {Root} from './Root';
registerRoot(Root);
"""

_REMOTION_ROOT_TSX = """\
import {Composition} from 'remotion';
import {MyComposition} from './Composition';
import {fps, durationInFrames, width, height} from './render-config.json';

export const Root = () => (
  <Composition
    id="Composition"
    component={MyComposition}
    fps={fps}
    durationInFrames={durationInFrames}
    width={width}
    height={height}
    defaultProps={{images: [], durationPerImage: 3}}
  />
);
"""

_REMOTION_COMPOSITION_TSX = """\
import {AbsoluteFill, Img, Sequence, staticFile, useCurrentFrame} from 'remotion';

export interface MyCompositionProps {
  images: string[];
  durationPerImage?: number;
}

export const MyComposition = ({images, durationPerImage = 3}: MyCompositionProps) => {
  const frame = useCurrentFrame();
  const fpi = Math.round(durationPerImage * 30);
  const total = images.length * fpi;
  const idx = Math.min(Math.floor(frame / fpi), Math.max(images.length, 1) - 1);
  // 6-frame crossfade between adjacent images.
  const FADE = 6;
  const fadeProgress =
    frame % fpi < FADE && idx < images.length - 1
      ? (frame % fpi) / FADE
      : idx > 0 && frame % fpi >= fpi - FADE
      ? 1 - ((frame % fpi) - (fpi - FADE)) / FADE
      : 1;

  return (
    <AbsoluteFill style={{backgroundColor: '#000'}}>
      {images.map((src, i) => (
        <Sequence key={i} from={i * fpi} durationInFrames={fpi}>
          <AbsoluteFill>
            <Img
              src={staticFile(src)}
              style={{
                width: '100%',
                height: '100%',
                objectFit: 'cover',
                opacity: i === idx ? fadeProgress : 0,
              }}
            />
          </AbsoluteFill>
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
"""

_RENDER_SH = """\
#!/usr/bin/env bash
# Re-render this bundle. Run from the composition/ directory.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d node_modules ]; then
  echo "==> installing dependencies (~500MB; first run only)"
  npm install --no-audit --no-fund --loglevel=error
fi
echo "==> rendering Composition -> ../output.mp4"
npx remotion render Composition ../output.mp4
echo "==> done: $(realpath ../output.mp4)"
"""

_BUNDLE_README = """\
# {project_id} render bundle

Self-contained Remotion bundle exported by ``tools/export_render_bundle.py``.

## What's inside

```
images/            AI-generated keyframes, names preserved
composition/       Remotion 4.0.508 project (npm i && npx remotion render)
review/            .final_review_frames PNGs from the original render
output.mp4         Last rendered mp4 from the source project (if any)
inputs.json        asset_manifest (Remotion input props)
manifest.json      Provenance: source paths, sha256, timestamps, namespaces
```

## Reproduce the render

```bash
cd composition
bash render.sh
```

Outputs to ``../output.mp4`` relative to the ``composition/`` directory.

## Re-link images without re-rendering

The bundle copies (not symlinks) images into
``composition/public/_staged/<project_id>/`` so it works on hosts that don't
have the original OM filesystem mounted. If you DO have the original mounted
and want to save 100s of MB, replace the copies with symlinks:

```bash
SRC=/opt/OpenMontage_Voicebox/projects/users/<ns>/<project_id>
DST=composition/public/_staged/<project_id>
rm -rf "$DST" && ln -s "$SRC/assets/_sessions" "$DST"
```
"""


def _build_remotion_bundle(
    project_id: str,
    images: list[Path],
    render_outputs: list[Path],
    review_frames: list[Path],
    session_meta: list[dict],
    root_project_json: dict | None,
    bundle_root: Path,
) -> dict:
    """Materialize the Remotion bundle under ``bundle_root``."""
    bundle_root.mkdir(parents=True, exist_ok=True)

    # --- images/  +  composition/public/_staged/<id>/ ---
    images_dir = bundle_root / _IMAGES_DIR
    images_dir.mkdir(exist_ok=True)
    comp_public_staged = bundle_root / _COMPOSITION_DIR / "public" / _PUBLIC_STAGED / project_id
    comp_public_staged.mkdir(parents=True, exist_ok=True)

    image_records = []
    for img in images:
        # 1) plain copy under images/
        _copy_or_link(img, images_dir / img.name, link=False)
        # 2) Remotion needs it under public/_staged/ to use staticFile()
        _copy_or_link(img, comp_public_staged / img.name, link=False)
        image_records.append({
            "filename": img.name,
            "source_path": str(img),
            "bytes": img.stat().st_size,
            "sha256": _sha256_of_file(img),
        })

    # --- output.mp4 (last rendered, if any) ---
    output_record = None
    if render_outputs:
        latest = render_outputs[0]
        dst = bundle_root / _OUTPUT_MP4
        _copy_or_link(latest, dst, link=False)
        output_record = {
            "source_path": str(latest),
            "bytes": latest.stat().st_size,
            "mtime": latest.stat().st_mtime,
        }

    # --- review/ ---
    review_dir = bundle_root / _REVIEW_DIR
    review_dir.mkdir(exist_ok=True)
    for f in review_frames:
        _copy_or_link(f, review_dir / f.name, link=False)

    # --- inputs.json (Remotion props) ---
    inputs = {
        "images": [f"_staged/{project_id}/{r['filename']}" for r in image_records],
        "durationPerImage": 3,
        "fps": 30,
    }
    # Try to recover aspect ratio from the rendered output via ffprobe if present
    if output_record and shutil.which("ffprobe"):
        w, h = _ffprobe_size(Path(output_record["source_path"]))
        if w and h:
            inputs["width"] = w
            inputs["height"] = h
    inputs.setdefault("width", 1080)
    inputs.setdefault("height", 1920)  # default vertical for this codebase
    (bundle_root / _INPUTS_JSON).write_text(json.dumps(inputs, indent=2) + "\n")

    # --- composition/ (Remotion project skeleton) ---
    comp = bundle_root / _COMPOSITION_DIR
    comp.mkdir(exist_ok=True)
    src = comp / "src"
    src.mkdir(exist_ok=True)
    (src / "index.tsx").write_text(_REMOTION_INDEX_TSX)
    (src / "Root.tsx").write_text(_REMOTION_ROOT_TSX)
    (src / "Composition.tsx").write_text(_REMOTION_COMPOSITION_TSX)
    (src / "render-config.json").write_text(json.dumps({
        "fps": inputs["fps"],
        "durationInFrames": len(image_records) * int(inputs["durationPerImage"] * inputs["fps"]),
        "width": inputs["width"],
        "height": inputs["height"],
    }, indent=2) + "\n")
    (comp / "package.json").write_text(
        _REMOTION_PKG_JSON
        .replace("{bundle_name}", project_id)
        .replace("{remotion_version}", REMOTION_VERSION)
        .replace("{react_version}", REACT_VERSION)
    )
    (comp / "tsconfig.json").write_text(_REMOTION_TSCONFIG)
    render_sh = comp / "render.sh"
    render_sh.write_text(_RENDER_SH)
    os.chmod(render_sh, 0o755)

    # --- manifest.json (provenance) ---
    manifest = {
        "project_id": project_id,
        "exported_at": _now_iso(),
        "remotion_version": REMOTION_VERSION,
        "namespaces": _find_user_namespaces(project_id),
        "root_project_json": root_project_json,
        "images": image_records,
        "render_output": output_record,
        "review_frames": [{"name": f.name, "bytes": f.stat().st_size} for f in review_frames],
        "session_metadata_files": [
            {"project_id": m.get("project_id"),
             "session_hash": m.get("session_hash"),
             "batch_id": m.get("batch_id"),
             "status": m.get("status")}
            for m in session_meta
        ],
    }
    (bundle_root / _MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")

    # --- README.md ---
    (bundle_root / _README).write_text(_BUNDLE_README.replace("{project_id}", project_id))

    return {
        "bundle_path": str(bundle_root),
        "file_count": sum(1 for _ in bundle_root.rglob("*") if _.is_file()),
        "total_bytes": sum(p.stat().st_size for p in bundle_root.rglob("*") if p.is_file()),
        "image_count": len(image_records),
        "render_output_included": output_record is not None,
    }


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sha256_of_file(p: Path) -> str:
    import hashlib
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _ffprobe_size(p: Path) -> tuple[int | None, int | None]:
    """Best-effort width/height extraction; returns (None, None) on any failure."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0 or not out.stdout.strip():
            return None, None
        parts = [p for p in out.stdout.strip().split(",") if p]
        if len(parts) < 2:
            return None, None
        return int(parts[0]), int(parts[1])
    except Exception:
        return None, None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def export_render_bundle(
    project_id: str,
    *,
    output_dir: str | os.PathLike = "/tmp/bundles",
    flavor: str = "remotion",
) -> dict:
    """Materialize a render bundle for ``project_id`` under ``output_dir``.

    Returns a dict with at least: ``bundle_path``, ``file_count``,
    ``total_bytes``, ``image_count``, ``render_output_included``.
    Raises ``FileNotFoundError`` if no images or no project artifacts
    are found (so the caller can distinguish "nothing to bundle" from
    "bundle written successfully").
    """
    if flavor != "remotion":
        raise ValueError(f"flavor={flavor!r} not supported; only 'remotion' is wired")

    images = _gather_session_assets(project_id)
    renders = _gather_render_outputs(project_id)
    reviews = _gather_review_frames(project_id)
    sessions = _gather_session_metadata(project_id)
    root_pj = _read_root_project_json(project_id)

    if not images and not renders and not reviews:
        raise FileNotFoundError(
            f"No artifacts found for project_id={project_id!r} under {PROJECTS_ROOT}"
        )

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    bundle_root = out_root / _BUNDLE_NAME_TMPL.format(project_id=project_id)

    return _build_remotion_bundle(
        project_id=project_id,
        images=images,
        render_outputs=renders,
        review_frames=reviews,
        session_meta=sessions,
        root_project_json=root_pj,
        bundle_root=bundle_root,
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("project_id", help="OM project id, e.g. 20260908-b50a93fde047")
    ap.add_argument("--out", default="/tmp/bundles",
                    help="Output parent directory (default /tmp/bundles)")
    ap.add_argument("--flavor", default="remotion", choices=["remotion"],
                    help="Bundle flavor (only 'remotion' supported)")
    args = ap.parse_args(argv)

    try:
        result = export_render_bundle(args.project_id, output_dir=args.out,
                                      flavor=args.flavor)
    except FileNotFoundError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 1

    print(f"OK: bundle at {result['bundle_path']}")
    print(f"    files:    {result['file_count']}")
    print(f"    bytes:    {result['total_bytes']}")
    print(f"    images:   {result['image_count']}")
    print(f"    render:   {'included' if result['render_output_included'] else 'none'}")
    print(f"\nNext: cd {result['bundle_path']}/composition && bash render.sh")
    return 0


if __name__ == "__main__":
    sys.exit(main())
