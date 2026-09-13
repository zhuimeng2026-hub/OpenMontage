/**
 * LuggageConcepts — three 60s explainer compositions, all scene-list driven.
 *
 * Used for the reference-driven remix of `sources/reference_video.mp4`. The
 * three concepts ("A: 5 hard metrics", "B: 3 scams to avoid", "C: 3 engineering
 * secrets") share the same render contract: a list of scenes, each with a
 * `type` (hero / stat / comparison / text / end), per-scene TTS audio, and a
 * shared BGM bed. The Chinese voice is synthesized offline via Kokoro; the
 * music bed via MusicGen-small; no external API calls.
 *
 * Stages: assets are placed under `public/_staged/luggage-<a|b|c>/` and
 * referenced with `staticFile()`.
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
import { DISPLAY, CJK, MONO } from "./fonts";
import { HeroTitle } from "./components/HeroTitle";
import { StatCard } from "./components/StatCard";
import { ComparisonCard } from "./components/ComparisonCard";
import { TextCard } from "./components/TextCard";
import { EndTag } from "./components/EndTag";

// -- Scene definitions ------------------------------------------------------

export interface LuggageSceneBase {
  /** seconds; total frame count = duration * fps (30) */
  duration: number;
  /** path relative to the asset base, e.g. "voice/scene_02.wav" */
  audio: string;
  /** Chinese word-level captions aligned to the TTS audio (optional but used by
   * the bilingual caption overlay). If omitted, the overlay shows no captions. */
  captions?: { text: string; end: number; start: number }[];
}

export interface HeroScene extends LuggageSceneBase {
  type: "hero";
  title: string;
  subtitle: string;
}
export interface StatScene extends LuggageSceneBase {
  type: "stat";
  stat: string;
  subtitle: string;
  accentColor?: string;
}
export interface ComparisonScene extends LuggageSceneBase {
  type: "comparison";
  title: string;
  leftLabel: string;
  leftValue: string;
  rightLabel: string;
  rightValue: string;
  leftColor?: string;
  rightColor?: string;
}
export interface TextScene extends LuggageSceneBase {
  type: "text";
  text: string;
  accentColor?: string;
}
export interface EndScene extends LuggageSceneBase {
  type: "end";
  text: string;
}
/**
 * Full-version talking-head scene. Renders a still host photo with Ken Burns
 * (slow zoom + pan) and an optional bottom caption. Used to approximate the
 * reference video's host-talking-head shots without external video gen.
 */
export interface HostScene extends LuggageSceneBase {
  type: "host";
  image: string;       // path relative to assetDir, e.g. "images/host.png"
  caption: string;     // Chinese bottom caption (English overlay rendered too)
  captionEn?: string;  // optional English keyword shown as overlay
  /** Ken Burns motion: "zoom_in" | "zoom_out" | "pan_right" | "pan_left" (default zoom_in). */
  motion?: "zoom_in" | "zoom_out" | "pan_right" | "pan_left";
}
/**
 * Full-version product shot. Still image with Ken Burns, optional overlay
 * stat annotation, optional caption. Replaces abstract StatCard with
 * real-feeling product imagery (minimax-generated).
 */
export interface ProductScene extends LuggageSceneBase {
  type: "product";
  image: string;
  caption?: string;
  captionEn?: string;
  annotation?: string;  // big overlay text on top of image (e.g., "≥100 次")
  annotationColor?: string;
  motion?: "zoom_in" | "zoom_out" | "pan_right" | "pan_left";
}
export type LuggageScene =
  | HeroScene
  | StatScene
  | ComparisonScene
  | TextScene
  | EndScene
  | HostScene
  | ProductScene;

/**
 * Component props. Extends `Record<string, unknown>` to satisfy Remotion's
 * `<Composition component={...}>` typing — Remotion treats `defaultProps`
 * as `Record<string, unknown>` and forwards them, so the component type
 * must accept arbitrary keys (Remotion's `LooseComponentType` constraint).
 */
export interface LuggageConceptsProps extends Record<string, unknown> {
  conceptId: "a" | "b" | "c";
  /** asset dir relative to public/, e.g. "_staged/luggage-a/" */
  assetDir: string;
  scenes: LuggageScene[];
}

// -- Scene renderer ---------------------------------------------------------

interface SceneRendererProps {
  scene: LuggageScene;
  accentColor: string;
  /** absolute path of the asset directory (used for staticFile on host/product images). */
  assetDir: string;
}

/**
 * Ken Burns transform: scale + translate interpolation over the scene duration.
 * Used by host/product scenes to give still images a sense of motion.
 */
const useKenBurnsTransform = (
  motion: "zoom_in" | "zoom_out" | "pan_right" | "pan_left" | undefined,
  totalFrames: number,
) => {
  const frame = useCurrentFrame();
  const t = frame / Math.max(1, totalFrames);
  switch (motion ?? "zoom_in") {
    case "zoom_in":
      return {
        scale: interpolate(t, [0, 1], [1.0, 1.12]),
        x: 0,
        y: 0,
      };
    case "zoom_out":
      return {
        scale: interpolate(t, [0, 1], [1.15, 1.0]),
        x: 0,
        y: 0,
      };
    case "pan_right":
      return {
        scale: 1.1,
        x: interpolate(t, [0, 1], [-30, 30]),
        y: 0,
      };
    case "pan_left":
      return {
        scale: 1.1,
        x: interpolate(t, [0, 1], [30, -30]),
        y: 0,
      };
  }
};

/**
 * Brand watermark — fixed "测评社 bilibili" mark in the top-left corner.
 * Mirrors the watermark visible in every frame of the reference video.
 */
const Watermark: React.FC = () => (
  <div
    style={{
      position: "absolute",
      top: 24,
      left: 32,
      zIndex: 100,
      color: "rgba(255, 255, 255, 0.92)",
      fontFamily: CJK,
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

/**
 * Bottom caption bar — Chinese + optional English keyword overlay.
 * White background, dark text, styled to match the reference video's
 * subtitle strip ("拉到身边随便一拍" style — solid white block, dark text).
 */
const CaptionBar: React.FC<{
  zh: string;
  en?: string;
}> = ({ zh, en }) => (
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
      fontFamily: CJK,
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
          fontFamily: MONO,
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

/**
 * Big overlay annotation — used by product scenes to show the headline stat
 * (e.g. "≥100 次") as a bold overlay on top of the product image.
 */
const AnnotationOverlay: React.FC<{
  text: string;
  color?: string;
}> = ({ text, color = "#F59E0B" }) => (
  <div
    style={{
      position: "absolute",
      top: "18%",
      left: "50%",
      transform: "translate(-50%, -50%)",
      color,
      fontFamily: DISPLAY,
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

const SceneRenderer: React.FC<SceneRendererProps> = ({
  scene,
  accentColor,
  assetDir,
}) => {
  const frame = useCurrentFrame();
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
          statFontSize={260}
          subtitleFontSize={48}
          color={scene.accentColor || accentColor}
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
          leftColor={scene.leftColor || "#2563EB"}
          rightColor={scene.rightColor || accentColor}
        />
      );
    case "text":
      return (
        <TextCard
          text={scene.text}
          fontSize={88}
          backgroundColor="#FAFAFA"
          color={scene.accentColor || accentColor}
        />
      );
    case "end":
      return <EndTag text={scene.text} palette="warm_ivory_on_black" />;
    case "host": {
      // Talking head: image + Ken Burns + bottom caption.
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
      // Product shot: image + Ken Burns + optional annotation + optional caption.
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
    default:
      // Exhaustiveness handled at compile time via LuggageScene union;
      // runtime fallback (in case a future scene type is added but not
      // yet wired) returns null rather than throwing.
      return null;
  }
};

// -- Main composition -------------------------------------------------------

const FPS = 30;

export const LuggageComposition = ({ assetDir, scenes }: LuggageConceptsProps) => {
  // BGM bed (mixed low under narration). Looped by Remotion via `loop` if the
  // bed is shorter than total; here we rely on MusicGen generating >= 70s.
  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0a", fontFamily: DISPLAY }}>
      {/* Brand watermark — fixed overlay, mirrors the "测评社 bilibili" mark
       * visible in every frame of the reference video. */}
      <Watermark />
      {/* BGM — ducked well under the narration. `loop` because MusicGen on
       * CPU is slow; we generate 30s and loop it across the full duration. */}
      <Audio
        src={staticFile(`${assetDir}/music/bgm.wav`)}
        volume={(_f: number) => 0.22}
        loop
      />
      {scenes.map((scene: LuggageScene, i: number) => {
        const startFrame = scenes
          .slice(0, i)
          .reduce((acc: number, s: LuggageScene) => acc + Math.round(s.duration * FPS), 0);
        const sceneFrames = Math.round(scene.duration * FPS);
        // Pick the accent color per-concept (set below by the wrapper).
        const accent =
          scene.type === "stat"
            ? scene.accentColor || "#F59E0B"
            : scene.type === "comparison"
            ? "#EF4444"
            : scene.type === "text"
            ? "#1A1A2E"
            : "#F59E0B";
        return (
          <Sequence
            key={i}
            from={startFrame}
            durationInFrames={sceneFrames}
            name={scene.type}
          >
            {/* TTS narration per scene */}
            <Audio src={staticFile(`${assetDir}/${scene.audio}`)} volume={1} />
            <SceneRenderer scene={scene} accentColor={accent} assetDir={assetDir} />
          </Sequence>
        );
      })}
    </AbsoluteFill>
  );
};

// -- Bilingual caption row (English keyword overlay) -----------------------

/**
 * `LuggageCaptions` is a SEPARATE composition the renderer can composite on
 * top via ffmpeg — not a layer inside `LuggageComposition` because that would
 * require two distinct `Audio` tracks and Remotion's multi-track mixing is
 * still rough. Instead the captions get burned in a separate small pass.
 *
 * Each scene carries English keyword overlays in `captions[].text` (the
 * "overlay" field). The burn-in is straightforward; see
 * `tools/analysis/concept_render_orchestrator.py`.
 */

// -- Default props for the three concept entries ----------------------------

export const luggageAProps: LuggageConceptsProps = {
  conceptId: "a",
  assetDir: "_staged/luggage-a",
  scenes: [
    {
      type: "hero",
      duration: 8,
      audio: "voice/a_hook.wav",
      title: "5 个硬指标",
      subtitle: "买行李箱前必看",
    },
    {
      type: "stat",
      duration: 8,
      audio: "voice/a_metric1.wav",
      stat: "≥100 次",
      subtitle: "硬指标 #1 · 放摔测试",
      accentColor: "#F59E0B",
    },
    {
      type: "stat",
      duration: 8,
      audio: "voice/a_metric2.wav",
      stat: "YKK",
      subtitle: "硬指标 #2 · 拉链品牌",
      accentColor: "#2563EB",
    },
    {
      type: "stat",
      duration: 8,
      audio: "voice/a_metric3.wav",
      stat: "≥30 kg",
      subtitle: "硬指标 #3 · 轮轴承重",
      accentColor: "#10B981",
    },
    {
      type: "stat",
      duration: 8,
      audio: "voice/a_metric4.wav",
      stat: "TSA",
      subtitle: "硬指标 #4 · 海关锁",
      accentColor: "#8B5CF6",
    },
    {
      type: "stat",
      duration: 8,
      audio: "voice/a_metric5.wav",
      stat: "≥2 年",
      subtitle: "硬指标 #5 · 售后保修",
      accentColor: "#EC4899",
    },
    {
      type: "end",
      duration: 12,
      audio: "voice/a_cta.wav",
      text: "评论区告诉我你的判断清单 ↓",
    },
  ],
};

export const luggageBProps: LuggageConceptsProps = {
  conceptId: "b",
  assetDir: "_staged/luggage-b",
  scenes: [
    {
      type: "hero",
      duration: 8,
      audio: "voice/b_hook.wav",
      title: "26 款里最坑的 3 个",
      subtitle: "避雷指南 · 测评社",
    },
    {
      type: "comparison",
      duration: 14,
      audio: "voice/b_scam1.wav",
      title: "智商税 #1 · 拉链",
      leftLabel: "YKK 标志",
      leftValue: "✓",
      rightLabel: "无品牌",
      rightValue: "✗",
      leftColor: "#2563EB",
      rightColor: "#EF4444",
    },
    {
      type: "comparison",
      duration: 14,
      audio: "voice/b_scam2.wav",
      title: "智商税 #2 · 轮轴",
      leftLabel: "真实承重 50kg",
      leftValue: "✓",
      rightLabel: "虚标 / 20kg 就垮",
      rightValue: "✗",
      leftColor: "#10B981",
      rightColor: "#EF4444",
    },
    {
      type: "comparison",
      duration: 14,
      audio: "voice/b_scam3.wav",
      title: "智商税 #3 · 售后",
      leftLabel: "门店可查",
      leftValue: "✓",
      rightLabel: "查不到门店",
      rightValue: "✗",
      leftColor: "#22D3EE",
      rightColor: "#EF4444",
    },
    {
      type: "end",
      duration: 10,
      audio: "voice/b_cta.wav",
      text: "想知道哪三款？下期讲 ↓",
    },
  ],
};

export const luggageCProps: LuggageConceptsProps = {
  conceptId: "c",
  assetDir: "_staged/luggage-c",
  scenes: [
    {
      type: "hero",
      duration: 8,
      audio: "voice/c_hook.wav",
      title: "工程师不会告诉你",
      subtitle: "3 件事 · 行李箱内幕",
    },
    {
      type: "text",
      duration: 14,
      audio: "voice/c_secret1.wav",
      text: "拉链齿密度\n≥ 6 个 / 厘米",
      accentColor: "#1A1A2E",
    },
    {
      type: "text",
      duration: 14,
      audio: "voice/c_secret2.wav",
      text: "轮轴承重 ≠ 耐久\n看跌落测试",
      accentColor: "#1A1A2E",
    },
    {
      type: "text",
      duration: 14,
      audio: "voice/c_secret3.wav",
      text: "PC 抗摔 · ABS 便宜\n看预算选",
      accentColor: "#1A1A2E",
    },
    {
      type: "end",
      duration: 10,
      audio: "voice/c_cta.wav",
      text: "最重要的 — 是品牌售后",
    },
  ],
};

// ---------------------------------------------------------------------------
// Concept A — FULL VERSION (~4.5 min, image-driven with Ken Burns + watermark)
// ---------------------------------------------------------------------------
// Per-metric structure: host-intro (talking head) → big-stat (text card) →
// host-deep-dive (talking head) → product-visual (minimax photo) → takeaway.
// Total ~270s. Each metric = ~50s. Plus hook + recap + end tag.

export const luggageAFullProps: LuggageConceptsProps = {
  conceptId: "a",
  assetDir: "_staged/luggage-a-full",
  scenes: [
    // HOOK — 12s
    {
      type: "hero",
      duration: 12,
      audio: "voice/00_hook.wav",
      title: "5 个硬指标",
      subtitle: "测评社 · 26 款行李箱实测",
    },

    // METRIC 1 — DROP TEST (~50s)
    { type: "host", duration: 8, audio: "voice/10_m1_intro.wav",
      image: "images/host.png", caption: "硬指标一：放摔测试", captionEn: "DROP TEST",
      motion: "zoom_in" },
    { type: "stat", duration: 8, audio: "voice/11_m1_stat.wav",
      stat: "≥100 次", subtitle: "跌落测试底线",
      accentColor: "#F59E0B" },
    { type: "host", duration: 11, audio: "voice/12_m1_detail.wav",
      image: "images/host.png", caption: "一米、五米、十米，三个高度", captionEn: "1m / 5m / 10m",
      motion: "pan_right" },
    { type: "product", duration: 12, audio: "voice/13_m1_visual.wav",
      image: "images/metric1_drop.png", annotation: "≥100 次",
      annotationColor: "#F59E0B", caption: "看这个测试现场", captionEn: "drop test in progress",
      motion: "zoom_in" },
    { type: "text", duration: 5, audio: "voice/14_m1_take.wav",
      text: "100 次是底线", accentColor: "#F59E0B" },

    // METRIC 2 — ZIPPER (~50s)
    { type: "host", duration: 8, audio: "voice/20_m2_intro.wav",
      image: "images/host.png", caption: "硬指标二：拉链品牌", captionEn: "ZIPPER BRAND",
      motion: "zoom_in" },
    { type: "stat", duration: 8, audio: "voice/21_m2_stat.wav",
      stat: "YKK", subtitle: "日本品牌，全球高端拉链",
      accentColor: "#2563EB" },
    { type: "host", duration: 10, audio: "voice/22_m2_detail.wav",
      image: "images/host.png", caption: "占全球高端拉链八成份额", captionEn: "80% market share",
      motion: "pan_right" },
    { type: "product", duration: 12, audio: "voice/23_m2_visual.wav",
      image: "images/metric2_zipper.png", annotation: "YKK",
      annotationColor: "#2563EB", caption: "看拉链头这个标志", captionEn: "look for this mark",
      motion: "zoom_in" },
    { type: "text", duration: 5, audio: "voice/24_m2_take.wav",
      text: "没标的别买", accentColor: "#2563EB" },

    // METRIC 3 — WHEEL LOAD (~50s)
    { type: "host", duration: 8, audio: "voice/30_m3_intro.wav",
      image: "images/host.png", caption: "硬指标三：轮轴承重", captionEn: "WHEEL LOAD",
      motion: "zoom_in" },
    { type: "stat", duration: 8, audio: "voice/31_m3_stat.wav",
      stat: "≥30 kg", subtitle: "装满衣物日用品的重量底线",
      accentColor: "#10B981" },
    { type: "host", duration: 10, audio: "voice/32_m3_detail.wav",
      image: "images/host.png", caption: "装满就推不动的箱子不能用", captionEn: "below 30kg = unusable",
      motion: "pan_right" },
    { type: "product", duration: 12, audio: "voice/33_m3_visual.wav",
      image: "images/metric3_wheel.png", annotation: "30 kg",
      annotationColor: "#10B981", caption: "看这个承重测试", captionEn: "load test in progress",
      motion: "zoom_in" },
    { type: "text", duration: 5, audio: "voice/34_m3_take.wav",
      text: "30 公斤是起步", accentColor: "#10B981" },

    // METRIC 4 — TSA LOCK (~50s)
    { type: "host", duration: 8, audio: "voice/40_m4_intro.wav",
      image: "images/host.png", caption: "硬指标四：海关锁", captionEn: "TSA LOCK",
      motion: "zoom_in" },
    { type: "stat", duration: 8, audio: "voice/41_m4_stat.wav",
      stat: "TSA", subtitle: "美国海关唯一认可的密码锁",
      accentColor: "#8B5CF6" },
    { type: "host", duration: 10, audio: "voice/42_m4_detail.wav",
      image: "images/host.png", caption: "没有 TSA 锁，海关会撬", captionEn: "no TSA = forced open",
      motion: "pan_right" },
    { type: "product", duration: 12, audio: "voice/43_m4_visual.wav",
      image: "images/metric4_tsa.png", annotation: "TSA",
      annotationColor: "#8B5CF6", caption: "看这个红色菱形标志", captionEn: "red diamond logo",
      motion: "zoom_in" },
    { type: "text", duration: 5, audio: "voice/44_m4_take.wav",
      text: "认准这个标志", accentColor: "#8B5CF6" },

    // METRIC 5 — WARRANTY (~50s)
    { type: "host", duration: 8, audio: "voice/50_m5_intro.wav",
      image: "images/host.png", caption: "硬指标五：售后保修", captionEn: "WARRANTY",
      motion: "zoom_in" },
    { type: "stat", duration: 8, audio: "voice/51_m5_stat.wav",
      stat: "≥2 年", subtitle: "行李箱不是耐用品，零件总会坏",
      accentColor: "#EC4899" },
    { type: "host", duration: 10, audio: "voice/52_m5_detail.wav",
      image: "images/host.png", caption: "保修低于两年 = 自己修", captionEn: "<2 yr = DIY repair",
      motion: "pan_right" },
    { type: "product", duration: 12, audio: "voice/53_m5_visual.wav",
      image: "images/metric5_warranty.png", annotation: "2 年",
      annotationColor: "#EC4899", caption: "看这个保修卡", captionEn: "warranty card",
      motion: "zoom_in" },
    { type: "text", duration: 5, audio: "voice/54_m5_take.wav",
      text: "两年是底线", accentColor: "#EC4899" },

    // RECAP + CTA — 28s
    { type: "host", duration: 12, audio: "voice/60_recap.wav",
      image: "images/host.png", caption: "五个硬指标，三个关键词",
      captionEn: "5 metrics · 3 keywords",
      motion: "zoom_in" },
    { type: "product", duration: 8, audio: "voice/60_recap.wav",
      image: "images/outdoor.png", caption: "100 次，YKK，30 公斤",
      captionEn: "100 drops · YKK · 30kg load",
      motion: "zoom_out" },

    // END TAG — 8s
    {
      type: "end",
      duration: 8,
      audio: "voice/70_end.wav",
      text: "测评社 · 下一期拆哪款",
    },
  ],
};

// Duration helpers (used by the Root composition metadata)
export const totalFramesFor = (scenes: LuggageScene[]): number =>
  scenes.reduce((acc, s) => acc + Math.round(s.duration * FPS), 0);