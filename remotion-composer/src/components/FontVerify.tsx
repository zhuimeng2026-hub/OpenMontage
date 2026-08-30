import React from "react";
import { AbsoluteFill } from "remotion";
import { SANS, DISPLAY, MONO, SERIF } from "../fonts";

const Row: React.FC<{
  font: string;
  label: string;
  zh: string;
  en: string;
  bg: string;
  accent: string;
}> = ({ font, label, zh, en, bg, accent }) => (
  <div
    style={{
      fontFamily: font,
      background: bg,
      padding: "28px 48px",
      marginBottom: 14,
      borderRadius: 8,
    }}
  >
    <div style={{ color: "#666", fontSize: 13, marginBottom: 6, letterSpacing: "0.08em" }}>
      {label}
    </div>
    <div style={{ color: "#fff", fontSize: 38, lineHeight: 1.2 }}>
      {zh} <span style={{ color: accent }}>{en}</span>
    </div>
    <div style={{ color: "#888", fontSize: 20, marginTop: 6 }}>
      {en} {zh}
    </div>
  </div>
);

export const FontVerify: React.FC = () => (
  <AbsoluteFill style={{ background: "#080808", padding: "40px 60px" }}>
    <div
      style={{
        fontFamily: DISPLAY,
        color: "#fbbf24",
        fontSize: 34,
        marginBottom: 36,
        letterSpacing: "0.04em",
      }}
    >
      🔤 字体验证 / Font Verification
    </div>

    <Row
      font={SANS}
      label="SANS — Inter"
      zh="简体中文简体中文"
      en="The quick brown fox jumps"
      bg="#111827"
      accent="#60a5fa"
    />
    <Row
      font={DISPLAY}
      label="DISPLAY — Space Grotesk"
      zh="中文字体测试"
      en="Space Grotesk renders correctly"
      bg="#1c1c1c"
      accent="#a78bfa"
    />
    <Row
      font={MONO}
      label="MONO — JetBrains Mono"
      zh="代码 变量 函数"
      en="const fn = () => 42;"
      bg="#1a1a1a"
      accent="#34d399"
    />
    <Row
      font={SERIF}
      label="SERIF — Playfair Display"
      zh="衬线体中文"
      en="The quick brown fox"
      bg="#161616"
      accent="#f472b6"
    />
  </AbsoluteFill>
);
