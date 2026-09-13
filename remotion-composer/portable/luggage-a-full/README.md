# luggage-a-full — Self-contained Remotion bundle

A standalone Remotion project that re-renders the "5 hard metrics" luggage
concept without the surrounding OpenMontage codebase. Ships:

- Source: `src/index.tsx` (entry), `src/Root.tsx` (composition registration),
  `src/LuggageComposition.tsx` (self-contained component with all
  sub-components inlined), `src/manifest.json` (scene data).
- Fonts: `public/fonts/` — 6 woff2 faces (Inter 500/700/800/900,
  Space Grotesk 700, JetBrains Mono 400). Loaded via the FontFace API
  at mount.
- Assets (`images/`, `music/`, `voice/`): not in git — copied in at
  packaging time from `remotion-composer/public/_staged/luggage-a-full/`
  (the asset pipeline's canonical home; gitignored because regenerable).
  During local dev the bundle's `public/_staged/luggage-a-full/` is
  symlinked back to that location.
- Manifest loader: `loadLuggageProps()` validates manifest.json at
  module load and crashes the bundler early with a useful error message
  on bad input. Designed for environments where the LLM may have
  produced an imprecise manifest.

This directory lives at `remotion-composer/portable/luggage-a-full/`
inside the OpenMontage repo. It is NOT inside `public/_staged/` (that
path is gitignored wholesale — see the repo's `.gitignore`).

## System requirements

- **Node.js ≥ 18** (for the bundled `npm install`)
- **Chromium** — `apt install chromium-browser` (Debian/Ubuntu),
  `brew install chromium` (macOS), or point at an existing Chrome via
  `--chrome-executable=/path/to/chrome`
- **CJK fonts for Chinese characters** — `apt install fonts-noto-cjk`
  (Debian/Ubuntu). macOS/Windows ship with equivalent system fonts.

`fonts.ts` in the parent project's main `src/` uses Google-Fonts-hosted
CJK fallbacks (`'Noto Sans CJK SC', 'PingFang SC', 'Microsoft YaHei'`).
This bundle's fallback chain is identical — the bundled woff2 covers
Latin only, so Chinese text relies on the host's CJK system fonts.

## Usage

```bash
# 1. Install dependencies (≈30s on a cold cache)
npm install

# 2. List compositions
npx remotion compositions src/index.tsx
# → LuggageAFull        30      1920x1080      7800 (260.00 sec)

# 3. Render
npx remotion render src/index.tsx LuggageAFull out.mp4 \
    --chrome-executable=/usr/bin/chromium-browser \
    --chrome-flag=--no-sandbox \
    --chrome-flag=--disable-gpu \
    --chrome-flag=--disable-dev-shm-usage \
    --codec h264

# Single-frame smoke test:
npx remotion render src/index.tsx LuggageAFull /tmp/frame.mp4 \
    --frames=1300-1300 \
    --chrome-executable=/usr/bin/chromium-browser \
    --chrome-flag=--no-sandbox \
    --chrome-flag=--disable-gpu \
    --chrome-flag=--disable-dev-shm-usage \
    --codec h264
```

## Editing the scene data

`src/manifest.json` is the single source of truth. The TypeScript types
in `src/LuggageComposition.tsx` mirror the JSON shape. Edits flow like
this:

1. Edit `src/manifest.json` (scene durations, captions, accent colors).
2. `npx remotion compositions src/index.tsx` — confirm `durationInFrames`
   updated.
3. `npx remotion render src/index.tsx LuggageAFull out.mp4 …`.

`loadLuggageProps()` runs at module load and surfaces field-level errors
(e.g. "scene[3] (host).caption missing" or "scene[5] (stat).accentColor
invalid hex: '#abc'") — if the bundler crashes with one of these, the
manifest needs fixing before render.

## Packaging for shipment

The bundle is designed to be tar'd up and shipped intact. `package.sh`
copies the assets in from their canonical location and produces a
self-contained tarball:

```bash
bash package.sh
# → produces luggage-a-full-portable.tgz (~13 MB, contains everything)
```

`package.sh` runs `cp -rL` to dereference the symlinks in
`public/_staged/luggage-a-full/` (which point at the asset pipeline's
canonical output location), then tars the bundle. Recipients just
`tar xzf` and run the install/render commands above.

## What this bundle deliberately does NOT include

- **No LLM or agent code** — this is purely a renderer. The LLM that
  wrote the manifest is a separate concern.
- **No Python, no MCP, no Python tools.** The parent project's Python
  orchestration pipeline is not part of the portable unit.
- **No multi-composition support.** Only `LuggageAFull` is registered
  in `Root.tsx`. To render a different concept, edit `manifest.json`
  and `Root.tsx`'s `id` prop.
- **No advanced remotion features** beyond `<Composition>` + audio/image
  sequences + Ken Burns. No `delayRender`, no `prefetch`, no
  `getCompositions` overrides.

## When the LLM produces a bad manifest

The runtime validator catches:

| Bad input | Error message |
|---|---|
| `type` missing or unknown | `scene[N].type must be one of hero\|host\|stat\|...; got <value>` |
| `duration` is a string or 0 | `scene[N].duration must be a positive number; got <value>` |
| `audio` field empty | `scene[N].audio must be a non-empty string; got <value>` |
| `host` scene without `image` | `scene[N] (host).image missing` |
| `accentColor` not a 6-digit hex | `scene[N] (stat).accentColor invalid hex: <value>` |
| `motion` not in the enum | `scene[N] (host).motion invalid: <value>` |
| `scenes` is an empty array | `manifest.scenes is empty — at least one scene required` |

All errors are surfaced at module load, which means they crash the
bundler with a stack trace pointing at `LuggageComposition.tsx:loadLuggageProps`.
Fix the manifest and re-render.
