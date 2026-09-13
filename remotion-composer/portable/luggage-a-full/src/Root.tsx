/**
 * Root.tsx — registers the LuggageAFull composition with Remotion.
 * Reads + validates manifest.json via loadLuggageProps() at module load.
 * Validation errors here crash the bundler early with a useful message,
 * which is what we want for an LLM-produced manifest.
 */

import * as React from "react";
import { Composition } from "remotion";
import {
  CANVAS_FPS,
  CANVAS_HEIGHT,
  CANVAS_WIDTH,
  LuggageComposition,
  loadLuggageProps,
  totalFramesFor,
} from "./LuggageComposition";
// `resolveJsonModule` is enabled in tsconfig.json so this import is typed
// as the raw JSON value; we validate + narrow it inside loadLuggageProps.
import manifest from "./manifest.json";

const props = loadLuggageProps(manifest);

export const Root: React.FC = () => (
  <>
    <Composition
      id="LuggageAFull"
      component={LuggageComposition}
      durationInFrames={totalFramesFor(props.scenes)}
      fps={CANVAS_FPS}
      width={CANVAS_WIDTH}
      height={CANVAS_HEIGHT}
      defaultProps={props}
    />
  </>
);
