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
 * AeroShell Carry-On brand promo — Landscape 16:9.
 * Same 8 story beats as AeroShellPromo, re-laid for 1920x1080 detail
 * pages / in-feed, with the royalty-free BGM bed mixed in via <Audio>.
 * ------------------------------------------------------------------ */
export const FPS = 30;
export const W = 1920;
export const H = 1080;

const SCENES = {
  hero: { from: 0, dur: 90 }, // 0–3s
  product: { from: 90, dur: 120 }, // 3–7s
  detail: { from: 210, dur: 120 }, // 7–11s
  lifestyle: { from: 330, dur: 120 }, // 11–15s
  interior: { from: 450, dur: 120 }, // 15–19s
  material: { from: 570, dur: 120 }, // 19–23s
  packaging: { from: 690, dur: 120 }, // 23–27s
  closing: { from: 810, dur: 150 }, // 27–32s
};
export const DURATION = 960;

const C = {
  bg: "#F3EFEA",
  ink: "#1A1A1A",
  sub: "#6B6258",
  accent: "#C9A24B",
  panel: "#FFFFFF",
  line: "#E0D9CF",
  dark: "#15120E",
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
            style={{ backgroundColor: v ? "#15120E" : "#fff" }}
          />
        ))
      )}
    </div>
  );
};

const Hero: React.FC = () => {
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
          src={staticFile("aeroshell/01_hero.png")}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      </AbsoluteFill>
      <AbsoluteFill style={{ backgroundColor: "rgba(21,18,14,0.40)" }} />
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
            AEROSHELL
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
            Silence in Motion.
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
            PREMIUM SMART CARRY-ON
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

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

const Product: React.FC = () => {
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
          src={staticFile("aeroshell/02_product.png")}
          style={{ width: "52%", transform: `scale(${imgScale})`, objectFit: "contain" }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: headingOp, padding: 70 }}>
        <div style={{ fontFamily: FONT, fontSize: 60, fontWeight: 800, color: C.ink }}>
          Full View
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
          Pearl warm-white. Champagne gold.
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
          <SpecRow k="MATERIAL" v="PC Hardshell" delay={20} f={f} />
          <SpecRow k="CAPACITY" v="38L · 24 inch" delay={30} f={f} />
          <SpecRow k="WEIGHT" v="3.4 kg" delay={40} f={f} />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

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
        <div style={{ height: 4, width: 110, backgroundColor: C.accent, marginBottom: 12 }} />
        <div style={{ fontFamily: FONT, fontSize: 32, fontWeight: 800, color: C.ink }}>
          {title}
        </div>
        <div style={{ fontFamily: FONT, fontSize: 24, fontWeight: 400, color: C.sub, marginTop: 6 }}>
          {body}
        </div>
      </div>
    </AbsoluteFill>
  );
};

const Detail: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 14 } });
  const headingOp = fade(f, 0, 16, 104, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <Img
          src={staticFile("aeroshell/03_detail.png")}
          style={{ width: "58%", transform: `scale(${imgScale})`, objectFit: "contain" }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: headingOp, padding: 70 }}>
        <div style={{ fontFamily: FONT, fontSize: 60, fontWeight: 800, color: C.ink }}>
          Every Detail
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
          Engineered down to the wheel.
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
        title="Champagne-Gold Corners"
        body="Aircraft alloy, impact proof."
        x={80}
        y={620}
        f={f}
        delay={32}
        align="right"
      />
    </AbsoluteFill>
  );
};

const Lifestyle: React.FC = () => {
  const f = useCurrentFrame();
  const zoom = interpolate(f, [0, 120], [1.12, 1.0], {
    easing: Easing.out(Easing.cubic),
  });
  const op = fade(f, 0, 14, 104, 120);
  const bigOp = fade(f, 18, 34, 100, 120);
  const subOp = fade(f, 30, 46, 100, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.dark }}>
      <AbsoluteFill
        style={{ transform: `scale(${zoom})`, justifyContent: "center", alignItems: "center" }}
      >
        <Img
          src={staticFile("aeroshell/04_lifestyle.png")}
          style={{ width: "100%", height: "100%", objectFit: "cover" }}
        />
      </AbsoluteFill>
      <AbsoluteFill style={{ backgroundColor: "rgba(21,18,14,0.32)" }} />
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center", padding: 60 }}>
        <div
          style={{
            textAlign: "center",
            transform: `translateY(${interpolate(f, [18, 34], [40, 0])}px)`,
            opacity: bigOp,
          }}
        >
          <div style={{ fontFamily: FONT, fontSize: 84, fontWeight: 800, color: "#fff", letterSpacing: 3 }}>
            Made for the Journey
          </div>
          <div
            style={{
              opacity: subOp,
              fontFamily: FONT,
              fontSize: 30,
              fontWeight: 400,
              color: C.accent,
              marginTop: 12,
              letterSpacing: 3,
            }}
          >
            Airports. Cities. Anywhere.
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Interior: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 14 } });
  const headingOp = fade(f, 0, 16, 104, 120);
  const capOp = fade(f, 18, 34, 104, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <Img
          src={staticFile("aeroshell/05_interior.png")}
          style={{ width: "70%", transform: `scale(${imgScale})`, objectFit: "contain" }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: headingOp, padding: 70 }}>
        <div style={{ fontFamily: FONT, fontSize: 60, fontWeight: 800, color: C.ink }}>
          38L of Calm
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
          Compression panels. Mesh dividers.
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: capOp, justifyContent: "flex-end", alignItems: "center", paddingBottom: 70 }}>
        <div
          style={{
            fontFamily: FONT,
            fontSize: 28,
            fontWeight: 700,
            color: C.ink,
            letterSpacing: 3,
            backgroundColor: C.accent,
            padding: "14px 38px",
            borderRadius: 999,
          }}
        >
          PACKS A WEEK IN DAYS
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Material: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 13, mass: 0.8 } });
  const bigOp = fade(f, 8, 26, 104, 120);
  const badgeOp = fade(f, 20, 38, 100, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "center" }}>
        <Img
          src={staticFile("aeroshell/06_material.png")}
          style={{ width: "66%", transform: `scale(${imgScale})`, objectFit: "contain" }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: bigOp, justifyContent: "flex-start", alignItems: "center", paddingTop: 80 }}>
        <div style={{ textAlign: "center", transform: `translateY(${interpolate(f, [8, 26], [40, 0])}px)` }}>
          <div style={{ fontFamily: FONT, fontSize: 80, fontWeight: 800, color: C.ink, letterSpacing: 3 }}>
            Built to Endure
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
            Engineered hardshell, alloy corners.
          </div>
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: badgeOp, justifyContent: "flex-end", alignItems: "center", paddingBottom: 70 }}>
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
          SCRATCH-RESISTANT SHELL
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const PackItem: React.FC<{ label: string; delay: number; f: number }> = ({
  label,
  delay,
  f,
}) => {
  const op = fade(f, delay, delay + 12, 104, 120);
  const x = interpolate(f, [delay, delay + 12], [50, 0], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
  return (
    <div
      style={{
        opacity: op,
        transform: `translateX(${x}px)`,
        display: "flex",
        alignItems: "center",
        marginBottom: 22,
      }}
    >
      <div
        style={{
          width: 14,
          height: 14,
          borderRadius: 999,
          backgroundColor: C.accent,
          marginRight: 18,
        }}
      />
      <div style={{ fontFamily: FONT, fontSize: 34, fontWeight: 600, color: C.ink }}>
        {label}
      </div>
    </div>
  );
};

const Packaging: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 16, 104, 120);
  const imgScale = spring({ frame: f, fps: FPS, config: { damping: 14 } });
  const headingOp = fade(f, 0, 16, 104, 120);
  const listOp = fade(f, 12, 32, 104, 120);

  return (
    <AbsoluteFill style={{ opacity: op, backgroundColor: C.bg }}>
      <AbsoluteFill style={{ justifyContent: "center", alignItems: "flex-start", paddingLeft: 120 }}>
        <Img
          src={staticFile("aeroshell/07_packaging.png")}
          style={{ width: "52%", transform: `scale(${imgScale})`, objectFit: "contain" }}
        />
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: headingOp, padding: 70 }}>
        <div style={{ fontFamily: FONT, fontSize: 60, fontWeight: 800, color: C.ink }}>
          What's in the Box
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
          A delivery you can trust.
        </div>
      </AbsoluteFill>

      <AbsoluteFill style={{ opacity: listOp }}>
        <div
          style={{
            position: "absolute",
            right: 120,
            top: 250,
            width: 460,
            background: C.panel,
            borderRadius: 24,
            padding: 40,
            boxShadow: "0 30px 60px rgba(0,0,0,0.14)",
          }}
        >
          <PackItem label="Protective Cover" delay={20} f={f} />
          <PackItem label="Spare Wheel Set" delay={30} f={f} />
          <PackItem label="Luggage Tag" delay={40} f={f} />
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

const Closing: React.FC = () => {
  const f = useCurrentFrame();
  const op = fade(f, 0, 14, 138, 150);
  const logo = spring({ frame: f, fps: FPS, config: { damping: 13, mass: 0.8 } });
  const priceOp = fade(f, 14, 30, 138, 150);
  const ctaOp = fade(f, 26, 42, 138, 150);
  const qrOp = fade(f, 40, 56, 138, 150);

  return (
    <AbsoluteFill
      style={{
        opacity: op,
        background: `linear-gradient(160deg, ${C.ink} 0%, #2a241c 55%, ${C.dark} 100%)`,
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
        <div style={{ fontFamily: FONT, fontSize: 104, fontWeight: 800, color: "#fff", letterSpacing: 14 }}>
          AEROSHELL
        </div>
        <div style={{ fontFamily: FONT, fontSize: 28, color: C.accent, letterSpacing: 6, marginTop: 8 }}>
          SILENCE IN MOTION.
        </div>
      </div>

      <div style={{ opacity: priceOp, textAlign: "center", marginTop: 50 }}>
        <div style={{ fontFamily: FONT, fontSize: 116, fontWeight: 800, color: "#fff" }}>
          $229
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

      <AbsoluteFill style={{ opacity: qrOp, justifyContent: "flex-end", alignItems: "flex-end", padding: 70 }}>
        <div style={{ textAlign: "center" }}>
          <QrPlaceholder size={130} />
          <div style={{ fontFamily: FONT, fontSize: 22, color: "#C7CCD2", marginTop: 12, letterSpacing: 2 }}>
            Scan to shop
          </div>
        </div>
      </AbsoluteFill>
    </AbsoluteFill>
  );
};

export const AeroShellPromoLandscape: React.FC = () => {
  return (
    <AbsoluteFill style={{ backgroundColor: C.bg }}>
      {/* Royalty-free BGM, generated by scripts/gen_bgm.py. */}
      <Audio src={staticFile("bgm.wav")} volume={1.0} />
      <Sequence from={SCENES.hero.from} durationInFrames={SCENES.hero.dur}>
        <Hero />
      </Sequence>
      <Sequence from={SCENES.product.from} durationInFrames={SCENES.product.dur}>
        <Product />
      </Sequence>
      <Sequence from={SCENES.detail.from} durationInFrames={SCENES.detail.dur}>
        <Detail />
      </Sequence>
      <Sequence from={SCENES.lifestyle.from} durationInFrames={SCENES.lifestyle.dur}>
        <Lifestyle />
      </Sequence>
      <Sequence from={SCENES.interior.from} durationInFrames={SCENES.interior.dur}>
        <Interior />
      </Sequence>
      <Sequence from={SCENES.material.from} durationInFrames={SCENES.material.dur}>
        <Material />
      </Sequence>
      <Sequence from={SCENES.packaging.from} durationInFrames={SCENES.packaging.dur}>
        <Packaging />
      </Sequence>
      <Sequence from={SCENES.closing.from} durationInFrames={SCENES.closing.dur}>
        <Closing />
      </Sequence>
    </AbsoluteFill>
  );
};
