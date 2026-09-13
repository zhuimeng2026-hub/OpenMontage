/**
 * Render FontVerify composition programmatically via Remotion's Node API.
 * Uses the same bundle path as Studio so fonts.ts changes are picked up.
 */
const path = require("path");
const { bundle } = require("@remotion/bundler");
const { getCompositions, renderStill } = require("@remotion/renderer");

const entry = path.join(__dirname, "..", "src/index.tsx");

bundle({ entryPoint: entry })
  .then((bundled) => {
    return getCompositions(bundled).then((comps) => {
      const comp = comps.find((c) => c.id === "FontVerify");
      if (!comp) {
        console.error("FontVerify not found. Available:", comps.map((c) => c.id));
        process.exit(1);
      }
      console.log(`Rendering FontVerify @ ${comp.width}x${comp.height}…`);
      return renderStill({
        composition: comp,
        bundle: bundled,
        output: path.join(__dirname, "..", "out/FontVerify-API.png"),
        inputProps: {},
        chromiumOptions: { args: ["--no-sandbox", "--disable-dev-shm-usage"] },
      });
    });
  })
  .then(() => {
    console.log("Done! → out/FontVerify-API.png");
    process.exit(0);
  })
  .catch((err) => {
    console.error("Render failed:", err.message);
    process.exit(1);
  });
