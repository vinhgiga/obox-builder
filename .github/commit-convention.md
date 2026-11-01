# Commit messages

Use this format:

```text
type(scope): short summary

why this change is needed
```

The scope is optional. When present, use one of: `builder`, `auth`, `jobs`,
`cli`, `i18n`, `deps`, or `ci`.

Types are `build`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `test`, and
`revert`. Write summaries in lowercase without a final period. The body must be
at least 20 characters; documentation commits may omit it. Breaking changes
and deprecations need a summary and migration or replacement details.

Examples:

```text
feat(builder): add grouped list filters

Let staff combine conditions when narrowing resource lists.
```

```text
fix(jobs): stop blocking the event loop

Move filesystem checks off the worker's asynchronous event loop.
```
