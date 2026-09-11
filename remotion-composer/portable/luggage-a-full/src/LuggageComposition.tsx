/**
 * LuggageComposition — self-contained, no external imports beyond Remotion
 * and React. Designed to be portable: this file + manifest.json + the asset
 * directory are the entire rendering surface.
 *
 * Public API:
 *   loadLuggageProps()         — reads + validates manifest.json, returns LuggageProps
 *   LuggageComposition         — the React component Remotion renders
 *   totalFramesFor(scenes)     — duration helper for Root.tsx
 */

import * as React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

// ──────────────────────────────────────────────────────────────────────
// Type definitions — mirror manifest.json. Keep in lock-step.
// ──────────────────────────────────────────────────────────────────────

export type SceneType =
  | "hero"
  | "host"
  | "stat"
  | "comparison"
  | "text"
  | "end"
  | "product";

export type Motion = "zoom_in" | "zoom_out" | "pan_right" | "pan_left";

export interface BaseScene {
  type: SceneType;
  /** seconds; total frame count = duration * 30 */
  duration: number;
  /** path relative to assetDir, e.g. "voice/00_hook.wav" */
  audio: string;
}

export interface HeroScene extends BaseScene {
  type: "hero";
  title: string;
  subtitle: string;
}

export interface HostScene extends BaseScene {
  type: "host";
  image: string;
  caption: string;
  captionEn?: string;
  motion?: Motion;
}

export interface StatScene extends BaseScene {
  type: "stat";
  stat: string;
  subtitle: string;
  accentColor?: string;
}

export interface ComparisonScene extends BaseScene {
  type: "comparison";
  title: string;
  leftLabel: string;
  leftValue: string;
  rightLabel: string;
  rightValue: string;
  leftColor?: string;
  rightColor?: string;
}

export interface TextScene extends BaseScene {
  type: "text";
  text: string;
  accentColor?: string;
}

export interface EndScene extends BaseScene {
  type: "end";
  text: string;
}

export interface ProductScene extends BaseScene {
  type: "product";
  image: string;
  caption?: string;
  captionEn?: string;
  annotation?: string;
  annotationColor?: string;
  motion?: Motion;
}

export type Scene =
  | HeroScene
  | HostScene
  | StatScene
  | ComparisonScene
  | TextScene
  | EndScene
  | ProductScene;

export interface LuggageProps {
  conceptId: "a" | "b" | "c";
  /** path relative to public/, e.g. "_staged/luggage-a-full" */
  assetDir: string;
  scenes: Scene[];
}

// ──────────────────────────────────────────────────────────────────────
// Constants
// ──────────────────────────────────────────────────────────────────────

const FPS = 30;
const VIDEO_WIDTH = 1920;
const VIDEO_HEIGHT = 1080;

// Brand watermark + caption bar rely on a fixed 1920×1080 canvas. The CSS
// pixel sizes below are tuned for that canvas; other resolutions would
// need proportional remapping (out of scope for this bundle).

// ──────────────────────────────────────────────────────────────────────
// Font fallbacks — bundle ships only the 5 weights used by LuggageComposition.
// CJK falls back to whatever the render host has installed; on Linux the
// host needs `apt install fonts-noto-cjk` (Noto Sans CJK SC is the primary
// fallback). See README.md "System requirements".
// ──────────────────────────────────────────────────────────────────────

const CJK_SANS =
  "'Noto Sans CJK SC', 'Noto Sans SC', 'PingFang SC', 'Microsoft YaHei'";

const FONT_DISPLAY = `'Space Grotesk', Inter, ${CJK_SANS}, system-ui, sans-serif`;
const FONT_BODY = `Inter, ${CJK_SANS}, system-ui, sans-serif`;
const FONT_MONO = `'JetBrains Mono', Consolas, Monaco, ${CJK_SANS}, monospace`;

// ──────────────────────────────────────────────────────────────────────
// Manifest loader — runtime validation with clear error messages.
// Designed for environments where the LLM may have produced an imprecise
// manifest. Fails loudly with the field name + scene index on bad input.
// ──────────────────────────────────────────────────────────────────────

const VALID_SCENE_TYPES: SceneType[] = [
  "hero",
  "host",
  "stat",
  "comparison",
  "text",
  "end",
  "product",
];

const VALID_MOTIONS: Motion[] = ["zoom_in", "zoom_out", "pan_right", "pan_left"];

function isString(v: unknown): v is string {
  return typeof v === "string" && v.length > 0;
}

function isPositiveNumber(v: unknown): v is number {
  return typeof v === "number" && v > 0 && Number.isFinite(v);
}

function isHexColor(v: unknown): v is string {
  return typeof v === "string" && /^#[0-9a-fA-F]{6}$/.test(v);
}

function isOptionalString(v: unknown): v is string | undefined {
  return v === undefined || (typeof v === "string" && v.length > 0);
}

function validateScene(raw: unknown, idx: number): Scene {
  if (typeof raw !== "object" || raw === null) {
    throw new Error(`scene[${idx}] is not an object: got ${typeof raw}`);
  }
  const s = raw as Record<string, unknown>;

  if (!isString(s.type) || !VALID_SCENE_TYPES.includes(s.type as SceneType)) {
    throw new Error(
      `scene[${idx}].type must be one of ${VALID_SCENE_TYPES.join("|")}; got ${JSON.stringify(s.type)}`,
    );
  }
  if (!isPositiveNumber(s.duration)) {
    throw new Error(`scene[${idx}].duration must be a positive number; got ${JSON.stringify(s.duration)}`);
  }
  if (!isString(s.audio)) {
    throw new Error(`scene[${idx}].audio must be a non-empty string; got ${JSON.stringify(s.audio)}`);
  }

  const base = { type: s.type, duration: s.duration, audio: s.audio };
  const t = s.type;

  if (t === "hero") {
    if (!isString(s.title)) throw new Error(`scene[${idx}] (hero).title missing`);
    if (!isString(s.subtitle)) throw new Error(`scene[${idx}] (hero).subtitle missing`);
    return { ...base, type: "hero", title: s.title, subtitle: s.subtitle };
  }
  if (t === "host") {
    if (!isString(s.image)) throw new Error(`scene[${idx}] (host).image missing`);
    if (!isString(s.caption)) throw new Error(`scene[${idx}] (host).caption missing`);
    if (!isOptionalString(s.captionEn)) throw new Error(`scene[${idx}] (host).captionEn invalid`);
    if (s.motion !== undefined && !VALID_MOTIONS.includes(s.motion as Motion)) {
      throw new Error(`scene[${idx}] (host).motion invalid: ${JSON.stringify(s.motion)}`);
    }
    return {
      ...base,
      type: "host",
      image: s.image,
      caption: s.caption,
      captionEn: s.captionEn,
      motion: s.motion as Motion | undefined,
    };
  }
  if (t === "stat") {
    if (!isString(s.stat)) throw new Error(`scene[${idx}] (stat).stat missing`);
    if (!isString(s.subtitle)) throw new Error(`scene[${idx}] (stat).subtitle missing`);
    if (s.accentColor !== undefined && !isHexColor(s.accentColor)) {
      throw new Error(`scene[${idx}] (stat).accentColor invalid hex: ${JSON.stringify(s.accentColor)}`);
    }
    return {
      ...base,
      type: "stat",
      stat: s.stat,
      subtitle: s.subtitle,
      accentColor: s.accentColor,
    };
  }
  if (t === "comparison") {
    for (const f of ["title", "leftLabel", "leftValue", "rightLabel", "rightValue"] as const) {
      if (!isString(s[f])) throw new Error(`scene[${idx}] (comparison).${f} missing`);
    }
    if (s.leftColor !== undefined && !isHexColor(s.leftColor)) {
      throw new Error(`scene[${idx}] (comparison).leftColor invalid hex`);
    }
    if (s.rightColor !== undefined && !isHexColor(s.rightColor)) {
      throw new Error(`scene[${idx}] (comparison).rightColor invalid hex`);
    }
    return {
      ...base,
      type: "comparison",
      title: s.title,
      leftLabel: s.leftLabel,
      leftValue: s.leftValue,
      rightLabel: s.rightLabel,
      rightValue: s.rightValue,
      leftColor: s.leftColor,
      rightColor: s.rightColor,
    };
  }
  if (t === "text") {
    if (!isString(s.text)) throw new Error(`scene[${idx}] (text).text missing`);
    if (s.accentColor !== undefined && !isHexColor(s.accentColor)) {
      throw new Error(`scene[${idx}] (text).accentColor invalid hex`);
    }
    return { ...base, type: "text", text: s.text, accentColor: s.accentColor };
  }
  if (t === "end") {
    if (!isString(s.text)) throw new Error(`scene[${idx}] (end).text missing`);
    return { ...base, type: "end", text: s.text };
  }
  // t === "product"
  if (!isString(s.image)) throw new Error(`scene[${idx}] (product).image missing`);
  if (!isOptionalString(s.caption)) throw new Error(`scene[${idx}] (product).caption invalid`);
  if (!isOptionalString(s.captionEn)) throw new Error(`scene[${idx}] (product).captionEn invalid`);
  if (!isOptionalString(s.annotation)) throw new Error(`scene[${idx}] (product).annotation invalid`);
  if (s.annotationColor !== undefined && !isHexColor(s.annotationColor)) {
    throw new Error(`scene[${idx}] (product).annotationColor invalid hex`);
  }
  if (s.motion !== undefined && !VALID_MOTIONS.includes(s.motion as Motion)) {
    throw new Error(`scene[${idx}] (product).motion invalid: ${JSON.stringify(s.motion)}`);
  }
  return {
    ...base,
    type: "product",
    image: s.image,
    caption: s.caption,
    captionEn: s.captionEn,
    annotation: s.annotation,
    annotationColor: s.annotationColor,
    motion: s.motion as Motion | undefined,
  };
}

export function loadLuggageProps(rawManifest: unknown): LuggageProps {
  if (typeof rawManifest !== "object" || rawManifest === null) {
    throw new Error(`manifest must be an object; got ${typeof rawManifest}`);
  }
  const m = rawManifest as Record<string, unknown>;

  if (m.conceptId !== "a" && m.conceptId !== "b" && m.conceptId !== "c") {
    throw new Error(`manifest.conceptId must be 'a'|'b'|'c'; got ${JSON.stringify(m.conceptId)}`);
  }
  if (!isString(m.assetDir)) {
    throw new Error(`manifest.assetDir must be a non-empty string`);
  }
  if (!Array.isArray(m.scenes)) {
    throw new Error(`manifest.scenes must be an array; got ${typeof m.scenes}`);
  }
  if (m.scenes.length === 0) {
    throw new Error(`manifest.scenes is empty — at least one scene required`);
  }

  const scenes: Scene[] = m.scenes.map((s, i) => validateScene(s, i));

  return {
    conceptId: m.conceptId,
    assetDir: m.assetDir,
    scenes,
  };
}

// ──────────────────────────────────────────────────────────────────────
// Ken Burns transform hook — scale + translate interpolation over the
// scene duration. Used by host/product scenes to give still images a
// sense of motion.
// ──────────────────────────────────────────────────────────────────────

interface KenBurnsTransform {
  scale: number;
  x: number;
  y: number;
}

function useKenBurnsTransform(
  motion: Motion | undefined,
  totalFrames: number,
): KenBurnsTransform {
  const frame = useCurrentFrame();
  const t = frame / Math.max(1, totalFrames);
  switch (motion ?? "zoom_in") {
    case "zoom_in":
      return { scale: interpolate(t, [0, 1], [1.0, 1.12]), x: 0, y: 0 };
    case "zoom_out":
      return { scale: interpolate(t, [0, 1], [1.15, 1.0]), x: 0, y: 0 };
    case "pan_right":
      return { scale: 1.1, x: interpolate(t, [0, 1], [-30, 30]), y: 0 };
    case "pan_left":
      return { scale: 1.1, x: interpolate(t, [0, 1], [30, -30]), y: 0 };
  }
}

// ──────────────────────────────────────────────────────────────────────
// Font loading — register the 6 bundled woff2 faces via the FontFace API
// at composition mount. Self-contained (no global font helper), async
// (won't block render — fallbacks kick in if the load is slow). Mirrors
// the main project's fonts.ts but inlined so this bundle has zero
// dependencies beyond remotion + react.
// ──────────────────────────────────────────────────────────────────────

interface FontSpec {
  family: string;
  weight: number;
  file: string;
}

const FONTS_TO_LOAD: FontSpec[] = [
  { family: "Inter", weight: 500, file: "fonts/Inter-wght500.woff2" },
  { family: "Inter", weight: 700, file: "fonts/Inter-wght700.woff2" },
  { family: "Inter", weight: 800, file: "fonts/Inter-wght800.woff2" },
  { family: "Inter", weight: 900, file: "fonts/Inter-wght900.woff2" },
  { family: "Space Grotesk", weight: 700, file: "fonts/SpaceGrotesk-wght700.woff2" },
  { family: "JetBrains Mono", weight: 400, file: "fonts/JetBrainsMono-wght400.woff2" },
];

const FontLoader: React.FC = () => {
  React.useEffect(() => {
    if (typeof document === "undefined") return;
    FONTS_TO_LOAD.forEach((spec) => {
      const url = staticFile(spec.file);
      const face = new FontFace(spec.family, `url(${url}) format("woff2")`, {
        weight: String(spec.weight),
        style: "normal",
      });
      face
        .load()
        .then((loaded) => document.fonts.add(loaded))
        .catch(() => {
          // Fallback chain in CSS will catch missing weights; ignore.
        });
    });
  }, []);
  return null;
};

// ──────────────────────────────────────────────────────────────────────
// Inlined sub-components (no project-local imports — keeps the bundle
// portable). Each is small enough to keep here; if any grows, split into
// a sibling file.
// ──────────────────────────────────────────────────────────────────────

const Watermark: React.FC = () => (
  <div
    style={{
      position: "absolute",
      top: 24,
      left: 32,
      zIndex: 100,
      color: "rgba(255, 255, 255, 0.92)",
      fontFamily: FONT_BODY,
      fontWeight: 700,
      fontSize: 22,
      letterSpacing: 2,
      textShadow: "0 2px 8px rgba(0,0,0,0.6)",
      pointerEvents: "none",
    }}
  >
    测评社 <span style={{ color: "#00AEEC" }}>bilibili</span>
  </div>
);

const CaptionBar: React.FC<{ zh: string; en?: string }> = ({ zh, en }) => (
  <div
    style={{
      position: "absolute",
      bottom: 56,
      left: "50%",
      transform: "translateX(-50%)",
      maxWidth: "70%",
      background: "rgba(255, 255, 255, 0.92)",
      color: "#1F2937",
      padding: "14px 28px",
      borderRadius: 4,
      fontFamily: FONT_BODY,
      fontWeight: 500,
      fontSize: 30,
      lineHeight: 1.4,
      textAlign: "center",
      pointerEvents: "none",
      boxShadow: "0 4px 20px rgba(0,0,0,0.25)",
    }}
  >
    {zh}
    {en && (
      <div
        style={{
          fontSize: 22,
          fontFamily: FONT_MONO,
          color: "#0F3460",
          marginTop: 6,
          letterSpacing: 1.5,
        }}
      >
        {en}
      </div>
    )}
  </div>
);

const AnnotationOverlay: React.FC<{ text: string; color?: string }> = ({
  text,
  color = "#F59E0B",
}) => (
  <div
    style={{
      position: "absolute",
      top: "18%",
      left: "50%",
      transform: "translate(-50%, -50%)",
      color,
      fontFamily: FONT_DISPLAY,
      fontWeight: 900,
      fontSize: 180,
      textShadow: `0 4px 30px rgba(0,0,0,0.7), 0 0 8px ${color}`,
      letterSpacing: -2,
      pointerEvents: "none",
    }}
  >
    {text}
  </div>
);

const HeroTitle: React.FC<{ title: string; subtitle: string }> = ({
  title,
  subtitle,
}) => (
  <AbsoluteFill
    style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      color: "#FFFFFF",
      fontFamily: FONT_DISPLAY,
      textShadow: "0 4px 30px rgba(0,0,0,0.7)",
    }}
  >
    <div style={{ fontSize: 140, fontWeight: 900, letterSpacing: -2 }}>{title}</div>
    <div style={{ fontSize: 44, fontWeight: 500, marginTop: 24, opacity: 0.85 }}>{subtitle}</div>
  </AbsoluteFill>
);

const StatCard: React.FC<{
  stat: string;
  subtitle: string;
  color: string;
}> = ({ stat, subtitle, color }) => (
  <AbsoluteFill
    style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      color,
      fontFamily: FONT_DISPLAY,
      textShadow: "0 4px 30px rgba(0,0,0,0.7)",
    }}
  >
    <div style={{ fontSize: 260, fontWeight: 900, letterSpacing: -4 }}>{stat}</div>
    <div style={{ fontSize: 48, fontWeight: 500, marginTop: 24, color: "#E5E7EB" }}>{subtitle}</div>
  </AbsoluteFill>
);

const ComparisonCard: React.FC<{
  title: string;
  leftLabel: string;
  leftValue: string;
  rightLabel: string;
  rightValue: string;
  leftColor: string;
  rightColor: string;
}> = ({ title, leftLabel, leftValue, rightLabel, rightValue, leftColor, rightColor }) => (
  <AbsoluteFill
    style={{
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      padding: 80,
      fontFamily: FONT_DISPLAY,
      color: "#FFFFFF",
    }}
  >
    <div style={{ fontSize: 56, fontWeight: 700, marginBottom: 60, textShadow: "0 4px 30px rgba(0,0,0,0.7)" }}>
      {title}
    </div>
    <div style={{ display: "flex", gap: 80 }}>
      <div style={{ textAlign: "center", color: leftColor }}>
        <div style={{ fontSize: 44, opacity: 0.85 }}>{leftLabel}</div>
        <div style={{ fontSize: 220, fontWeight: 900, marginTop: 12 }}>{leftValue}</div>
      </div>
      <div style={{ textAlign: "center", color: rightColor }}>
        <div style={{ fontSize: 44, opacity: 0.85 }}>{rightLabel}</div>
        <div style={{ fontSize: 220, fontWeight: 900, marginTop: 12 }}>{rightValue}</div>
      </div>
    </div>
  </AbsoluteFill>
);

const TextCard: React.FC<{
  text: string;
  color: string;
}> = ({ text, color }) => (
  <AbsoluteFill
    style={{
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      backgroundColor: "#FAFAFA",
      padding: 80,
      fontFamily: FONT_DISPLAY,
    }}
  >
    <div
      style={{
        fontSize: 88,
        fontWeight: 800,
        color,
        textAlign: "center",
        lineHeight: 1.3,
        whiteSpace: "pre-line",
      }}
    >
      {text}
    </div>
  </AbsoluteFill>
);

const EndTag: React.FC<{ text: string }> = ({ text }) => (
  <AbsoluteFill
    style={{
      backgroundColor: "#000000",
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
      fontFamily: FONT_DISPLAY,
    }}
  >
    <div
      style={{
        color: "#F5EBD5",
        fontSize: 72,
        fontWeight: 700,
        letterSpacing: 8,
        textTransform: "uppercase",
        textAlign: "center",
        padding: "0 120px",
      }}
    >
      {text}
    </div>
  </AbsoluteFill>
);

// ──────────────────────────────────────────────────────────────────────
// Scene renderer — dispatches on scene.type to one of the inlined
// sub-components.
// ──────────────────────────────────────────────────────────────────────

const SceneRenderer: React.FC<{
  scene: Scene;
  assetDir: string;
}> = ({ scene, assetDir }) => {
  const { fps } = useVideoConfig();
  const totalFrames = Math.round(scene.duration * fps);

  switch (scene.type) {
    case "hero":
      return <HeroTitle title={scene.title} subtitle={scene.subtitle} />;
    case "stat":
      return (
        <StatCard
          stat={scene.stat}
          subtitle={scene.subtitle}
          color={scene.accentColor ?? "#F59E0B"}
        />
      );
    case "comparison":
      return (
        <ComparisonCard
          title={scene.title}
          leftLabel={scene.leftLabel}
          leftValue={scene.leftValue}
          rightLabel={scene.rightLabel}
          rightValue={scene.rightValue}
          leftColor={scene.leftColor ?? "#2563EB"}
          rightColor={scene.rightColor ?? "#F59E0B"}
        />
      );
    case "text":
      return (
        <TextCard
          text={scene.text}
          color={scene.accentColor ?? "#1A1A2E"}
        />
      );
    case "end":
      return <EndTag text={scene.text} />;
    case "host": {
      const kb = useKenBurnsTransform(scene.motion, totalFrames);
      return (
        <AbsoluteFill style={{ backgroundColor: "#000" }}>
          <Img
            src={staticFile(`${assetDir}/${scene.image}`)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              transform: `scale(${kb.scale}) translate(${kb.x}px, ${kb.y}px)`,
              transformOrigin: "center center",
            }}
          />
          <CaptionBar zh={scene.caption} en={scene.captionEn} />
        </AbsoluteFill>
      );
    }
    case "product": {
      const kb = useKenBurnsTransform(scene.motion, totalFrames);
      return (
        <AbsoluteFill style={{ backgroundColor: "#0a0a0a" }}>
          <Img
            src={staticFile(`${assetDir}/${scene.image}`)}
            style={{
              width: "100%",
              height: "100%",
              objectFit: "cover",
              transform: `scale(${kb.scale}) translate(${kb.x}px, ${kb.y}px)`,
              transformOrigin: "center center",
            }}
          />
          {scene.annotation && (
            <AnnotationOverlay text={scene.annotation} color={scene.annotationColor} />
          )}
          {scene.caption && (
            <CaptionBar zh={scene.caption} en={scene.captionEn} />
          )}
        </AbsoluteFill>
      );
    }
  }
};

// ──────────────────────────────────────────────────────────────────────
// Main composition — BGM bed + per-scene Sequence (TTS audio + scene body)
// + brand watermark overlay. Renders into a fixed 1920×1080 canvas.
// ──────────────────────────────────────────────────────────────────────

export const LuggageComposition: React.FC<LuggageProps> = ({ assetDir, scenes }) => (
  <AbsoluteFill style={{ backgroundColor: "#0a0a0a", fontFamily: FONT_DISPLAY }}>
    <FontLoader />
    <Watermark />
    <Audio
      src={staticFile(`${assetDir}/music/bgm.wav`)}
      volume={() => 0.22}
      loop
    />
    {scenes.map((scene, i) => {
      const startFrame = scenes
        .slice(0, i)
        .reduce((acc, s) => acc + Math.round(s.duration * FPS), 0);
      const sceneFrames = Math.round(scene.duration * FPS);
      return (
        <Sequence
          key={i}
          from={startFrame}
          durationInFrames={sceneFrames}
          name={scene.type}
        >
          <Audio src={staticFile(`${assetDir}/${scene.audio}`)} volume={1} />
          <SceneRenderer scene={scene} assetDir={assetDir} />
        </Sequence>
      );
    })}
  </AbsoluteFill>
);

export function totalFramesFor(scenes: Scene[]): number {
  return scenes.reduce((acc, s) => acc + Math.round(s.duration * FPS), 0);
}

// Canvas dimensions exported for Root.tsx to wire into <Composition>.
export const CANVAS_WIDTH = VIDEO_WIDTH;
export const CANVAS_HEIGHT = VIDEO_HEIGHT;
export const CANVAS_FPS = FPS;
