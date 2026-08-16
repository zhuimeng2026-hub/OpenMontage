import React from "react";
import {
  AbsoluteFill,
  Audio,
  Img,
  Sequence,
  Easing,
  interpolate,
  spring,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";

export interface EcommerceProductDemoProps {
  brandName: string;
  productName: string;
  promise: string;
  price: string;
  compareAtPrice: string;
  offer: string;
  cta: string;
  featureOne: { title: string; body: string };
  featureTwo: { title: string; body: string };
  featureThree: { title: string; body: string };
  specs: Array<{ label: string; value: string }>;
  assets: {
    hero: string;
    product: string;
    detail: string;
    lifestyle: string;
    logo?: string;
    music?: string;
  };
  accentColor?: string;
  /** Total output duration requested by the render job, in seconds. */
  targetDurationSeconds?: number;
  [key: string]: unknown;
}

const DEFAULT_PROPS: EcommerceProductDemoProps = {
  brandName: "VOYAGE",
  productName: "AeroShell Carry-On",
  promise: "Travel lighter. Move further.",
  price: "$189",
  compareAtPrice: "$239",
  offer: "Free worldwide shipping · 30-day returns",
  cta: "SHOP NOW",
  featureOne: { title: "Silent 360° wheels", body: "Glides smoothly through terminals and streets." },
  featureTwo: { title: "Impact-ready shell", body: "Aerospace-grade protection for every trip." },
  featureThree: { title: "Smart interior", body: "Compression panels keep every item in place." },
  specs: [
    { label: "CAPACITY", value: "38 L" },
    { label: "WEIGHT", value: "3.2 kg" },
    { label: "WARRANTY", value: "Lifetime" },
  ],
  assets: {
    hero: "luggage_scene_airport.png",
    product: "luggage_threequarter.png",
    detail: "luggage_side.png",
    lifestyle: "luggage_front.png",
    music: "bgm.wav",
  },
  accentColor: "#D1A84B",
};

const FONT = "Arial, 'Helvetica Neue', Helvetica, sans-serif";
const BG = "#F3F1EC";
const INK = "#17191D";
const MUTED = "#666A70";

function appear(frame: number, start: number, duration = 16) {
  return interpolate(frame, [start, start + duration], [0, 1], {
    extrapolateLeft: "clamp",
    extrapolateRight: "clamp",
    easing: Easing.out(Easing.cubic),
  });
}

function Scene({ children }: { children: React.ReactNode }) {
  return <AbsoluteFill style={{ backgroundColor: BG }}>{children}</AbsoluteFill>;
}

function Label({ children, accent }: { children: React.ReactNode; accent: string }) {
  return (
    <div style={{ fontFamily: FONT, fontSize: 18, fontWeight: 700, letterSpacing: 4, color: accent }}>
      {children}
    </div>
  );
}

function ProductImage({ src, style }: { src: string; style?: React.CSSProperties }) {
  return <Img src={staticFile(src)} style={{ objectFit: "contain", ...style }} />;
}

function Hook({ p }: { p: EcommerceProductDemoProps }) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const scale = interpolate(frame, [0, 90], [1.12, 1.02], { easing: Easing.out(Easing.cubic) });
  const reveal = spring({ frame, fps, config: { damping: 16, stiffness: 90 } });
  return (
    <Scene>
      <ProductImage src={p.assets.hero} style={{ position: "absolute", width: "100%", height: "100%", transform: `scale(${scale})`, objectFit: "cover" }} />
      <AbsoluteFill style={{ backgroundColor: "rgba(10,12,14,.46)" }} />
      <AbsoluteFill style={{ justifyContent: "center", padding: 110 }}>
        <div style={{ opacity: reveal, transform: `translateY(${interpolate(reveal, [0, 1], [35, 0])}px)` }}>
          <Label accent={p.accentColor || "#D1A84B"}>{p.brandName}</Label>
          <div style={{ color: "#fff", fontFamily: FONT, fontSize: 92, fontWeight: 800, maxWidth: 1000, marginTop: 22 }}>{p.promise}</div>
          <div style={{ color: "#E6E6E6", fontFamily: FONT, fontSize: 30, marginTop: 24 }}>{p.productName}</div>
        </div>
      </AbsoluteFill>
    </Scene>
  );
}

function Overview({ p }: { p: EcommerceProductDemoProps }) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const product = spring({ frame, fps, config: { damping: 15, stiffness: 85 } });
  const text = appear(frame, 16);
  return (
    <Scene>
      <AbsoluteFill style={{ flexDirection: "row", alignItems: "center", padding: "70px 110px" }}>
        <div style={{ width: "42%", opacity: text }}>
          <Label accent={p.accentColor || "#D1A84B"}>MEET THE PRODUCT</Label>
          <div style={{ fontFamily: FONT, fontSize: 70, lineHeight: 1.05, fontWeight: 800, color: INK, marginTop: 20 }}>{p.productName}</div>
          <div style={{ fontFamily: FONT, fontSize: 28, lineHeight: 1.45, color: MUTED, marginTop: 24 }}>{p.promise}</div>
          <div style={{ marginTop: 40, fontFamily: FONT, fontSize: 22, color: INK }}>{p.offer}</div>
        </div>
        <div style={{ width: "58%", height: "100%", display: "flex", justifyContent: "center", alignItems: "center", transform: `scale(${interpolate(product, [0, 1], [.8, 1])})` }}>
          <ProductImage src={p.assets.product} style={{ width: "100%", height: "100%" }} />
        </div>
      </AbsoluteFill>
    </Scene>
  );
}

function Feature({ p, index, image }: { p: EcommerceProductDemoProps; index: 1 | 2 | 3; image: string }) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const feature = p[index === 1 ? "featureOne" : index === 2 ? "featureTwo" : "featureThree"];
  const product = spring({ frame, fps, config: { damping: 14, stiffness: 100 } });
  return (
    <Scene>
      <AbsoluteFill style={{ flexDirection: index % 2 ? "row" : "row-reverse", alignItems: "center", padding: "70px 110px", gap: 70 }}>
        <div style={{ flex: 1, opacity: appear(frame, 12), transform: `translateX(${interpolate(appear(frame, 12), [0, 1], [index % 2 ? -35 : 35, 0])}px)` }}>
          <Label accent={p.accentColor || "#D1A84B"}>WHY IT WORKS · 0{index}</Label>
          <div style={{ fontFamily: FONT, fontSize: 64, lineHeight: 1.06, fontWeight: 800, color: INK, marginTop: 22 }}>{feature.title}</div>
          <div style={{ fontFamily: FONT, fontSize: 30, lineHeight: 1.45, color: MUTED, marginTop: 24 }}>{feature.body}</div>
        </div>
        <div style={{ flex: 1.2, height: "100%", display: "flex", justifyContent: "center", alignItems: "center", transform: `scale(${interpolate(product, [0, 1], [.86, 1])})` }}>
          <ProductImage src={image} style={{ width: "100%", height: "100%" }} />
        </div>
      </AbsoluteFill>
    </Scene>
  );
}

function Proof({ p }: { p: EcommerceProductDemoProps }) {
  const frame = useCurrentFrame();
  return (
    <Scene>
      <AbsoluteFill style={{ flexDirection: "row", alignItems: "center", padding: "80px 110px", gap: 80 }}>
        <div style={{ flex: 1, height: "100%", display: "flex", justifyContent: "center", alignItems: "center", opacity: appear(frame, 8) }}><ProductImage src={p.assets.lifestyle} style={{ width: "100%", height: "100%" }} /></div>
        <div style={{ flex: 1, opacity: appear(frame, 18) }}>
          <Label accent={p.accentColor || "#D1A84B"}>THE DETAILS</Label>
          <div style={{ fontFamily: FONT, fontSize: 58, fontWeight: 800, color: INK, marginTop: 20 }}>Designed for real life.</div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 14, marginTop: 48 }}>
            {p.specs.map((spec) => <div key={spec.label} style={{ borderTop: `2px solid ${p.accentColor || "#D1A84B"}`, paddingTop: 14 }}><div style={{ fontFamily: FONT, fontSize: 16, fontWeight: 700, letterSpacing: 2, color: MUTED }}>{spec.label}</div><div style={{ fontFamily: FONT, fontSize: 30, fontWeight: 700, color: INK, marginTop: 9 }}>{spec.value}</div></div>)}
          </div>
        </div>
      </AbsoluteFill>
    </Scene>
  );
}

function Offer({ p }: { p: EcommerceProductDemoProps }) {
  const frame = useCurrentFrame();
  const { fps } = useVideoConfig();
  const reveal = spring({ frame, fps, config: { damping: 15, stiffness: 90 } });
  return (
    <AbsoluteFill style={{ background: `linear-gradient(145deg, ${INK}, #30343A)`, color: "#fff", justifyContent: "center", alignItems: "center", textAlign: "center" }}>
      <div style={{ opacity: reveal, transform: `translateY(${interpolate(reveal, [0, 1], [35, 0])}px)` }}>
        <Label accent={p.accentColor || "#D1A84B"}>{p.brandName}</Label>
        <div style={{ fontFamily: FONT, fontSize: 62, fontWeight: 800, marginTop: 20 }}>{p.productName}</div>
        <div style={{ marginTop: 28, fontFamily: FONT }}><span style={{ fontSize: 62, fontWeight: 800 }}>{p.price}</span><span style={{ fontSize: 28, color: "#A8ADB3", textDecoration: "line-through", marginLeft: 18 }}>{p.compareAtPrice}</span></div>
        <div style={{ fontFamily: FONT, fontSize: 22, color: "#D8D9DB", marginTop: 12 }}>{p.offer}</div>
        <div style={{ display: "inline-block", marginTop: 36, padding: "22px 58px", borderRadius: 999, backgroundColor: p.accentColor || "#D1A84B", color: INK, fontFamily: FONT, fontSize: 26, fontWeight: 800, letterSpacing: 3 }}>{p.cta}</div>
      </div>
    </AbsoluteFill>
  );
}

export const EcommerceProductDemo: React.FC<Partial<EcommerceProductDemoProps>> = (input) => {
  const p = { ...DEFAULT_PROPS, ...input, assets: { ...DEFAULT_PROPS.assets, ...(input.assets || {}) } } as EcommerceProductDemoProps;
  const scale = p.targetDurationSeconds && p.targetDurationSeconds > 0
    ? (p.targetDurationSeconds * 30) / ECOMMERCE_PRODUCT_DEMO_DURATION
    : 1;
  const frame = (base: number) => Math.round(base * scale);
  return (
    <AbsoluteFill>
      {p.assets.music && <Audio src={staticFile(p.assets.music)} volume={0.22} />}
      <Sequence from={frame(0)} durationInFrames={frame(90)}><Hook p={p} /></Sequence>
      <Sequence from={frame(90)} durationInFrames={frame(120)}><Overview p={p} /></Sequence>
      <Sequence from={frame(210)} durationInFrames={frame(105)}><Feature p={p} index={1} image={p.assets.product} /></Sequence>
      <Sequence from={frame(315)} durationInFrames={frame(105)}><Feature p={p} index={2} image={p.assets.detail} /></Sequence>
      <Sequence from={frame(420)} durationInFrames={frame(105)}><Feature p={p} index={3} image={p.assets.lifestyle} /></Sequence>
      <Sequence from={frame(525)} durationInFrames={frame(120)}><Proof p={p} /></Sequence>
      <Sequence from={frame(645)} durationInFrames={frame(105)}><Offer p={p} /></Sequence>
    </AbsoluteFill>
  );
};

export const ecommerceProductDemoDefaultProps = DEFAULT_PROPS;
export const ECOMMERCE_PRODUCT_DEMO_DURATION = 750;
