# Plan: Extend Remotion Asset Staging to audio / backgroundImage

**Status:** Pending
**Created:** 2026-08-02
**Owner:** agent (OpenMontage)
**Task:** task #1 in session tracker
**Files touched (target):** `tools/video/video_compose.py`, `remotion-composer/src/Explainer.tsx`

---

## Background

`video_compose._remotion_render()` stages local media into
`remotion-composer/public/_staged/` and rewrites the cut source to a
public-relative path so Remotion loads it via `staticFile()`. This is
**required** because headless Chrome (used by `npx remotion render`) runs the
composition on `http://localhost:3001` and refuses `file://` URLs from that
origin ("Not allowed to load local resource").

The staging fix (2026-08-02) currently covers **only `cuts[].source`**.

## Problem

Other local-resource fields in the Remotion composition are NOT staged, so
they hit the same `file://`-blocked failure when a pipeline passes local
absolute paths:

| Field | Consumer (JS) | Goes through `resolveAsset()`? |
|---|---|---|
| `audio.narration.src` | `Explainer.tsx:829` `<Audio>` | ✅ yes |
| `audio.music.src` | `Explainer.tsx:835` `<Audio>` | ✅ yes |
| `cut.backgroundImage` | `BackgroundImageLayer` via `Explainer.tsx:552` | ✅ yes (component calls `resolveAsset` at `Explainer.tsx:477`) |
| `cut.backgroundVideo` | `BackgroundVideoLayer` via `Explainer.tsx:541` | ✅ yes (component calls `resolveAsset` at `Explainer.tsx:514`) |
| `cut.images[]` (anime_scene) | `AnimeScene.tsx:265` | ✅ yes |
| `cut.backgroundImage` (screenshot_scene) | `ScreenshotScene.tsx:273` | ✅ yes |

Since every consumer already routes through `resolveAsset()` (the `Explainer.tsx`
version now returns `file://` URIs verbatim, and the staged path falls into the
`staticFile()` branch), **no JS-side changes are required** — the fix is purely
Python-side: stage these fields the same way as `cuts[].source`.

## Implementation Plan

### 1. Refactor staging into a reusable helper in `_remotion_render`

Extract the current inline staging loop (`video_compose.py` ~line 1320-1338)
into a method:

```python
def _stage_remotion_asset(self, source: str, idx: int, staged_dir: Path) -> str:
    """Stage a local media file into remotion-composer/public/_staged/ and
    return the public-relative path. http/https/data: sources pass through
    unchanged. Missing files pass through unchanged (Remotion will surface
    the load error)."""
    if not source or source.startswith(("http://", "https://", "data:")):
        return source
    resolved = Path(source.replace("file://", ""))
    if not resolved.exists():
        return source
    staged_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.md5(resolved.as_posix().encode()).hexdigest()[:8]
    target = staged_dir / f"{idx}_{digest}_{resolved.name}"
    if not target.exists():
        import shutil
        shutil.copy2(resolved, target)
    return f"_staged/{target.name}"
```

### 2. Apply it to every local-resource field

In `_remotion_render`, after staging `cuts`, stage the rest:

```python
# cuts[].source (existing) + cut.backgroundImage/backgroundVideo/images
for idx, cut in enumerate(props.get("cuts", [])):
    for field in ("source", "backgroundImage", "backgroundVideo"):
        if cut.get(field):
            cut[field] = self._stage_remotion_asset(cut[field], idx, staged_dir)
    if cut.get("images"):
        cut["images"] = [
            self._stage_remotion_asset(img, idx, staged_dir) for img in cut["images"]
        ]

# audio.narration.src / audio.music.src
audio = props.get("audio")
if audio:
    for layer in ("narration", "music"):
        if audio.get(layer, {}).get("src"):
            audio[layer]["src"] = self._stage_remotion_asset(
                audio[layer]["src"], idx=-1, staged_dir=staged_dir
            )
```

### 3. Tests

- Smoke-render a cut with `backgroundImage` set to a local image + a
  `text_card` on top (validates `backgroundImage` staging + overlay).
- Smoke-render with a local `audio.music.src` (any small mp3) and verify the
  final MP4 has the audio track.
- Anime scene with `images[]` (optional — needs an anime-style brief).
- Screenshot scene with `backgroundImage` (optional).

## Edge Cases / Notes

- `idx=-1` for audio: the hash already namespaces by content path, so the
  index is only a collision aid; audio/music reuse is fine.
- Do **not** touch JS `resolveAsset()` — its `file://` pass-through (already
  fixed) plus `staticFile()` branch covers all staged relative paths.
- `remotion-composer/public/_staged/` is already gitignored via
  `.gitignore:74` (`remotion-composer/public/*`); no VCS change needed.
- `backgroundVideo` staging copies the mp4 into `public/_staged/` — same
  `staticFile()` path, works for `OffthreadVideo` too.

## Acceptance Criteria

- [ ] `audio.music.src` local file renders into final MP4 audio track
- [ ] `cut.backgroundImage` local file shows behind a component cut
- [ ] `cut.backgroundVideo` local file plays behind a component cut
- [ ] anime_scene `images[]` and screenshot_scene `backgroundImage` staged
- [ ] existing single-image Ken Burns render still passes (regression)
