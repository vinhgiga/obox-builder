import { build } from "esbuild";
import { copyFileSync, mkdirSync } from "node:fs";

const directory = "obox_builder/contrib/admin/static/admin/vendor/tanstack";
mkdirSync(directory, {recursive: true});
await build({
  stdin: {
    contents: 'export { Virtualizer, observeElementRect, observeElementOffset, elementScroll } from "@tanstack/virtual-core";',
    resolveDir: process.cwd(),
  },
  bundle: true,
  format: "iife",
  globalName: "AdminVirtual",
  minify: true,
  outfile: `${directory}/virtual-core.min.js`,
  banner: {js: "/* @tanstack/virtual-core 3.19.8 | MIT License | https://tanstack.com/virtual */"},
});
copyFileSync("node_modules/@tanstack/virtual-core/LICENSE", `${directory}/LICENSE`);
