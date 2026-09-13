import { Composition } from "remotion";
import { LuggagePromo, FPS, DURATION, W, H } from "./LuggagePromo";
import {
  LuggagePromoLandscape,
  FPS as LFPS,
  DURATION as LDURATION,
  W as LW,
  H as LH,
} from "./LuggagePromoLandscape";
import {
  EcommerceProductDemo,
  ecommerceProductDemoDefaultProps,
  ECOMMERCE_PRODUCT_DEMO_DURATION,
} from "./EcommerceProductDemo";
import {
  AeroShellPromo,
  FPS as AFPS,
  DURATION as ADURATION,
  W as AW,
  H as AH,
} from "./AeroShellPromo";
import {
  AeroShellPromoLandscape,
  FPS as ALFPS,
  DURATION as ALDURATION,
  W as ALW,
  H as ALH,
} from "./AeroShellPromoLandscape";

export const RemotionRoot: React.FC = () => {
  return (
    <>
      {/* Portrait 9:16 — for TikTok / Shorts / Reels */}
      <Composition
        id="LuggagePromo"
        component={LuggagePromo}
        durationInFrames={DURATION}
        fps={FPS}
        width={W}
        height={H}
        defaultProps={{}}
      />
      {/* Landscape 16:9 — for YouTube detail pages / in-feed, with BGM */}
      <Composition
        id="LuggagePromoLandscape"
        component={LuggagePromoLandscape}
        durationInFrames={LDURATION}
        fps={LFPS}
        width={LW}
        height={LH}
        defaultProps={{}}
      />
      <Composition
        id="EcommerceProductDemo"
        component={EcommerceProductDemo}
        durationInFrames={ECOMMERCE_PRODUCT_DEMO_DURATION}
        fps={30}
        width={1920}
        height={1080}
        defaultProps={ecommerceProductDemoDefaultProps}
      />
      {/* AeroShell Carry-On — portrait 9:16 promo (8 generated stills) */}
      <Composition
        id="AeroShellPromo"
        component={AeroShellPromo}
        durationInFrames={ADURATION}
        fps={AFPS}
        width={AW}
        height={AH}
        defaultProps={{}}
      />
      {/* AeroShell Carry-On — landscape 16:9 promo with BGM */}
      <Composition
        id="AeroShellPromoLandscape"
        component={AeroShellPromoLandscape}
        durationInFrames={ALDURATION}
        fps={ALFPS}
        width={ALW}
        height={ALH}
        defaultProps={{}}
      />
    </>
  );
};
