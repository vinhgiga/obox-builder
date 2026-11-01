const types = ["build", "ci", "docs", "feat", "fix", "perf", "refactor", "test", "revert"];
const scopes = ["builder", "auth", "jobs", "cli", "i18n", "deps", "ci"];
const headerPattern = /^([a-z]+)(?:\(([^()\s]+)\))?: (\S(?:.*\S)?)$/u;
const revertLine = /^This reverts commit [a-f0-9]{7,64}\.?$/im;

export default {
  defaultIgnores: false,
  ignores: [
    (message) => /^Merge (?:pull request|branch)\b/u.test(message),
    (message) => /^build\(deps\): bump /iu.test(message),
  ],
  parserPreset: {
    parserOpts: {
      headerPattern,
      headerCorrespondence: ["type", "scope", "subject"],
      noteKeywords: ["BREAKING CHANGE", "DEPRECATED"],
    },
  },
  plugins: [
    {
      rules: {
        "header-format": ({ header }) => [
          headerPattern.test(header ?? ""),
          "header must use type(scope): summary or type: summary, with one space after the colon",
        ],
        "subject-start-case": ({ subject }) => [
          !/^\p{Lu}/u.test(subject ?? ""),
          "summary must not start with an uppercase letter",
        ],
        "body-required": ({ type, body }) => [
          (type === "docs" && !body?.trim()) || (body?.trim().length ?? 0) >= 20,
          "body must contain at least 20 characters (only docs may omit it)",
        ],
        "notice-format": ({ notes }) => [
          notes.every(({ text }) => /^\S[^\n]*\n\s*\n[\s\S]*\S$/u.test(text)),
          "BREAKING CHANGE and DEPRECATED need a summary, blank line, and migration/replacement details",
        ],
        "revert-format": ({ type, scope, subject, body }) => {
          if (type !== "revert") return [true];
          const target = subject?.match(headerPattern);
          return [
            !scope &&
              Boolean(target && types.includes(target[1])) &&
              !/^\p{Lu}/u.test(target?.[3] ?? "") &&
              revertLine.test(body ?? "") &&
              Boolean(body?.replace(revertLine, "").trim()),
            "revert must use revert: <original header>, with This reverts commit <SHA> and a reason in the body",
          ];
        },
      },
    },
  ],
  rules: {
    "header-format": [2, "always"],
    "type-enum": [2, "always", types],
    "scope-enum": [2, "always", scopes],
    "subject-start-case": [2, "always"],
    "subject-full-stop": [2, "never", "."],
    "body-required": [2, "always"],
    "body-leading-blank": [2, "always"],
    "footer-leading-blank": [2, "always"],
    "notice-format": [2, "always"],
    "revert-format": [2, "always"],
  },
};
