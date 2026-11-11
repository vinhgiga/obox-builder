import { readFileSync, writeFileSync } from "node:fs";

const source = new URL("../node_modules/@tabler/core/dist/css/tabler.min.css", import.meta.url);
const target = new URL("../obox_builder/builder/static/tabler.min.css", import.meta.url);
const css = readFileSync(source, "utf8");
writeFileSync(target, `${css.trimEnd()}\n`);
