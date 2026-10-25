import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

const body = "prevent invalid messages from entering the commit history";
const sha = "0123456789abcdef0123456789abcdef01234567";
const revertBody = `This reverts commit ${sha}.\n\nrestore compatibility with existing integrations`;
const root = new URL("../../", import.meta.url);

function lint(message, args = []) {
  const result = spawnSync("pnpm", ["exec", "commitlint", ...args], {
    cwd: root, input: message, encoding: "utf8", timeout: 10000,
  });
  assert.ifError(result.error);
  return result;
}

const valid = [
  ...["build", "ci", "feat", "fix", "perf", "refactor", "test"].map(
    (type) => `${type}: validate commit messages\n\n${body}`
  ),
  "docs: explain commit messages",
  `docs: explain commit messages\n\n${body}`,
  `fix(admin): preserve HTTP headers\n\n${body}`,
  `fix(db/migrations): preserve SQL defaults\n\n${body}`,
  `feat(@example/package): add export\n\n${body}`,
  `fix: handle empty input\n\n${"a".repeat(20)}`,
  `fix: handle empty input\n\n${body}\n\nFixes #123`,
  `fix: update the setting\n\n${body}\n\nBREAKING CHANGE: rename the setting\n\nreplace OLD_SETTING with NEW_SETTING\n\nCloses #123`,
  `feat: replace the setting\n\n${body}\n\nDEPRECATED: retire the old setting\n\nuse NEW_SETTING instead`,
  `revert: feat(admin): add export\n\n${revertBody}`,
];
for (const message of valid) {
  test(`accept ${message.replaceAll("\n", " / ")}`, () => {
    const result = lint(message);
    assert.equal(result.status, 0, result.stdout + result.stderr);
  });
}

const invalid = [
  ["missing type", `update commit checks\n\n${body}`, "header-format"],
  ["unsupported type", `chore: update commit checks\n\n${body}`, "type-enum"],
  ["uppercase type", `Fix: update commit checks\n\n${body}`, "header-format"],
  ["empty subject", `fix: \n\n${body}`, "header-format"],
  ["empty scope", `fix(): update commit checks\n\n${body}`, "header-format"],
  ["missing separator", `fix:update commit checks\n\n${body}`, "header-format"],
  ["extra separator space", `fix:  update commit checks\n\n${body}`, "header-format"],
  ["uppercase summary", `fix: Update commit checks\n\n${body}`, "subject-start-case"],
  ["sentence period", `fix: update commit checks.\n\n${body}`, "subject-full-stop"],
  ["missing body", "fix: update commit checks", "body-required"],
  ["short body", `fix: update commit checks\n\n${"a".repeat(19)}`, "body-required"],
  ["short docs body", "docs: explain commit checks\n\nshort", "body-required"],
  ["padded short body", "fix: update commit checks\n\n                 short", "body-required"],
  ["footer without body", "fix: update commit checks\n\nFixes #123", "body-required"],
  ["missing body separator", `fix: update commit checks\n${body}`, "body-leading-blank"],
  ["missing footer separator", `fix: update commit checks\n\n${body}\nFixes #123`, "footer-leading-blank"],
  ["missing breaking details", `fix: update the setting\n\n${body}\n\nBREAKING CHANGE: rename the setting`, "notice-format"],
  ["missing deprecation details", `feat: replace the setting\n\n${body}\n\nDEPRECATED: retire the setting`, "notice-format"],
  ["breaking header shorthand", `feat!: replace the setting\n\n${body}`, "header-format"],
  ["automatic merge", "Merge branch 'main'\n\nmerge the latest upstream changes", "header-format"],
  ["fixup bypass", `fixup! fix: update commit checks\n\n${body}`, "header-format"],
  ["automatic revert bypass", `Revert \"add export\"\n\n${revertBody}`, "header-format"],
  ["missing revert target", `revert: undo changes\n\n${revertBody}`, "revert-format"],
  ["scoped revert", `revert(admin): feat(admin): add export\n\n${revertBody}`, "revert-format"],
  ["invalid revert type", `revert: chore: add export\n\n${revertBody}`, "revert-format"],
  ["uppercase revert target", `revert: feat: Add export\n\n${revertBody}`, "revert-format"],
  ["missing revert SHA", `revert: feat: add export\n\n${body}`, "revert-format"],
  ["missing revert reason", `revert: feat: add export\n\nThis reverts commit ${sha}.`, "revert-format"],
];
for (const [name, message, rule] of invalid) {
  test(`reject ${name}`, () => {
    const result = lint(message);
    assert.equal(result.status, 1, result.stdout + result.stderr);
    assert.ok(result.stdout.includes(`[${rule}]`), result.stdout + result.stderr);
  });
}

test("--edit validates the file passed by the commit-msg hook", () => {
  const directory = mkdtempSync(join(tmpdir(), "obox-commitlint-"));
  const path = join(directory, "COMMIT_EDITMSG");
  try {
    writeFileSync(path, `fix: validate the commit file\n\n${body}\n`);
    assert.equal(lint(undefined, ["--edit", path]).status, 0);
    writeFileSync(path, "fix: reject a missing body\n");
    assert.equal(lint(undefined, ["--edit", path]).status, 1);
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});
