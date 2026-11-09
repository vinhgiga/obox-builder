import { copyFileSync, readFileSync, realpathSync, writeFileSync } from "node:fs";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const source = new URL("../node_modules/chart.js/", import.meta.url);
const target = new URL("../obox_builder/contrib/analytics/static/", import.meta.url);
copyFileSync(new URL("dist/chart.umd.js", source), new URL("chart.umd.js", target));
const color = pathToFileURL(
  createRequire(realpathSync(new URL("package.json", source))).resolve("@kurkle/color"),
);
writeFileSync(
  new URL("CHART_LICENSE.txt", target),
  [
    "Chart.js",
    readFileSync(new URL("LICENSE.md", source), "utf8"),
    "@kurkle/color",
    readFileSync(new URL("../LICENSE.md", color), "utf8"),
  ].join("\n"),
);
