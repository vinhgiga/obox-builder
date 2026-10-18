# Row History — Architecture, System Design & Database Model

**Project:** obox-builder · **Branch:** `dev` @ `dabd36d` (v0.2.1) · **Date:** 2026-10-18 · **Status:** Accepted — Phases 1–4 shipped

> **Scope:** a *row history* feature for the hand-rolled FastAPI admin: record *who* changed *what*, *when* and *from where* on every row; show a timeline; support revert later. This document is based on (1) reading the `dev` branch code and (2) research into Airtable, AppSheet, Jotform and reference systems (Directus, Salesforce, django-auditlog, supa\_audit, SQLAlchemy).

> **Implementation status (2026-10-18):** Phase 1 done — see
> [docs/history.md](../history.md) for the user guide. Phase 2 done:
> M2M (`relations`), global feed `/admin/history` + filters + change-set,
> permission-aware masking, append-only triggers installed via `post_migrate`
> (Postgres blocks UPDATE/DELETE/TRUNCATE; SQLite blocks UPDATE only so dev
> purges keep working), `history.bulk_update`, `history_purge` + per-model
> retention, `history_export` (NDJSON/CSV, formula-injection guard). Phase 3
> done: `VersionedMixin` + `W201`, `row_version` on entries, entry and
> change-set revert (`POST .../revert`, conflict + forced override, deleted-row
> restore, `history.restore_row_change`), `mask` classification with masked
> values in the payload, audit-feed export UI (`GET /admin/history/export`,
> `history.export_row_change`) and `history_anonymize` (actor/object scopes;
> SQLite drops the append-only trigger for the run, PostgreSQL uses
> `obox.history_maintenance`). Minor deviations from the RFC: added the
> `HISTORY_TRACK_ALL_ADMIN_MODELS` setting; the timeline uses the keyset
> `?before=<id>` as designed; the card lives in
> `templates/admin/model/form.html` (regular models),
> `templates/admin/users/form.html` and `templates/admin/roles/form.html`
> (bespoke pages), the shared timeline page in `templates/admin/history.html`
> and the global feed in `templates/admin/history_feed.html`; the blocked-revert
> page is `templates/admin/revert.html`. Phase 4 done: `history_reconcile`
> (per-label replay of create snapshots + diffs; reports
> missing/resurrected/unexpected/stale and `--fix` appends repair entries with
> `source="db_trigger"`), seal blocks (`history_seal` builds Merkle-chained
> `history_seal_block` rows over a lag window; `history_verify` re-derives every
> leaf and exits non-zero on edits/deletes/chain rewrites), `history_relabel`
> (batch `model_label` rewrite under the maintenance guard) and PostgreSQL
> monthly partitioning (`history_partition --sql` prints the reviewed
> conversion DDL — composite PK, validated CHECK, attach of the legacy table,
> shared sequence; `--ensure` creates months + `DEFAULT`; `--drop-before` is
> O(1) retention). The NF2/insert benchmark landed as
> `tests/history/test_write_cost.py` (`pytest -m benchmark`), and the PG-only
> paths (partition conversion, triggers, maintenance GUC) are exercised against
> real PostgreSQL both locally (18.4) and in CI (`postgres:17-alpine`). The
> "deviations" paragraph below predates Phase 4 and is kept for history.

## 0. Decision summary

| # | Decision | Short rationale |
| --- | --- | --- |
| 1 | Capture at the **SQLAlchemy Session** layer (`before_flush` → `after_flush`), in the same transaction as the data | Knows actor/request, has `FieldType`, portable SQLite + Postgres, atomic |
| 2 | **One append-only fact table** `history_row_change`: 1 row = 1 changed row per flush; diff as JSONB `{field: [old, new]}` | Compact, timeline read = 1 index scan, no joins |
| 3 | Group by `change_set_id` (UUID, 1 DB transaction), **no** header table | Avoids FK/savepoint rollback issues; partitionable later |
| 4 | **Reuse** the existing `FieldSpec.history` + `FieldType.diff_repr()` code | No reinvention; finish `diff_repr` for number/temporal/relation |
| 5 | Actor travels via `session.info` (late-bound from the request) + ContextVar for CLI/jobs; **no FK** to `auth_user` | Preserves layering; history survives user deletion |
| 6 | New module `obox_builder.contrib.history`, placed **right below `contrib.admin`** in import-linter; models declare `__history__` (like `__permissions__`) | `auth` never has to import `history` |
| 7 | Immutability in the DB (trigger + REVOKE); tamper-evidence via *seal blocks* in a later phase | Cheap guardrail first, strong guarantees later |
| 8 | 4-phase rollout; **Phase 1 (MVP)** = create/update/delete + per-row timeline for `auth.User/Role` | Early value, low risk |

---

## 1. Context: what `dev` already has / lacks

**Already present (reused):**

- `FieldSpec.history: bool = True` (`fields/base.py`). `introspect.py` sets `history=False` for `secret` columns or names ending in `_hash`; `history` is a reserved key in `info={"admin": {...}}` (test: `token_hash.history is False`).
- `FieldType.diff_repr(value)` — docstring: *"Stable JSON representation used by the change log"*. Implemented for `Text`, `Choice`, `Boolean`, `Json`.
- `get_db()` = **1 transaction / request**, committed at the end; the `_create/_update/_delete` views use `begin_nested()` + `flush()` (savepoint rollback on `IntegrityError`).
- `RequestIDMiddleware` → `request.state.request_id`; `auth_session` stores `ip`, `user_agent`.
- `post_migrate.send_async()` after `upgrade` (auth uses it for `sync_permissions`) → precedent for installing DDL/triggers after migrate.
- `AppConfig.ready()`, `AppConfig.permissions` (app-level permissions), `Model.__permissions__` (model-level permissions), `core.signals.Signal`, the `checks` system (`obox.admin.E101…`).
- `relationship()` defaults to `lazy="raise"` → listeners **must not** trigger IO while reading attributes.
- Tests: the `db` fixture uses `metadata.drop_all/create_all`; CI runs **twice** (SQLite and Postgres 17).

**Missing / risks to handle:**

- `diff_repr` is **missing** for `Integer`, `Decimal`, `Date`, `DateTime`, `Time`, `Link` → `datetime`/`Decimal` are not JSON-serializable.
- **Noise sources:** `touch_session()` writes `last_seen_at` every request; `TimestampMixin.updated_at`; `User.last_login_at / failed_login_count / locked_until`.
- **M2M** (`auth_user_roles`) is a bare `sa.Table`, written via `target.roles = selected` (`views/users.py`) — no `User` column changes.
- **Core bulk** (`delete(AuthSession)…`) bypasses the unit of work → no flush event.
- The README mentions `docs/adr/` but the directory does not exist on `dev` → this document is proposed as `docs/adr/0001-row-history.md`.
- `BaseSettings` uses `extra="forbid"` → every new setting must be declared explicitly.

---

## 2. Research into production systems

### 2.1 Airtable — three separate layers

1. **Record revision history (UI):** the feed lives inside the expanded record. Plan-based retention: Business 2 years, Enterprise Scale 3 years; Enterprise admins can extend up to 10 years (1-year steps). *Clear revision history* is irreversible. Fields synced from external sources are **hidden** from history; fields hidden in the interface also hide their changes (history is **permission-aware**). The community reports that duplicating bases / restoring snapshots loses history and changes record IDs.
2. **Webhooks change feed (pull):** every payload carries `baseTransactionNumber`, `actionMetadata` (source + user), `changedRecordsById` with `current` / `previous` / `unchanged` (previous & unchanged are opt-in), keyed by *field ID*. Consumed with a cursor; webhooks expire after 7 days without a refresh; rate limit 5 req/s/base.
3. **Enterprise audit log:** events API (id, timestamp, action, actor, modelId, modelType, payload, **payloadVersion**, context, origin) and NDJSON-gzip export for windows ≤180 days, with 7-day links.

Also: trash retention defaults to 30 days; records re-created after retention get a **new ID**.

### 2.2 AppSheet — "Audit History" is *not* row history

- Audit History is an **operations log** (sync, add/edit/delete, API call, bot invocation, report). Retention is **7 days** (53 with Enterprise Plus); entries are batched so they can lag ~5 minutes; filtering/analytics are Enterprise-only; BigQuery export for root admins. The *By User* column: `-1` = guest, `-2` = no consent.
- Real row history must be **built yourself**: a "data change event" bot + a Log table using `[_THISROW_BEFORE]` / `[_THISROW_AFTER]`, `USEREMAIL()`; or the backend's Change History (Google Sheets, requires running "As App User").
- **Key limits:** bots only see the before/after of the exact triggering record; changes written **directly to the data source (outside the app) do not trigger bots**; data changes caused by a bot do **not** trigger other bots (loop prevention); blank-cell comparisons historically misfire (NULL/blank bug).

### 2.3 Jotform — three layers, but no field-level diff

- **Account Action Logs** (History tab): submission edits / deletions / purges, logins, API keys… The Enterprise admin console filters by user/time/action and exports Excel/CSV.
- **Tables Revision History:** view and *restore* a previous table version (data, settings, visual); every restore **is logged**.
- **Trash → Purge** in two phases (soft delete, then permanent purge as a separate event).
- **Limitations** (per support): Tables only shows that a submission *was updated* and when — **not** which field or old value changed; workarounds are hidden fields or tag-based email notifications ("EDIT").

### 2.4 Additional references

| System | Lesson |
| --- | --- |
| **Directus** | `directus_activity` (header) + `directus_revisions` (`data` snapshot + `delta` + `parent`). Issue #17894: ~700k revisions made the timeline very slow due to a missing (collection, item) index → dedicated retention for activity/revisions was added. |
| **Salesforce** | Field history: 20 fields/object cap, 18 months (UI) / 24 months (API) retention; Field Audit Trail extends retention to 10 years → *cap cost per field* and *retention as a first-class feature*. |
| **django-auditlog** | `LogEntry`: `object_pk`, `object_repr`, `changes` (JSON), `actor`, `cid`, `remote_addr`, `timestamp`, `additional_data`; `mask_fields`; M2M off by default; `pre_log/post_log` signals; v3 changed `changes` from text to JSON (→ payload versioning needed). |
| **supa\_audit** (Supabase) | A single JSONB table `record` + `old_record`, a stable `record_id` from the PK, BRIN on `ts`. **Repo archived (02/2025)** → reference the schema only, never a dependency. |
| **SQLAlchemy** | The `versioned_history` example uses `before_flush` + `attributes.get_history`; the asyncio docs allow attaching events on `sync_session_class`. |

### 2.5 Comparison

|  | Field-level old→new | Actor + source | Transaction grouping | Retention | Restore | Changes outside the app |
| --- | --- | --- | --- | --- | --- | --- |
| **Airtable** | Yes (history + webhook `previous/current`) | Yes (`actionMetadata.source`) | `baseTransactionNumber` | 2–3 years, up to 10 | Rollback / snapshot | Synced fields hidden |
| **AppSheet** | No built-in (DIY) | `By User`, -1/-2 | No | 7 / 53 days | No | **Not visible** |
| **Jotform** | **No** (only "edited") | Yes (action log) | No | Per plan | Table-level restore | — |
| **Directus** | Yes (`delta`) | Yes | `parent` | Configurable | Revert | Not visible |
| **Salesforce** | Yes (Old/New) | Yes | No | 18–24 months; extendable | No | Not visible |

### 2.6 Lessons → design decisions

| # | Lesson (source) | Decision |
| --- | --- | --- |
| L1 | Separate *operations audit* from *row history* (AppSheet, Airtable) | Dedicated table for data changes only; login/export events later |
| L2 | Old/new **per field** is mandatory (Jotform lacks it) | `changes.fields = {f: [old, new]}` |
| L3 | Group operations by transaction (Airtable, Directus `parent`) | `change_set_id` |
| L4 | Explicit source + actor with guest/system sentinels (Airtable `source`, AppSheet -1/-2) | `source`, `actor_id NULL`, `actor_label` |
| L5 | Blind spot when writes happen outside the app (AppSheet) | Bulk guard + DB trigger + `history_reconcile` (replay + `--fix`) |
| L6 | Recursion prevention (bots do not trigger bots) | `RowChange` is never tracked; written with a Core insert |
| L7 | Wrong null/blank comparisons cause misfires (AppSheet) | Comparison with a `NO_VALUE` sentinel, normalised through `diff_repr`; `""` ≠ `None` when the type distinguishes them |
| L8 | Right indexes from day one + keyset pagination (Directus #17894) | Index `(model_label, object_pk, id DESC)`; no OFFSET |
| L9 | Retention as a first-class, per-model feature (Airtable, Salesforce, Directus) | `HISTORY_RETENTION_DAYS` + per-model override; `history_purge` and (PG) `history_partition --drop-before` |
| L10 | Reads must be permission-aware (Airtable) | Filter fields by `FieldSpec` + viewer permissions |
| L11 | Restore is a *forward* action, logged, with conflict checks (Jotform); IDs are reused (Airtable) | `reverts_id`; UI splits "incarnations" at `delete` |
| L12 | Payload has a version (Airtable `payloadVersion`; auditlog v3) | `schema_version` column |
| L13 | Cap cost (Salesforce 20-field limit) | `HISTORY_MAX_VALUE_BYTES`, allow-lists, skip no-ops |
| L14 | Do not depend on archived libraries (supa\_audit) | Implement it ourselves; borrow schema ideas only |
| L15 | Mask/redact sensitive data (auditlog `mask_fields`) | `redacted` marker, optional `mask` |

---

## 3. Requirements

**Functional**

- **F1** Record every INSERT/UPDATE/DELETE of tracked models: actor, timestamp, field old→new, source, request id, IP.
- **F2** Per-row timeline in the admin (Airtable-style activity feed), keyset paginated.
- **F3** Global audit feed, filtered by model / actor / source / time / `change_set`.
- **F4** *Permission-aware* rendering: viewers only see fields they are allowed to see; secrets never leak.
- **F5** Revert a change / restore a deleted row (Phase 3), always producing a new entry.
- **F6** NDJSON/CSV export, per-model retention, safe purge.

**Non-functional**

- **NF1 Atomic:** history commit ⇔ data commit (same transaction).
- **NF2 Low overhead:** 1 `executemany` / flush; no extra SELECTs for updates. Measured (2026-10-18, in-memory SQLite on macOS, `tests/history/test_write_cost.py -m benchmark`): vs the same ORM flush on an untracked model, **+44 µs/row on creates (×1.38)** and **+117 µs/row on 1-field updates of a 12-column model (×12 vs a 3-column control)**; ~8k changed rows/s; the invariant test asserts exactly +1 `INSERT` statement per flush.
- **NF3 Append-only** and eventually tamper-evident.
- **NF4 Portable:** SQLite (dev/test) + Postgres (prod); Postgres-only features must be optional.
- **NF5 Correct layering** (import-linter).
- **NF6 Schema evolution:** renaming/dropping/retyping columns must not break old history.
- **NF7 Observable:** logs, signals, checks, CLI commands.

**Out of scope for v1:** row comments, recording *reads* (access log), schema/config history, tracking changes made outside the app, 3-way diff UI.

---

## 4. Architecture

### 4.1 Component view

```
 HTTP request                                      CLI / background job
 ┌──────────────────────────────────────┐          ┌─────────────────────────┐
 │ RequestIDMiddleware                   │          │ async with history.use( │
 │ admin router: Depends(bind_history)   │          │   source="cli", ...)     │
 │   → db.info["obox.history.ctx"] = lazy│          │   → ContextVar          │
 └───────────────────┬──────────────────┘          └────────────┬────────────┘
                     ▼                                          ▼
        AsyncSession (get_db: 1 txn/request)            session_scope()
                     │  views / services: add, setattr, delete, flush
        ┌────────────▼─────────────────────────────────────────────┐
        │ contrib.history.capture (listeners on the sync Session)    │
        │  before_flush → compute Pending from attribute history     │
        │                 (no IO)                                    │
        │  after_flush  → 1× INSERT executemany (same transaction)   │
        └────────────┬─────────────────────────────────────────────┘
                     ▼
          history_row_change  (append-only, commits with the data)
                     ▲
        HistoryService (keyset, filters, permission-aware masking)
                     ▲
   admin: /<model>/{pk}/history · form.html card · /admin/history
```

### 4.2 Layering (import-linter)

```toml
layers = [
  "obox_builder.contrib.admin",
  "obox_builder.contrib.history",   # NEW — admin may import it, auth does not need to
  "obox_builder.contrib.auth",
  "obox_builder.core", ... "obox_builder.db", "obox_builder.apps", "obox_builder.conf", "obox_builder.utils",
]
```

`history` only imports `db`, `fields`, `apps`, `core.signals/utils`, `conf` — all lower layers. Models declare it declaratively and **import nothing**:

```python
class User(Model, TimestampMixin):
    __history__ = {"relations": ["roles"], "exclude": ["meta"]}
```

The `Model` base gains `__history__: ClassVar[bool | Mapping[str, Any] | None] = None` (same shape as `__permissions__`). `contrib.history` reads it in `ready()`. Third-party models can opt in programmatically with `history.track(Model, ...)` (like `auditlog.register`).

### 4.3 Write path

1. `RequestIDMiddleware` assigns `request_id`; the admin router attaches a *resolver* to `db.info` (the user is unknown yet — late binding).
2. The authentication dependency fills `request.state.user`; the view mutates the object and calls `flush()` inside `begin_nested()`.
3. **`before_flush`**: resets `session.info[PENDING]` (garbage from a previously failed flush); walks `session.new / dirty / deleted` and, for tracked types, computes `Pending` from *attribute history* — it *never* touches an attribute that could lazy-load.
4. SQLAlchemy emits INSERT/UPDATE/DELETE → new-row PKs are now available.
5. **`after_flush`**: builds rows, resolves the context, `session.connection().execute(insert(RowChange), rows)` — a single `executemany`.
6. Savepoint rollback (`IntegrityError` in `_create`) rolls history back with it; with no header table, `change_set_id` is always valid.
7. End-of-request commit → atomic. The `post_record` signal is sent after `after_commit`.

`change_set_id` is generated lazily, stored in `session.info`, and cleared on the outermost transaction's `after_commit` / `after_rollback`.

### 4.4 Applied design patterns

| Pattern | Use | Why |
| --- | --- | --- |
| **Observer** | Session events + `Signal` (`pre_record` / `post_record`) | Capture without touching views/services |
| **Unit of Work** | `flush` is the batch boundary + atomicity | 1 insert per flush |
| **Strategy** | `FieldType.diff_repr()` | Matches the core philosophy of `fields/` |
| **Registry** | `trackers: dict[type, Tracker]` built in `ready()` | Like `site.register`, `field_types` |
| **Context Object + Null Object + Ambient Context** | `HistoryContext`; `system` actor; ContextVar | No actor threading through every function |
| **Append-only event log** | `history_row_change` | Immutable facts; derive state by reverse-apply |
| **Memento (reverse delta)** | Store `old` → restore without snapshots | Two-way replay |
| **Command** | Revert = an explicit command that produces a new change | History is never edited |
| **Repository / Query Object** | `HistoryService`, `QuerySpec`-style filters | Reading is decoupled from the UI |
| **Cursor-based pull / outbox** | Monotonic `id` as the cursor for external shippers | Like the Airtable payload cursor |
| **Dependency inversion** | Declare `__history__` instead of importing | Preserves layering |
| **Fail-closed / fail-open policy** | `HISTORY_FAILURE_MODE` | Compliance vs availability |

---

## 5. Database model

### 5.1 Overview

A single fact table: no outgoing FKs, no header table.

```
history_row_change
  id            BIGINT identity PK     monotonic: timeline order + pagination cursor
  occurred_at   timestamptz            UTC, set by the app
  change_set_id uuid                   1 DB transaction (multi-row change group)
  op            create | update | delete
  model_label   'auth.user'   object_pk '42'   object_repr 'vinh'
  changes       jsonb (payload v1)     schema_version smallint
  row_version   int NULL               (when the model has VersionedMixin)
  actor_id NULL · actor_label · source · request_id · ip · user_agent
  reason NULL · reverts_id NULL
```

### 5.2 Model (SQLAlchemy 2.0, following existing conventions)

```python
# obox_builder/contrib/history/models.py
class RowChange(Model):  # tablename derived: history_row_change
    __history__ = False  # recursion guard: never track itself

    id: Mapped[int] = mapped_column(BigIntIdentity, primary_key=True)
    occurred_at: Mapped[datetime] = mapped_column(TZDateTime, default=utcnow)
    change_set_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid)
    op: Mapped[str] = mapped_column(sa.String(8))
    model_label: Mapped[str] = mapped_column(sa.String(150))
    object_pk: Mapped[str] = mapped_column(sa.String(255))
    object_repr: Mapped[str] = mapped_column(sa.String(255), default="")
    changes: Mapped[JsonDict] = mapped_column(JSONB)
    schema_version: Mapped[int] = mapped_column(sa.SmallInteger, default=1)
    row_version: Mapped[int | None]
    actor_id: Mapped[int | None] = mapped_column(sa.BigInteger)  # NO FK
    actor_label: Mapped[str] = mapped_column(sa.String(150), default="")
    source: Mapped[str] = mapped_column(sa.String(16))
    request_id: Mapped[str | None] = mapped_column(sa.String(64))
    ip: Mapped[str | None] = mapped_column(sa.String(45))
    user_agent: Mapped[str | None] = mapped_column(sa.String(255))
    reason: Mapped[str | None] = mapped_column(sa.String(500))
    reverts_id: Mapped[int | None] = mapped_column(sa.BigInteger)  # soft ref, no FK

    __table_args__ = (
        sa.CheckConstraint("op IN ('create','update','delete')", name="op"),
        sa.CheckConstraint(
            "source IN ('admin_ui','api','cli','system','import','revert','db_trigger')",
            name="source",
        ),
        sa.Index("ix_history_row_change_target", "model_label", "object_pk", sa.desc("id")),
        sa.Index("ix_history_row_change_actor", "actor_id", sa.desc("id")),
        sa.Index("ix_history_row_change_model", "model_label", sa.desc("id")),
        sa.Index("ix_history_row_change_change_set", "change_set_id"),
        sa.Index("ix_history_row_change_occurred_at", "occurred_at", postgresql_using="brin"),
    )
```

| Column | Decision |
| --- | --- |
| `model_label` | Reuse the existing `ModelAdmin.model_label` identity (`app_label.model_name`) |
| `object_pk` | Normalised text (int/str/UUID). The admin only supports single PKs (`primary_key[0]`); composite PKs → JSON array as text |
| `object_repr` | `str(obj)` snapshot, fallback `#pk` (same convention as `_object_label_for`) so deleted rows stay readable |
| `actor_id` / `actor_label` | No FK: no `auth` import, survives user deletion. `actor_label` = the username at that moment |
| `reverts_id` | Soft reference, no FK, so partitioning/purging has no constraints |
| `row_version` | Absolute per-row sequence when the model uses `VersionedMixin` (Phase 3); NULL otherwise |

**Four invariants that keep the partition door open:** `occurred_at` is always NOT NULL and app-set; no FK pointing *into* this table; no unique constraints beyond the PK; every timeline query always carries `(model_label, object_pk)`.

### 5.3 `changes` payload — schema v1

```json
{
  "fields":      {"status": ["draft", "published"], "title": ["Hello", "Hello world"]},
  "m2m":         {"roles": {"add": ["3"], "remove": ["1"]}},
  "redacted":    ["password_hash"],
  "truncated":   {"body": {"len": 120345, "sha256": "9f2c..."}},
  "unknown_old": []
}
```

- Keys are sorted by field name (deterministic → golden tests). Values go through `FieldType.diff_repr`.
- `create`: `[null, new]` for every non-null field. `delete`: `[old, null]` for every non-null field ⇒ the **tombstone is the full final snapshot**, so no separate snapshot column is needed. `update`: only fields with a net change.
- Drop the PK (it lives in `object_pk`); drop `exclude`d or `history=False` fields. `secret` fields appear only in `redacted` (you know *that* it changed, never the value).
- FKs (`Link`): store the raw FK column value, never the related object's repr.
- M2M: ids as strings (stable for int/UUID), recorded on the row of the *actively-mutating* side.
- Values over `HISTORY_MAX_VALUE_BYTES`: store a 256-character preview + `{len, sha256}` under `truncated`; such an entry **cannot be reverted**.
- `unknown_old`: the field changed but its old value was never loaded (deferred/expired). Never lazy-load inside the listener.
- Adding a new key is backward-compatible; changing the meaning of an existing key bumps `schema_version`; readers support at least 2 versions.

### 5.4 Indexes and query shapes

```sql
-- One row's timeline: keyset, never OFFSET
SELECT * FROM history_row_change
WHERE model_label = :m AND object_pk = :pk AND id < :cursor
ORDER BY id DESC LIMIT 51;            -- 50 + 1 to know whether more pages exist

-- Global feed
... WHERE model_label = :m AND id < :cursor ORDER BY id DESC LIMIT 51;
... WHERE actor_id    = :a AND id < :cursor ORDER BY id DESC LIMIT 51;

-- Everything from one change_set (1 request = 1 change_set)
... WHERE change_set_id = :cs ORDER BY id;
```

`request_id` is for log correlation; to look it up directly, add a partial index `WHERE request_id IS NOT NULL`. The write cost of the 5 indexes on an insert-only table was measured in Phase 1 (NF2: +47 µs/row create, +117 µs/row update at 500 rows/commit on SQLite, see 5.6); if the write path is sensitive, drop the `model` index (per-model feeds can filter with BRIN + `model_label`).

### 5.5 Append-only (defense in depth)

1. **App:** Core INSERT only; there is no UPDATE/DELETE path in the code except maintenance commands.
2. **DB triggers**, installed by a `post_migrate` receiver (same mechanism as `sync_permissions_receiver`, idempotent):

```sql
CREATE OR REPLACE FUNCTION history_forbid_mutation() RETURNS trigger AS $$
BEGIN
  IF current_setting('obox.history_maintenance', true) = 'on' THEN
    RETURN COALESCE(NEW, OLD);
  END IF;
  RAISE EXCEPTION 'history_row_change is append-only (%)', TG_OP USING ERRCODE = '42501';
END $$ LANGUAGE plpgsql;

CREATE TRIGGER history_row_change_immutable BEFORE UPDATE OR DELETE ON history_row_change
  FOR EACH ROW EXECUTE FUNCTION history_forbid_mutation();
CREATE TRIGGER history_row_change_no_truncate BEFORE TRUNCATE ON history_row_change
  FOR EACH STATEMENT EXECUTE FUNCTION history_forbid_mutation();
```

SQLite (dev/test): a trigger blocks `UPDATE` with `RAISE(ABORT, ...)`; `DELETE` stays allowed so dev purges keep working. 3. **DB roles (the real boundary):** `GRANT INSERT, SELECT` to the app role; `REVOKE UPDATE, DELETE, TRUNCATE`; a separate maintenance role for purge/anonymize.

> ⚠️ The `obox.history_maintenance` GUC is only a guardrail against accidents/bugs: anyone with `SET` privilege can turn it on. The real security boundary is `REVOKE` + a separate role. Defeating a malicious superuser/DBA requires hash-chaining and external anchoring (shipped: `history_seal`/`history_verify`).

### 5.6 Retention, partitioning, sizing

- **Retention:** global `HISTORY_RETENTION_DAYS` + per-model override via `__history__["retention_days"]`. The `history_purge` command deletes in batches (≤10k rows per batch, sleeping between batches), enables `SET LOCAL obox.history_maintenance = 'on'`, and logs every batch to a logger (never into the table itself).
- **Partitioning (shipped: Postgres only):** monthly RANGE on `occurred_at`, PK `(id, occurred_at)`, with a `DEFAULT` partition so inserts never fail when cron drifts; retention = `DROP PARTITION` (O(1), no bloat). Migrating the existing table: create the new partitioned table and attach the old table as the first partition (requires a unique index containing the partition column and a validated CHECK range) or backfill in batches; the shipped path is `history_partition --sql` → reviewed DDL → `--ensure` for new months → `--drop-before` for retention. Timeline queries traverse partitions via MergeAppend on the `(model_label, object_pk, id DESC)` index + `LIMIT`, so they stop early.
- **Sizing (measured):** payload JSON averaged **213 B** per entry in the Phase 1 benchmark (12-column model, 80-char text, JSON meta; creates 313 B, updates ~115 B). On PostgreSQL 18 (10k rows, 245 B payloads, all indexes, fresh insert, no bloat) the table measures **603 B/entry** (heap 432 B + indexes 168 B) — close to the 500 B working estimate. Budget ~600 B/entry ⇒ 1M entries ≈ 0.6 GB before any bloat.

### 5.7 Data shapes considered

| Option | Pros | Cons | Verdict |
| --- | --- | --- | --- |
| Full row snapshot per version (SQLAlchemy `history_meta`, simple-history) | Easy restores | Grows with row width × number of edits | No |
| EAV, one row per field (Salesforce) | Easy field-level queries and ACLs | N rows per save, loses the row's atomicity | No |
| **Delta JSONB / row** (auditlog, Directus `delta`) | Compact, 1 row/row/flush | Field-level queries need JSONB | **Chosen** |
| Delta + periodic checkpoint snapshots | Fast point-in-time | More complex | Later if needed (nullable column, cheap on Postgres) |

---

## 6. Capture engine

### 6.1 Tracker (resolved once in `ready()`)

Field source: `introspect_model(model, overrides=...)`, then filter: drop the PK; drop `FieldSpec.history is False` (non-secret); drop `TimestampMixin` columns (`created_at`, `updated_at`); drop `__history__["exclude"]`; when `__history__["fields"]` is set, keep only the allow-list. `secret` fields go to `redacted` only. For `auth.User`, attach `info={"admin": {"history": False}}` to `last_login_at`, `failed_login_count`, `locked_until`, `password_changed_at` to avoid noise.

### 6.2 Pseudocode

```python
@event.listens_for(Session, "before_flush")
def before_flush(session, ctx, instances):
    pending = session.info[PENDING] = []  # reset: a previously failed flush leaves no garbage
    for obj in session.new:
        _add(pending, obj, "create")
    for obj in session.dirty:
        _add(pending, obj, "update")
    for obj in session.deleted:
        _add(pending, obj, "delete")


def _add(pending, obj, op):
    tracker = trackers.get(type(obj))
    if tracker is None:
        return
    change = tracker.diff(obj, op)  # reads only loaded attribute history, no IO
    if change.has_effect:  # no-op / only excluded fields => skip
        pending.append(change)


@event.listens_for(Session, "after_flush")
def after_flush(session, ctx):
    pending = session.info.pop(PENDING, [])
    if not pending:
        return
    context = resolve_context(session)  # session.info -> ContextVar -> system
    cs = session.info.setdefault(CHANGE_SET, uuid4())
    rows = [c.to_row(context, cs) for c in pending]  # new-row PKs exist after flush
    session.connection().execute(sa.insert(RowChange), rows)  # 1 executemany, same transaction
```

- **Update:** for each spec read `state.attrs[key].history` (this property does *not* emit a loader). An empty `deleted` means the old value was never loaded ⇒ add it to `unknown_old`.
- **Delete:** use `state.attrs[key].loaded_value` (returns `NO_VALUE` when never loaded).
- **Comparison:** `diff_repr(old) != diff_repr(new)` with a `NO_VALUE` sentinel (lesson L7, avoiding AppSheet's blank=blank bug).
- **M2M:** for each relationship in `__history__["relations"]` that has a `secondary`, read `history.added / deleted` ⇒ `m2m[rel] = {add, remove}`. The collection must be loaded before assignment (the `users.py` code already does this); **add a test pinning this behaviour** because of `lazy="raise"`.
- Listeners attach to the `sqlalchemy.orm.Session` class (the framework is async-only, so that is exactly the sync session under `AsyncSession`).

### 6.3 Edge-case matrix

| # | Situation | Behaviour | Mitigation / test |
| --- | --- | --- | --- |
| 1 | Rollback or savepoint rollback | No history (same transaction) | Test `_create` with `IntegrityError` |
| 2 | Bulk `update()`/`delete()` (ORM-enabled) | Bypasses the unit of work ⇒ no entry | `do_orm_execute` guard honouring `HISTORY_BULK_POLICY` (`ignore`/`warn`/`raise`); `history.bulk_update()` helper |
| 3 | Raw SQL / psql / another service | **Blind spot** (like AppSheet) | DB trigger blocks tampering; `history_reconcile` replays and `--fix` repairs; documented |
| 4 | `ON DELETE CASCADE` in the DB | Child rows are not recorded | The parent's `delete` records the loaded m2m snapshot; or use ORM cascades on tracked models |
| 5 | Two writers editing one row | `old` may be stale (lost update) | `VersionedMixin` (`version_id_col`) ⇒ `StaleDataError`; DB trigger for absolute accuracy |
| 6 | Deferred/expired attribute | `unknown_old`, **no** lazy-load | Test with `lazy="raise"` |
| 7 | PK reuse (SQLite) | Timeline merges two "lives" | UI splits at `delete`; Postgres identity never reuses |
| 8 | No-op save / only excluded columns changed | Skipped | Fights `touch_session` noise |
| 9 | Huge values | Truncate + sha256 | `HISTORY_MAX_VALUE_BYTES` |
| 10 | Secret field | `redacted` marker only | Test "value never leaks" |
| 11 | `Decimal`, `datetime`, enums | Through `diff_repr` | Finish the missing types in Phase 1 |
| 12 | Same row flushed twice in one transaction | 2 entries in the same `change_set` | UI groups by `change_set` |
| 13 | Model renamed | Old `model_label` becomes orphaned | `history_relabel` maintenance command |
| 14 | Column dropped/retyped | Keep old names, UI labels them "legacy field" | `schema_version`, `humanize(name)` fallback |
| 15 | CLI / job without a request | ContextVar or the `system` actor | `history.use(...)` |
| 16 | History insert fails | `closed`: fail the whole transaction; `open`: savepoint + log + metric | `HISTORY_FAILURE_MODE` |

---

## 7. Context propagation (actor, source)

```python
@dataclass(frozen=True, slots=True)
class HistoryContext:
    actor_id: int | None
    actor_label: str  # username | "system" | "anonymous" | "cli:<os-user>"
    source: str  # admin_ui | api | cli | system | import | revert | db_trigger
    request_id: str | None = None
    ip: str | None = None
    user_agent: str | None = None
    reason: str | None = None
    reverts_id: int | None = None
```

Resolution order: `session.info["obox.history.ctx"]` (late-bound callable) → ContextVar → `HistoryContext.system()`.

```python
# Web: the admin router adds a dependency next to bind_site
async def bind_history(request: Request, db: AsyncSession = Depends(get_db)) -> None:
    history.bind(db, request)  # stores a closure; reads request.state.user at flush time


# CLI / job
async with history.use(source="cli", actor_label="cli:createsuperuser"):
    await services.create_user(...)
```

- **Late binding is essential:** the router-level dependency runs *before* `permission_required` (which fills `request.state.user`), so the resolver must read the user at flush time, not at bind time.
- **IP:** trust `X-Forwarded-For` only when `HISTORY_TRUSTED_PROXIES` is configured; otherwise use `request.client.host` (audit logs must not trust client-supplied headers).
- **Sentinels:** `actor_id = NULL` + `actor_label` ∈ {`system`, `anonymous`, `cli:<user>`} (learned from AppSheet `-1/-2`, but without magic numbers).

---

## 8. Read path, UI, and permissions

| Route | Purpose | Permission |
| --- | --- | --- |
| `GET /admin/<path>/{pk}/history` | Full timeline, `?before=<id>` | `ModelAdmin.has_history_permission` |
| `GET /admin/history` | Global feed + filters | `history.view_row_change` |
| `GET /admin/history/{change_set_id}` | Detail of one change set | same as above |
| `POST /admin/<path>/{pk}/history/{entry_id}/revert` (P3) | Revert | `history.restore_row_change` + the model's `change`/`add` |
| `GET /admin/history/export` (P3) | NDJSON / CSV | `history.export_row_change` |

- Per-model routes are registered in `register_model_routes` when the model is tracked.
- `has_history_permission(ctx, obj=None)` = `history.view_row_change` **and** `has_view_permission` (following the existing `has_*_permission` pattern). App-level permissions are declared in `HistoryConfig.permissions` (the existing `AppConfig.permissions` + `sync_permissions` mechanism). The global feed only shows models the viewer has `view` on.
- **UI:** the right column of `form.html` (currently just the button card) gains a *Recent activity* card (5 entries) + a *View all* link; shown on change pages only, not on add. Each entry: actor initials, relative time (UTC tooltip), `op` and `source` badges, a `field: old → new` list. Stack unchanged: Jinja2 SSR + Tabler. Autoescape stays on; **`|safe` is forbidden** for old/new values (XSS from stored data).
- **Permission-aware masking (L10):** `visible = {spec.name for spec in form_specs + list_specs if not spec.secret}`; fields outside this set are filtered at render time. `redacted` renders as "changed" without a value. Fields no longer in any spec (legacy) are superuser-only because their sensitivity is unknown.
- `HistoryService` returns dataclasses already masked; templates never read raw JSON:

```python
recent(db, admin, pk, limit=5)
timeline(db, model_label, pk, before=None, limit=50)
feed(db, filters, before=None, limit=50)
change_set(db, change_set_id)
```

---

## 9. Revert / Restore (Phase 3)

Principle: **never edit or delete an existing entry.** A revert is a forward change, logged with `source='revert'` and `reverts_id` (Jotform also logs every restore).

`revert(entry)`:

1. Check permissions; block fields that are `truncated`/`redacted`/`mask`ed and list exactly which fields cannot be restored.
2. In one transaction: `SELECT ... FOR UPDATE` the current row (Postgres; skipped on SQLite).
3. **Per-field conflict check:** `current == entry.new` passes; otherwise return 409 with `old / new / current` for the user to choose (field-level 3-way).
4. Apply `old` through the standard path (`FieldType.from_input` + the form schema) so validators/constraints still run; context `source=revert`.
5. The capture engine writes a new entry `op=update, reverts_id=entry.id` by itself.

Variants: reverting a `delete` = INSERT again with the original PK if it is unused (the original PK is always below the current sequence value, so no collision), recorded as `op=create`; reverting a `create` = a confirmed delete; reverting a whole `change_set` = apply in reverse `id DESC` order inside one transaction, or fail entirely.

---

## 10. Security, privacy, compliance

- **Secrets** are never stored, only the `redacted` marker.
- **PII:** `__history__["mask"]` (partial masking like `a***@x.com`; such fields cannot be reverted) or `redacted` (marker only); choose per field.
- **Erasure (GDPR):** `history_anonymize --actor-id N` or `--model M --pk K` runs under the maintenance role, replacing `actor_label`/values with opaque tokens and **never deleting** entries (timeline structure stays intact); every run is logged.
- **Access:** permissions as in section 8; exports are rate-limited and logged (logger `obox.history.audit`); CSV guards against formula injection (prefix `'` on cells starting with `=`, `+`, `-`, `@`).
- **Never trust client-supplied data:** `RequestIDMiddleware` currently accepts the raw `x-request-id` header ⇒ normalise it to `[A-Za-z0-9._-]{1,64}` before storing, and treat it as a *correlation hint*, not evidence. IP trust follows `HISTORY_TRUSTED_PROXIES`.
- **Encryption at rest:** at the DB/disk layer; history holds copies of sensitive data, so it needs the same protections as the source table.
- **Tamper evidence (shipped):** table `history_seal_block(block_id, first_id, last_id, count, merkle_root, prev_block_hash, block_hash, sealed_at)`. A `history_seal` worker seals entries *older than a lag window* (larger than `idle_in_transaction_session_timeout`/`statement_timeout`, e.g. 10 minutes) in `id` order, so late-committing entries cannot slip into a sealed block; `block_hash` is periodically anchored outside (WORM object storage / a log service); `history_verify` re-checks. Entries stay immutable; hashes are never written back into entries.

---

## 11. Configuration, observability, system checks

**Settings** (added to `BaseSettings`, prefix `OBOX_`; explicit declaration mandatory because of `extra="forbid"`):

| Setting | Default | Meaning |
| --- | --- | --- |
| `HISTORY_ENABLED` | `True` | Master switch |
| `HISTORY_FAILURE_MODE` | `"closed"` | `closed`: fail the whole transaction; `open`: savepoint + log + continue |
| `HISTORY_BULK_POLICY` | `"warn"` | Bulk update/delete on tracked models: `ignore` / `warn` / `raise` |
| `HISTORY_MAX_VALUE_BYTES` | `8192` | Truncation threshold per value |
| `HISTORY_RETENTION_DAYS` | `None` | `None` = keep forever |
| `HISTORY_TRUSTED_PROXIES` | `[]` | Proxies trusted for `X-Forwarded-For` |
| `HISTORY_TRACK_ALL_ADMIN_MODELS` | `False` | Enable for every model registered in the `AdminSite` |

**Per-model declaration:** `__history__ = True` or `{"fields": [...], "exclude": [...], "relations": [...], "mask": [...], "retention_days": int}`.

**System checks** (same `core/checks` system): `obox.history.E201` composite/missing PK; `E202` `relations` entry is not a relationship with a `secondary`; `E203` unknown field in `fields`/`exclude`/`mask`; `W201` tracked model without `VersionedMixin` (lost-update risk); `W202` `HISTORY_FAILURE_MODE="open"`.

**Signals:** `pre_record`, `post_record` (synchronous, using `core.signals.Signal.send`; `post_record` fires after `after_commit`) are the plug points for Prometheus/OTel and SIEM shippers. Suggested metrics: `history_entries_written_total{model,op}`, `history_write_seconds`, `history_failures_total`, `history_bulk_untracked_total`, `history_table_bytes`. Logs: logger `obox.history`.

**CLI** (under the app's `management/commands/`, like `sync_permissions`): `history_purge`, `history_export`, `history_anonymize`, `history_relabel` (all shipped, together with `history_seal`, `history_verify`, `history_partition`, `history_reconcile`).

---

## 12. Test strategy

Package `tests/history/`, using the `db` / `client` / `login` fixtures. CI already runs twice (SQLite and Postgres 17), so every test must be portable by default; Postgres-only tests skip by dialect.

| Group | Content |
| --- | --- |
| Unit (pure) | Diff engine; `diff_repr` for every `FieldType` (JSON round-trip); truncate/redact/mask; **golden file** for the v1 payload to freeze the schema |
| Integration (flush) | create/update/delete; no-op; excluded columns; M2M `roles`; `__history__ = False` models are not tracked; `RowChange` never tracks itself |
| Atomicity | Rollback ⇒ 0 entries; `IntegrityError` inside `begin_nested()` ⇒ 0 entries and no garbage leaking into the next flush; `change_set_id` resets after commit |
| Context | Web: correct `actor_id` even though the router dependency runs before auth (late binding); CLI `history.use`; default `system`; sanitized `request_id` |
| Admin UI | History pages render; 403 without permission; secret/hidden fields never leak; XSS (`<script>` in old/new is escaped); card in the form |
| Performance | Test that SQL count per flush = exactly +1 insert (no N+1); benchmarks behind a marker, not run by default |
| Postgres-only | Append-only trigger; two concurrent sessions on one row (with/without `VersionedMixin`); partitioning (P4) |
| Architecture | `lint-imports` with the new layer; `ruff`; `pyright`; `mkdocs build --strict` |

---

## 13. Roadmap

Rough estimates for one developer familiar with the codebase — ordering aid, not commitments.

| Phase | Content | Exit criteria | Size |
| --- | --- | --- | --- |
| **0** | ADR `docs/adr/0001-row-history.md`; listener spike + insert benchmark 1k/10k rows, measure overhead and entry size | Measurements replace the assumptions in 5.6 and NF2 — done 2026-10-18 (`tests/history/test_write_cost.py -m benchmark`) | ~0.5 week |
| **1 — MVP** | `contrib.history` app; `RowChange` + migration; finish `diff_repr`; capture create/update/delete (no M2M yet); context (session.info + ContextVar + admin bind); `Model.__history__`; track `auth.User/Role`; settings, checks; timeline page + form card; docs | CI green on SQLite + PG; tests prove secrets never leak; atomicity tests; overhead within the measured budget | ~1.5–2 weeks |
| **2** | M2M; bulk guard + helper; global feed + filters; permission-aware masking; immutability trigger (`post_migrate`); signals; `history_purge`, `history_export`; retention | Feed respects permissions; batches purge safely; trigger blocks UPDATE | ~1–1.5 weeks |
| **3** | Revert/restore (incl. deleted-row restore); `VersionedMixin`; export UI; `mask`; `history_anonymize` | Conflict checks correct; revert always produces a new entry | ~1.5 weeks |
| **4** | Postgres hardening: partitioning + DEFAULT partition; safety-net trigger + `history_reconcile`; seal blocks + `history_verify`; REVOKE/role docs; `history_relabel` | Retention via DROP PARTITION; verify detects edits/deletes — done 2026-10-18 (`tests/history/test_partition.py`, `test_seal.py`, `test_reconcile.py`; PG paths run against real PostgreSQL) | ~2 weeks |

**Phase 1 changed-file list:**

```
obox_builder/contrib/history/{__init__,apps,models,options,tracker,capture,context,payload,service,checks,signals}.py
obox_builder/db/base.py                    + Model.__history__ ClassVar
obox_builder/fields/types/{number,temporal,relation}.py    + diff_repr
obox_builder/contrib/auth/models.py        + __history__ for User/Role; history=False for noisy columns
obox_builder/contrib/admin/{options,urls}.py, views/history.py (new), views/model.py (route + card)
obox_builder/contrib/admin/templates/admin/history/*.html, model/form.html (card), navigation.py ("Audit log" entry)
obox_builder/conf/base.py                  + HISTORY_* settings
pyproject.toml                             + importlinter layer
docs/history.md · docs/adr/0001-row-history.md · mkdocs.yml · CHANGELOG.md
tests/history/...
```

---

## 14. Considered decisions (summarised ADR)

| Topic | Chosen | Rejected | Why |
| --- | --- | --- | --- |
| Capture | SQLAlchemy Session events | DB trigger; CDC (logical decoding/Debezium); overriding `save()` | Only the app knows the actor, request and `FieldType`; portable; the trigger stays as a Phase 4 safety net; CDC is too heavy for an internal admin |
| Library | Build it ourselves | sqlalchemy-continuum (per-model shadow tables); supa\_audit (archived, Postgres-only) | Shadow tables double the schema/migrations; we need to match `FieldType`, async and `lazy="raise"` |
| Change grouping | Denormalized `change_set_id` | Header table (like Directus `activity`) | Avoids FK/savepoint issues, easier partitioning; the metadata-duplication cost is small |
| Actor | `actor_id` + `actor_label` snapshot | FK to `auth_user` | Preserves layering, survives user deletion; the trade-off is that erasure needs a maintenance command |
| Partitioning | Deferred to Phase 4 | Do it on day one | Composite PK + Identity is not portable to SQLite; the four invariants keep the door open |
| Tracking default | Opt-in per model (`__history__`) | Track everything | Controls volume (Salesforce lesson); `auth` is always on |

---

## 15. Risks and open questions

**Risks**

1. **Blind spot for writes outside the app** (raw SQL, other services, bulk): reduced by the guard + Phase 4, but never fully eliminated at the app layer.
2. **Stale `old` under concurrent writers** when the model lacks `VersionedMixin` (edge #5).
3. **A listener bug breaks the flush:** fail-closed by default; needs thorough tests and `HISTORY_FAILURE_MODE`.
4. **SQLAlchemy attribute-history semantics** for collections/replace/deferred: must be pinned by tests in Phase 1 on both SQLite and PG.
5. **Unbounded growth:** retention + (P4) partitioning; watch `history_table_bytes`.
6. **`RequestIDMiddleware` trusts a client `x-request-id`:** must be sanitized before storing.

**Open questions for you** (they influence priority order)

1. Compliance level: internal audit or a regulated environment? (prioritises immutability/hash-chaining)
2. Which models must be tracked at launch: only `auth`, or every admin model?
3. Is revert/restore mandatory for v1?
4. Retention requirement: 1 year, 3 years, or forever?
5. Is production Postgres-only? (unlocks partitioning and triggers)
6. Are there writers outside the admin (public API, workers, other services)? (prioritises the safety-net trigger)

---

## Sources

- **Airtable:** record revision history; data retention policies; Webhooks API (payload, expiration); audit log events API — support.airtable.com, airtable.com/developers/web/api
- **AppSheet:** Monitor app activity using Audit History (support.google.com/appsheet/answer/10104794); Bots, Events and data change, `_THISROW_BEFORE/AFTER` (answers 11432969, 11547057, 11445188)
- **Jotform:** account activity logs (jotform.com/help/370); Tables revision history (jotform.com/help/1011); Enterprise admin console; forum "Tables: Viewing Specific Data Updates and Revision History for Submissions"
- **Directus:** schema docs, revisions reference, issue #17894, PR #24058 (retention)
- **Salesforce:** Field History Tracking / Field Audit Trail (Trailhead, SalesforceBen)
- **django-auditlog** docs (usage, internals); Supabase blog "Postgres Auditing in 150 lines of SQL" and github.com/supabase/supa\_audit (archived)
- **SQLAlchemy 2.0:** ORM events (`before_flush`/`after_flush`), asyncio events, `examples/versioned_history`
- **Source code:** github.com/vinhgiga/obox-builder, branch `dev` @ `dabd36d`
