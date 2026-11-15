# Reference

Use this section to look up public symbols, configuration, commands, and HTTP
contracts.

## Public API

- [Python API](python-api.md): `Obox`, `AppConfig`, `ModelBuilder`, plugins,
  policies, components, and services.
- [Settings](settings.md): settings modules, database configuration, prefixes,
  and limits.
- [CLI](cli.md): scaffold, user, static asset, jobs, and webhook commands.

## HTTP contracts

- [Resource API](../api-spec.md): implemented resource CRUD, query, selector,
  relationship, batch write, Auth, Files, and webhook routes. Planned routes are
  marked in the source document.
- [Record and Activity API](../record-activity-spec.md): resource record pages,
  concurrency, revisions, and comments. The source document describes its
  implementation status and migration needs.

## Design specifications

These documents preserve detailed design contracts, but they do not all describe
the released runtime. Check the status at the top of each file before using a
proposed API:

- [Core specification](../core-spec.md) — proposed public API and implementation
  contract for the core resource builder.
- [Analytics specification](../extension-spec.md) — proposed replacement
  contract for analytics; compare it with the implemented features described in
  [Analytics](../features/analytics.md).
