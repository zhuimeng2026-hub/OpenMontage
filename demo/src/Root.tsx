import { Composition } from "remotion";
import { LuggagePromo, FPS, DURATION, W, H } from "./LuggagePromo";
import {
  LuggagePromoLandscape,
  FPS as LFPS,
  DURATION as LDURATION,
  W as LW,
  H as LH,
} from "./LuggagePromoLandscape";

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
    </>
  );
};
