import { readdirSync } from "node:fs";
import { spawnSync } from "node:child_process";

const directory = "obox_builder/contrib/admin/static/admin/js";
for (const name of readdirSync(directory).filter((name) => name.endsWith(".js"))) {
  const result = spawnSync(process.execPath, ["--check", `${directory}/${name}`], {stdio: "inherit"});
  if (result.status) process.exit(result.status);
}
