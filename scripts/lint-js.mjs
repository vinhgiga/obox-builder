// Syntax checks for the JavaScript tooling retained by the core package.
import { spawnSync } from "node:child_process";

const result = spawnSync(process.execPath, ["--check", "commitlint.config.mjs"], { stdio: "inherit" });
process.exitCode = result.status ?? 1;
