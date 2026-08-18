import * as React from "react";
import * as Babel from "@babel/standalone";
import * as Remotion from "remotion";

/**
 * CustomComposition — runtime-compiles a user-authored Remotion TSX string and
 * renders it. The user code convention (see the BFF `DEFAULT_COMP` template):
 *
 *   import {AbsoluteFill, useCurrentFrame, interpolate, Sequence, Easing} from "remotion";
 *   export const MyComposition = ({images, durationPerImage = 3}) => { ... };
 *
 * The component receives `images: string[]` (paths relative to Remotion's
 * public dir — wrap them with `staticFile(src)` inside the user code) and
 * `durationPerImage` (seconds). It must return a React element using Remotion
 * APIs. `export default` is also accepted.
 */

export interface CustomCompositionProps {
  code: string;
  images: string[];
  durationPerImage: number;
  width: number;
  height: number;
  fps: number;
}

type AnyComp = React.FC<Record<string, unknown>>;

function compileUserComponent(
  code: string
): { Component: AnyComp | null; error: string | null } {
  if (!code || !code.trim()) {
    return { Component: null, error: "empty code" };
  }
  try {
    const out = Babel.transform(code, {
      presets: [
        ["react", { runtime: "automatic" }],
        "typescript",
      ],
      plugins: ["transform-modules-commonjs"],
      filename: "custom.tsx",
    }).code as string;

    // `require` that resolves "remotion" to this bundle's real module so the
    // user's `import {...} from "remotion"` binds to the live Remotion runtime.
    const userRequire = (mod: string): unknown => {
      if (mod === "remotion") return Remotion;
      // eslint-disable-next-line @typescript-eslint/no-var-requires
      return require(mod);
    };

    const factory = new Function(
      "module",
      "exports",
      "require",
      out +
        "\n;return (module.exports.default && (typeof module.exports.default === 'function' ? module.exports.default : module.exports.default)) || module.exports.MyComposition || Object.values(module.exports).find(function (v) { return typeof v === 'function'; });"
    );

    const mod = { exports: {} as Record<string, unknown> };
    const compiled = factory(mod, mod.exports, userRequire) as unknown;

    if (typeof compiled !== "function") {
      return {
        Component: null,
        error:
          "用户代码未导出可渲染组件：请使用 `export const MyComposition = (...) => {...}` 或 `export default`。",
      };
    }
    return { Component: compiled as AnyComp, error: null };
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return { Component: null, error: `编译失败：${msg}` };
  }
}

const ErrorScreen: React.FC<{ message: string }> = ({ message }) => {
  return (
    <Remotion.AbsoluteFill
      style={{
        backgroundColor: "#7F1D1D",
        color: "#FECACA",
        fontFamily: "monospace",
        fontSize: 28,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: 48,
        textAlign: "center",
      }}
    >
      {message}
    </Remotion.AbsoluteFill>
  );
};

export const CustomComposition: React.FC<CustomCompositionProps> = ({
  code,
  images,
  durationPerImage,
}) => {
  const { Component, error } = React.useMemo(
    () => compileUserComponent(code),
    [code]
  );

  if (error || !Component) {
    return <ErrorScreen message={error || "未知错误"} />;
  }

  try {
    return (
      <Component
        images={images}
        durationPerImage={durationPerImage}
      />
    );
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e);
    return <ErrorScreen message={`运行期错误：${msg}`} />;
  }
};
