import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  Sequence,
  interpolate,
  useCurrentFrame,
  Easing,
  spring,
  staticFile,
} from "remotion";

/* ------------------------------------------------------------------ *
 * Landscape (16:9) brand promo for YouTube detail pages / in-feed.
 * Same 6 scenes as the portrait version, re-laid-out for 1920x1080,
 * with a royalty-free BGM bed mixed in via <Audio>.
 * ------------------------------------------------------------------ */
export const FPS = 30;
export const W = 1920;
export const H = 1080;

const SCENES = {
  intro: { from: 0, dur: 90 }, // 0–3s   airport hero + brand title
  front: { from: 90, dur: 120 }, // 3–7s   front view + spec card
  side: { from: 210, dur: 120 }, // 7–11s  side view + annotations
  angle: { from: 330, dur: 120 }, // 11–15s 45° hero + 360 claim
  montage: { from: 450, dur: 90 }, // 15–18s four-image grid
  end: { from: 540, dur: 90 }, // 18–21s endboard + CTA
};
export const DURATION = 630;

const C = {
  bg: "#ECEFF1",
  ink: "#16181D",
  sub: "#5A6169",
  accent: "#C9A24B",
  panel: "#FFFFFF",
  line: "#D7DBDF",
  dark: "#101216",
};
const FONT = "Arial, 'Helvetica Neue', Helvetica, sans-serif";

function fade(
  frame: number,
  inA: number,
  inB: number,
  outA: number,
  outB: number
): number {
  return interpolate(frame, [inA, inB, outA, outB], [0, 1, 1, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
  });
}

/* ---------- faux QR placeholder (visual only) ---------- */
const QR_PATTERN = [
  [1, 1, 1, 1, 1, 0, 1, 0, 1, 1, 1, 0, 1, 1, 1, 1, 1],
  [1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 1, 0, 0, 0, 1],
  [1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1, 0, 1],
  [1, 0, 1, 0, 1, 0, 0, 0, 1, 1, 1, 0, 1, 0, 1, 0, 1],
  [1, 1, 1, 1, 1, 0, 1, 0, 0, 0, 1, 0, 1, 1, 1, 1, 1],
  [0, 0, 0, 0, 0, 0, 0, 1, 0, 1, 0, 0, 0, 0, 0, 0, 0],
  [1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 1, 0, 1, 0, 1, 0],
  [0, 1, 0, 1, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 1],
  [1, 1, 0, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1],
  [0, 0, 1, 1, 0, 1, 0, 1, 0, 1, 1, 0, 0, 1, 0, 0, 0],
  [1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1],
  [0, 1, 1, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 1, 0, 1, 0],
  [1, 1, 1, 1, 1, 0, 1, 1, 0, 0, 1, 1, 0, 1, 1, 1, 1],
  [1, 0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0, 0, 0, 1, 1],
  [1, 0, 1, 0, 1, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1, 0, 1],
  [1, 1, 1, 1, 1, 0, 1, 1, 0, 1, 0, 1, 1, 1, 1, 1, 1],
  [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
];
const QrPlaceholder: React.FC<{ size: number }> = ({ size }) => {
  const cell = size / QR_PATTERN.length;
  return (
    <div
      style={{
        width: size,
        height: size,
        backgroundColor: "#fff",
        padding: cell,
        borderRadius: 10,
        display: "grid",
        gridTemplateColumns: `repeat(${QR_PATTERN.length}, 1fr)`,
        gridTemplateRows: `repeat(${QR_PATTERN.length}, 1fr)`,
      }}
    >
      {QR_PATTERN.flatMap((row, r) =>
        row.map((v, c) => (
          <div
            key={`${r}-${c}`}
            style={{ backgroundColor: v ? "#101216" : "#fff" }}
          />
        ))
      )}
    </div>
  );
};

/* ------------------------------------------------------------------ *
 * Scene 1 — Intro
 * ------------------------------------------------------------------ */
const Intro: React.FC = () => {
  const f = useCurrentFrame();
  const zoom = interpolate(f, [0, 90], [1.16, 1.02], {
    easing: Easing.out(Easing.cubic),
  });
  const baseOp = fade(f, 0, 14, 78, 90);
  const title = spring({ frame: f, fps: FPS, config: { damping: 13, mass: 0.9 } });
  const taglineOp = fade(f, 34, 54, 80, 90);
  const subOp = fade(f, 44, 60, 80, 90);

  return (
    <AbsoluteFill style={{ opacity: baseOp, backgroundColor: C.bg }}>
      <AbsoluteFill
        style={{
          transform: `scale(${zoom})`,
          justifyContent: "center",
          alignItems: "center",
        }}
      >
        <Img
          src={staticFile("luggage_scene_airport.png")}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      </AbsoluteFill>
      <AbsoluteFill style={{ backgroundColor: "rgba(16,18,22,0.42)" }} />
      <AbsoluteFill
        style={{ justifyContent: "center", alignItems: "center", padding: 60 }}
      >
        <div
          style={{
            transform: `translateY(${interpolate(title, [0, 1], [50, 0])}px)`,
            opacity: title,
            textAlign: "center",
          }}
        >
          <div
            style={{
              fontFamily: FONT,
              fontWeight: 800,
              fontSize: 132,
              color: "#fff",
              letterSpacing: 18,
            }}
          >
            VOYAGE
          </div>
          <div
            style={{
              opacity: taglineOp,
              marginTop: 20,
              fontFamily: FONT,
              fontWeight: 400,
              fontSize: 40,
              color: C.accent,
              letterSpacing: 6,
            }}
          >
            Travel Light. Travel Far.
          </div>
          <div
            style={{
              opacity: subOp,
              marginTop: 14,
              fontFamily: FONT,
              fontWeight: 400,
              fontSize: 26,
              color: "#EDEDED",
              letterSpacing: 5,
            }}
          >
            PREMIUM HARDSHELL LUGGAGE
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ *
 * Scene 2 — Front view + spec card
 * ------------------------------------------------------------------ */
const SpecRow: React.FC<{ k: string; v: string; delay: number; f: number }> = ({
  k,
  v,
  delay,
  f,
}) => {
  const op = fade(f, delay, delay + 12, 104, 120);
  const x = interpolate(f, [delay, delay + 12], [60, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  return (
    <div
      style={{
        opacity: op,
        transform: `translateX(${x}px)`,
        marginBottom: 22,
        borderBottom: `1px solid ${C.line}`,
        paddingBottom: 16,
      }}
    >
      <div
        style={{
          fontFamily: FONT,
          fontSize: 22,
          fontWeight: 700,
          color: C.accent,
          letterSpacing: 3,
        }}
      >
        {k}
      </div>
      <div
        style={{
          fontFamily: FONT,
          fontSize: 34,
          fontWeight: 600,
          color: C.ink,
          marginTop: 4,
        }}
      >
        {v}
      </div>
    </div>
  );
};

const Front: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 14 } });
  const headingOp = fade(f, 0, 16, 104, 120);
  const cardOp = fade(f, 12, 32, 104, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill
        style={{ justifyContent: "center", alignItems: "flex-start", paddingLeft: 120 }}
      >
        <Img
          src={staticFile("luggage_front.png")}
          style={{
            width: "56%",
            transform: `scale(${imgScale})`,
            objectFit: "contain",
          }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: headingOp, padding: 70 }}>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 60,
            fontWeight: 800,
            color: C.ink,
          }}
        >
          Front View
        </div>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 28,
            fontWeight: 400,
            color: C.sub,
            marginTop: 6,
          }}
        >
          Clean lines. Built to last.
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: cardOp }}>
        <div
          style={{
            position: "absolute",
            right: 120,
            top: 250,
            width: 480,
            background: C.panel,
            borderRadius: 24,
            padding: 40,
            boxShadow: "0 30px 60px rgba(0,0,0,0.14)",
          }}
        >
          <div
            style={{
              fontFamily: FONT,
              fontSize: 26,
              fontWeight: 800,
              color: C.ink,
              letterSpacing: 2,
              marginBottom: 22,
            }}
          >
            SPECIFICATIONS
          </div>
          <SpecRow k="MATERIAL" v="Bayer PC Shell" delay={20} f={f} />
          <SpecRow k="CAPACITY" v="38L · 24 inch" delay={30} f={f} />
          <SpecRow k="WEIGHT" v="3.2 kg" delay={40} f={f} />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ *
 * Scene 3 — Side view + annotations
 * ------------------------------------------------------------------ */
const Annotation: React.FC<{
  title: string;
  body: string;
  x: number;
  y: number;
  f: number;
  delay: number;
  align: "left" | "right";
}> = ({ title, body, x, y, f, delay, align }) => {
  const op = fade(f, delay, delay + 12, 104, 120);
  const ty = interpolate(f, [delay, delay + 12], [24, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  return (
    <AbsoluteFill style={{ opacity: op }}>
      <div
        style={{
          position: "absolute",
          left: align === "left" ? x : undefined,
          right: align === "right" ? x : undefined,
          top: y,
          width: 380,
          transform: `translateY(${ty}px)`,
        }}
      >
        <div
          style={{ height: 4, width: 110, backgroundColor: C.accent, marginBottom: 12 }}
        />
        <div
          style={{
            fontFamily: FONT,
            fontSize: 32,
            fontWeight: 800,
            color: C.ink,
          }}
        >
          {title}
        </div>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 24,
            fontWeight: 400,
            color: C.sub,
            marginTop: 6,
          }}
        >
          {body}
        </div>
      </div>
    </AbsoluteFill>
  );
};

const Side: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 14 } });
  const headingOp = fade(f, 0, 16, 104, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <Img
          src={staticFile("luggage_side.png")}
          style={{
            width: "62%",
            transform: `scale(${imgScale})`,
            objectFit: "contain",
          }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: headingOp, padding: 70 }}>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 60,
            fontWeight: 800,
            color: C.ink,
          }}
        >
          Side Profile
        </div>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 28,
            fontWeight: 400,
            color: C.sub,
            marginTop: 6,
          }}
        >
          Details that matter.
        </div>
      </AbsoluteFill>

      <Annotation
        title="Silent Spinner Wheels"
        body="360° glide, whisper quiet."
        x={80}
        y={330}
        f={f}
        delay={18}
        align="left"
      />
      <Annotation
        title="Aero-grade Corner Guards"
        body="Aluminum alloy, impact proof."
        x={80}
        y={620}
        f={f}
        delay={32}
        align="right"
      />
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ *
 * Scene 4 — 45° hero + 360 claim
 * ------------------------------------------------------------------ */
const Angle: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 13, mass: 0.8 } });
  const badgeOp = fade(f, 20, 38, 100, 120);
  const bigOp = fade(f, 8, 26, 104, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <Img
          src={staticFile("luggage_threequarter.png")}
          style={{
            width: "64%",
            transform: `scale(${imgScale})`,
            objectFit: "contain",
          }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: bigOp, justifyContent: "flex-start", alignItems: "center", paddingTop: 90 }}>
        <div style={{ textAlign: "center", transform: `translateY(${interpolate(f, [8, 26], [40, 0])}px)` }}>
          <div
            style={{
              fontFamily: FONT,
              fontSize: 84,
              fontWeight: 800,
              color: C.ink,
              letterSpacing: 4,
            }}
          >
            360° DESIGN
          </div>
          <div
            style={{
              fontFamily: FONT,
              fontSize: 30,
              fontWeight: 400,
              color: C.sub,
              marginTop: 12,
              letterSpacing: 2,
            }}
          >
            Engineered from every angle.
          </div>
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: badgeOp, justifyContent: "flex-end", alignItems: "center", paddingBottom: 80 }}>
        <div
          style={{
            background: C.ink,
            color: "#fff",
            fontFamily: FONT,
            fontSize: 26,
            fontWeight: 700,
            letterSpacing: 4,
            padding: "16px 38px",
            borderRadius: 999,
          }}
        >
          TSA-APPROVED LOCK
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ *
 * Scene 5 — Montage (2x2 grid)
 * ------------------------------------------------------------------ */
const GridCell: React.FC<{ src: string; delay: number }> = ({ src, delay }) => {
  const f = useCurrentFrame();
  const s = spring({ frame: f - delay, fps: FPS, config: { damping: 13 } });
  return (
    <AbsoluteFill style={{ transform: `scale(${s})` }}>
      <Img
        src={staticFile(src)}
        style={{ width: "100%", height: "100%", objectFit: "cover" }}
      />
    </AbsoluteFill>
  );
};

const Montage: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 14, 78, 90);
  const centerOp = fade(f, 24, 40, 78, 90);

  const cells = [
    { src: "luggage_front.png", x: 0, y: 0, d: 0 },
    { src: "luggage_side.png", x: 960, y: 0, d: 6 },
    { src: "luggage_threequarter.png", x: 0, y: 540, d: 12 },
    { src: "luggage_scene_airport.png", x: 960, y: 540, d: 18 },
  ];

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.dark }}>
      {cells.map((c, i) => (
        <div
          key={i}
          style={{
            position: "absolute",
            left: c.x,
            top: c.y,
            width: 960,
            height: 540,
            padding: 8,
            boxSizing: "border-box",
          }}
        >
          <GridCell src={c.src} delay={c.d} />
        </div>
      ))}
      <AbsoluteFill
        style={{
          opacity: centerOp,
          justifyContent: "center",
          alignItems: "center",
          backgroundColor: "rgba(16,18,22,0.35)",
        }}
      >
        <div style={{ textAlign: "center", transform: `translateY(${interpolate(f, [24, 40], [30, 0])}px)` }}>
          <div
            style={{
              fontFamily: FONT,
              fontSize: 68,
              fontWeight: 800,
              color: "#fff",
              letterSpacing: 4,
            }}
          >
            BUILT TO LAST
          </div>
          <div
            style={{
              fontFamily: FONT,
              fontSize: 28,
              fontWeight: 400,
              color: C.accent,
              marginTop: 12,
              letterSpacing: 3,
            }}
          >
            FOUR VIEWS. ONE PROMISE.
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ *
 * Scene 6 — Endboard + CTA
 * ------------------------------------------------------------------ */
const End: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 14, 78, 90);
  const logo = spring({ frame: f, fps: FPS, config: { damping: 13, mass: 0.8 } });
  const priceOp = fade(f, 14, 30, 78, 90);
  const ctaOp = fade(f, 26, 42, 78, 90);

  return (
    <AbsoluteFill
      style={{
        opacity: op,
        background: `linear-gradient(160deg, ${C.ink} 0%, #20242c 55%, ${C.dark} 100%)`,
        justifyContent: "center",
        alignItems: "center",
        padding: 70,
      }}
    >
      <div
        style={{
          transform: `translateY(${interpolate(logo, [0, 1], [40, 0])}px) scale(${0.9 + logo * 0.1})`,
          opacity: logo,
          textAlign: "center",
        }}
      >
        <div
          style={{
            fontFamily: FONT,
            fontSize: 104,
            fontWeight: 800,
            color: "#fff",
            letterSpacing: 14,
          }}
        >
          VOYAGE
        </div>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 28,
            color: C.accent,
            letterSpacing: 6,
            marginTop: 8,
          }}
        >
          TRAVEL LIGHT. TRAVEL FAR.
        </div>
      </div>

      <div style={{ opacity: priceOp, textAlign: "center", marginTop: 50 }}>
        <div style={{ fontFamily: FONT, fontSize: 116, fontWeight: 800, color: "#fff" }}>
          $189
        </div>
        <div style={{ fontFamily: FONT, fontSize: 28, color: "#C7CCD2", marginTop: 4 }}>
          Free Worldwide Shipping
        </div>
      </div>

      <div
        style={{
          opacity: ctaOp,
          marginTop: 40,
          background: C.accent,
          color: C.dark,
          fontFamily: FONT,
          fontSize: 40,
          fontWeight: 800,
          letterSpacing: 4,
          padding: "24px 72px",
          borderRadius: 999,
          transform: `translateY(${interpolate(f, [26, 42], [30, 0])}px)`,
        }}
      >
        SHOP NOW
      </div>

      <AbsoluteFill style={{ justifyContent: "flex-end", alignItems: "flex-end", padding: 70 }}>
        <div style={{ textAlign: "center", opacity: priceOp }}>
          <QrPlaceholder size={130} />
          <div
            style={{
              fontFamily: FONT,
              fontSize: 22,
              color: "#C7CCD2",
              marginTop: 12,
              letterSpacing: 2,
            }}
          >
            Scan to shop
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

/* ------------------------------------------------------------------ *
 * Root composition — 6 scenes on a timeline + BGM bed.
 * ------------------------------------------------------------------ */
export const LuggagePromoLandscape: React.FC = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: C.bg }}>
      {/* Royalty-free BGM, generated by scripts/gen_bgm.py, mixed in automatically. */}
      <Audio src={staticFile("bgm.wav")} volume={1.0} />
      <Sequence from={SCENES.intro.from} durationInFrames={SCENES.intro.dur}>
        <Intro />
      </Sequence>
      <Sequence from={SCENES.front.from} durationInFrames={SCENES.front.dur}>
        <Front />
      </Sequence>
      <Sequence from={SCENES.side.from} durationInFrames={SCENES.side.dur}>
        <Side />
      </Sequence>
      <Sequence from={SCENES.angle.from} durationInFrames={SCENES.angle.dur}>
        <Angle />
      </Sequence>
      <Sequence from={SCENES.montage.from} durationInFrames={SCENES.montage.dur}>
        <Montage />
      </Sequence>
      <Sequence from={SCENES.end.from} durationInFrames={SCENES.end.dur}>
        <End />
      </Sequence>
    </AbsoluteFill>
  );
};
