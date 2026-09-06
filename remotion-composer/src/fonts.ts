/**
 * Central font registry for every composition.
 *
 * Two rules this file exists to enforce:
 *
 * 1. Latin faces must actually be loaded. Declaring `fontFamily: "Inter"`
 *    without loading it silently falls back to a serif, so every render used
 *    the wrong typeface. We self-host static woff2 files under `public/fonts/`
 *    (one file per weight × subset, downloaded from Google Fonts at setup time)
 *    and register them via the FontFace API + `document.fonts.add()`.  A
 *    `delayRender()` handle is held until the browser confirms all faces are
 *    painted, so Remotion never captures a frame before the real typeface is
 *    active.  This is deliberately offline — no CDN dependency at render time.
 * 2. CJK coverage comes from the locally installed Noto CJK family, never from
 *    the network.  The render host already ships Noto CJK; the CSS fallback
 *    resolves it instantly and works offline.
 *
 * Always import a stack from here.  Do not inline font stacks in components.
 */

import { continueRender, delayRender, staticFile } from "remotion";

/**
 * Each entry is one static woff2 file.  Using one file per (family, weight,
 * style, subset) rather than a variable font avoids all axis-compatibility
 * issues and makes the unicode-range approach straightforward.
 *
 * Setup: run `node scripts/download-fonts.js` (or `make fonts`) to pull the
 * woff2 files from Google Fonts into `public/fonts/`.  Without the files the
 * render still proceeds (catch below unblocks after 20 s) but Latin text falls
 * back to a system serif.
 *
 * File naming: `{Family}-{wghtNNN}[-Italic][-subset].woff2`
 *   e.g. Inter-wght400.woff2, PlayfairDisplay-Italic-wght600.woff2
 */

interface FaceSpec {
  family: string;
  weight: string;
  style: "normal" | "italic";
  /** relative to public/fonts/ */
  file: string;
}

const FACES: FaceSpec[] = [
  // Inter — weights 300/400/500/600/700/800/900
  { family: "Inter", weight: "300", style: "normal", file: "Inter-wght300.woff2" },
  { family: "Inter", weight: "400", style: "normal", file: "Inter-wght400.woff2" },
  { family: "Inter", weight: "500", style: "normal", file: "Inter-wght500.woff2" },
  { family: "Inter", weight: "600", style: "normal", file: "Inter-wght600.woff2" },
  { family: "Inter", weight: "700", style: "normal", file: "Inter-wght700.woff2" },
  { family: "Inter", weight: "800", style: "normal", file: "Inter-wght800.woff2" },
  { family: "Inter", weight: "900", style: "normal", file: "Inter-wght900.woff2" },
  // Space Grotesk — weights 300/400/500/600/700
  { family: "Space Grotesk", weight: "300", style: "normal", file: "SpaceGrotesk-wght300.woff2" },
  { family: "Space Grotesk", weight: "400", style: "normal", file: "SpaceGrotesk-wght400.woff2" },
  { family: "Space Grotesk", weight: "500", style: "normal", file: "SpaceGrotesk-wght500.woff2" },
  { family: "Space Grotesk", weight: "600", style: "normal", file: "SpaceGrotesk-wght600.woff2" },
  { family: "Space Grotesk", weight: "700", style: "normal", file: "SpaceGrotesk-wght700.woff2" },
  // JetBrains Mono — weights 400/500/600/700
  { family: "JetBrains Mono", weight: "400", style: "normal", file: "JetBrainsMono-wght400.woff2" },
  { family: "JetBrains Mono", weight: "500", style: "normal", file: "JetBrainsMono-wght500.woff2" },
  { family: "JetBrains Mono", weight: "600", style: "normal", file: "JetBrainsMono-wght600.woff2" },
  { family: "JetBrains Mono", weight: "700", style: "normal", file: "JetBrainsMono-wght700.woff2" },
  // Playfair Display — roman 400–900, italic 400–700
  { family: "Playfair Display", weight: "400", style: "normal", file: "PlayfairDisplay-wght400.woff2" },
  { family: "Playfair Display", weight: "500", style: "normal", file: "PlayfairDisplay-wght500.woff2" },
  { family: "Playfair Display", weight: "600", style: "normal", file: "PlayfairDisplay-wght600.woff2" },
  { family: "Playfair Display", weight: "700", style: "normal", file: "PlayfairDisplay-wght700.woff2" },
  { family: "Playfair Display", weight: "800", style: "normal", file: "PlayfairDisplay-wght800.woff2" },
  { family: "Playfair Display", weight: "900", style: "normal", file: "PlayfairDisplay-wght900.woff2" },
  { family: "Playfair Display", weight: "400", style: "italic",  file: "PlayfairDisplay-Italic-wght400.woff2" },
  { family: "Playfair Display", weight: "500", style: "italic",  file: "PlayfairDisplay-Italic-wght500.woff2" },
  { family: "Playfair Display", weight: "600", style: "italic",  file: "PlayfairDisplay-Italic-wght600.woff2" },
  { family: "Playfair Display", weight: "700", style: "italic",  file: "PlayfairDisplay-Italic-wght700.woff2" },
];

// One-time registration guard.  The module is evaluated by every component that
// imports it, but FontFace objects only need to be created the first time.
// Using a module-scoped flag (not globalThis) because ES modules are singletons
// within a single import graph.
let fontsRegistered = false;

if (typeof document !== "undefined" && !fontsRegistered) {
  fontsRegistered = true;
  const handle = delayRender();

  const tasks = FACES.map(async (spec) => {
    const url = staticFile("fonts/" + spec.file);
    const face = new FontFace(spec.family, `url(${url}) format("woff2")`, {
      weight: spec.weight,
      style: spec.style,
    });
    const loaded = await face.load();
    document.fonts.add(loaded);
  });

  Promise.all(tasks)
    .then(() => document.fonts.ready)
    .then(() => continueRender(handle))
    .catch((err) => {
      // eslint-disable-next-line no-console
      console.error("[fonts] Font load error (render continues with fallback):", err);
      continueRender(handle);
    });
}

/**
 * CJK fallbacks, in resolution order: the Linux render host's Noto CJK first,
 * then the Google Fonts naming, then macOS and Windows system faces so the
 * Studio preview looks the same on a designer's machine.
 */
const CJK_SANS =
  "'Noto Sans CJK SC', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei'";
const CJK_SERIF =
  "'Noto Serif CJK SC', 'Noto Serif SC', 'Songti SC', SimSun";
const CJK_MONO = `'Noto Sans Mono CJK SC', ${CJK_SANS}`;

/** Body copy, labels, chart axes — the default for anything not a headline. */
export const SANS = `Inter, ${CJK_SANS}, system-ui, sans-serif`;

/** Headlines, stat readouts, section titles, captions. */
export const DISPLAY = `'Space Grotesk', Inter, ${CJK_SANS}, system-ui, sans-serif`;

/** Terminal scenes and code. */
export const MONO = `'JetBrains Mono', Consolas, Monaco, ${CJK_MONO}, monospace`;

/** Editorial / quote voice. */
export const SERIF = `'Playfair Display', Georgia, 'Times New Roman', ${CJK_SERIF}, serif`;

/**
 * Apple-keynote voice, used by ProductReveal.  SF Pro only exists on macOS, so
 * this degrades to Inter on the Linux render host — kept distinct from SANS so
 * the intent survives when previewing on a designer's machine.
 */
export const APPLE_DISPLAY = `'SF Pro Display', 'Helvetica Neue', Inter, ${CJK_SANS}, system-ui, sans-serif`;

/** CJK-first stack, for the Chinese row of bilingual captions. */
export const CJK = `${CJK_SANS}, system-ui, sans-serif`;
