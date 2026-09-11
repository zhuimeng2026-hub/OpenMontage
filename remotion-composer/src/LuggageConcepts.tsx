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
  Sequence,
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
  captions?: { text: string; start: number; end: number }[];
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
export type LuggageScene = HeroScene | StatScene | ComparisonScene | TextScene | EndScene;

export interface LuggageConceptsProps {
  conceptId: "a" | "b" | "c";
  /** asset dir relative to public/, e.g. "_staged/luggage-a/" */
  assetDir: string;
  scenes: LuggageScene[];
}

// -- Scene renderer ---------------------------------------------------------

interface SceneRendererProps {
  scene: LuggageScene;
  accentColor: string;
}

const SceneRenderer = ({ scene, accentColor }: SceneRendererProps) => {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();

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
      {/* BGM — ducked well under the narration. `loop` because MusicGen on
       * CPU is slow; we generate 30s and loop it across the full 60s. */}
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
            <SceneRenderer scene={scene} accentColor={accent} />
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

// Duration helpers (used by the Root composition metadata)
export const totalFramesFor = (scenes: LuggageScene[]): number =>
  scenes.reduce((acc, s) => acc + Math.round(s.duration * FPS), 0);