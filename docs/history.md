# Row history

`obox_builder.contrib.history` records **who changed what, when and from
where** for every tracked row. Entries are append-only facts written inside
the same transaction as the data change, and surface in the admin as a
timeline card on the edit page, a full per-row history page and a global
audit feed.

Design background lives in [ADR-0001](adr/0001-row-history.md).

## Install

```python
INSTALLED_APPS = [
    "obox_builder.contrib.history",
    "obox_builder.contrib.auth",
    "obox_builder.contrib.admin",
    # ...
]
```

Then generate and apply the migration for `history_row_change`:

```bash
python manage.py makemigrations
python manage.py migrate
```

`migrate` also installs the append-only triggers (see
[Guarantees](#guarantees-and-failure-policy)); no extra step is needed.

!!! note
    The app is standalone: without `contrib.admin` you still get capture and
    the read API (`history.service`); the timeline UI requires `contrib.auth`
    (actor resolution, permissions) and `contrib.admin`.

## Track a model

Mirror of `__permissions__`: declare the models you want on the model itself.

```python
class Post(Model, TimestampMixin):
    title: Mapped[str] = mapped_column(sa.String(200))
    body: Mapped[str] = mapped_column(sa.Text, default="")

    __history__ = True  # or a mapping with options
```

```python
class Post(Model):
    __history__ = {
        "exclude": ["internal_note"],  # never recorded
        "relations": ["tags"],  # M2M recorded as id sets (add/remove)
        "retention_days": 90,  # overrides HISTORY_RETENTION_DAYS for this model
    }
```

Field-level switches use the existing `info={"admin": {...}}` namespace:

* `info={"admin": {"secret": True}}` — value is **never stored**; a `redacted`
  marker is recorded instead (so you still see that the field changed).
* `info={"admin": {"history": False}}` — the field is completely invisible to
  history (no marker).

Models you cannot annotate (third-party) can opt in at app-ready time:

```python
from obox_builder.contrib.history import track

track(SomeModel, exclude=["cache_blob"])
```

`HISTORY_TRACK_ALL_ADMIN_MODELS = True` tracks every model registered in the
admin as a shortcut (volume warning: history grows with every save).

Add `VersionedMixin` (`obox_builder.db.mixins`) to models whose edits are
concurrent: every update carries `WHERE version = <loaded>`, the loser's flush
raises `StaleDataError`, entries record `row_version`, and the revert UI can
tell an outdated revert from a real conflict. `W201` warns when a tracked
model has no version column; unversioned models still work (entries store
`row_version = NULL`).

## Actor context

Web requests resolve the actor late-bound from `request.state.user` (the admin
router binds this automatically via `contrib.admin.deps.bind_history`), with
`source="admin_ui"`, IP, user agent and sanitized request id.

CLI commands, jobs and scripts set the context explicitly:

```python
from obox_builder.contrib.history import use

async with use(source="cli", actor_label="import-posts", reason="weekly import"):
    async with session_scope() as db:
        ...  # every tracked change in this block is attributed to import-posts
```

Resolution order at flush time: session binding (`session.info`) → `use(...)`
ContextVar → `system`.

## Reading entries

```python
from obox_builder.contrib.history.service import entry_count, history_entries

entries = await history_entries(db, Post, post.id, before=None, limit=50)  # newest first
total = await entry_count(db, Post, post.id)
```

`before` is a keyset cursor (an entry id), not an offset. Each
`RowChangeEntry` carries `op`, `occurred_at`, `actor_label`, `source`,
`object_repr`, and `diffs` (`FieldDiff(label, old, new)`), plus `redacted`,
`unknown_old` and `truncated` markers.

The stored payload (`changes`) has schema version 1:

```json
{
  "fields": {"body": ["v1", "v2"]},
  "m2m": {"tags": {"add": ["7"], "remove": ["3"]}},
  "redacted": ["secret_token"],
  "masked": {},
  "truncated": {},
  "unknown_old": []
}
```

Fields declared with `mask` store their display-masked values under `masked`
(e.g. `"email": ["a***@example.com"]`), so an erasure/revert never leaks the
raw value.

M2M changes come from the rows that own the association: an entry records the
net `add`/`remove` id lists (sorted, strings) of each declared relation. A
`delete` entry snapshots the collections that were already loaded — history
never triggers a lazy load to read them.

Large values are replaced with `{len, sha256}` under `truncated`
(`HISTORY_MAX_VALUE_BYTES`, default 8 KiB). `unknown_old` marks fields whose
previous value was never loaded (`lazy="raise"`, expired attributes).

## Masking by permission

`entry_view` filters each entry through a `DiffMask` computed from the model's
admin and the viewer:

* Relation changes are always visible (they are opt-in via `relations`).
* Fields hidden from the admin interface (`fields`/`exclude`/`info={"admin":
  {"history": False}}`) are also hidden from every viewer, including
  superusers — history never reveals more than the UI does.
* `redacted` markers are visible; the values behind them never enter the
  database. Secret and `mask` fields stay hidden for everyone.
* Legacy entries whose field names predate the current tracker (renamed or
  removed `__history__` keys) are shown to superusers only.
* The `mask` option adds per-model extra classification on top of `secret`:

```python
class Post(Model):
    __history__ = {"mask": ["author_email"]}
```

Field labels in the UI are resolved at render time from the *current* model
schema, so renames apply to old entries as long as the field still exists.

## Admin UI

* The edit form shows a **History** card (latest 5 entries) with a *Full
  history* link. This includes the bespoke User (`/admin/users/<id>`) and Role
  (`/admin/roles/<id>`) pages, which use their own history routes.
* `GET /admin/<app>/<model>/<pk>/history` shows the full timeline, keyset
  paginated with `?before=<id>`. The auth pages mirror it at
  `/admin/users/<id>/history` and `/admin/roles/<id>/history`.
* Access requires the model's `view` permission **and**
  `history.view_row_change` (`ModelAdmin.has_history_permission`).
* Rows without a registered history (app missing or model untracked) simply
  have no card and no route.
* Right-clicking a list row opens the context menu with a **Row history**
  entry that links to the timeline (the item is hidden for untracked rows or
  users without the permission).
* Admin bulk deletes are executed per row through the ORM, so each deleted
  row gets its own `delete` entry.
* `GET /admin/history` is the **global audit feed** (newest first, 50 per
  page) with filters for model, actor, source, operation and date window;
  pagination is keyset via `?before=<id>`. Results are scoped to the models
  the user can view; superusers see everything.
* Each feed entry links to its row timeline and to `GET
  /admin/history/<change_set_id>`, which groups everything written in the same
  transaction (200 entries max; 404 when nothing is visible to the user).
* The feed header offers **Export CSV / Export NDJSON** buttons that keep the
  current filters (up to 10 000 entries, mask-aware, same permission as the
  feed).
* Timeline entries that can be reverted get an inline **Revert** button; the
  change-set page gets a **Revert change set** button (superusers). Rows
  deleted from the table still show their timeline (`/history` resolves the row
  from history when the row is gone) with a warning banner.
* The sidebar gets an **Audit → History** entry once the app is installed.

## Restore (revert)

Requires the model's op permission plus `history.restore_row_change`
(`POST /admin/<app>/<model>/<pk>/history/<entry_id>/revert`; the change-set
route is superuser-only). A revert is just another tracked change: it writes a
**new** entry with `source="revert"` and `reverts_id` set to the entry it
undoes — the original entry is never mutated.

* `update` → writes the old values back; `create` → deletes the row (needs the
  `delete` permission); `delete` → re-inserts with the recorded primary key
  (`version` restarts at 1 — it is a fresh row, not a resurrection).
* The whole entry reverts **all-or-nothing**, inside a nested transaction. When
  a model is versioned and the current row changed after the recorded entry,
  the page asks for confirmation and `?force=1` overrides the mismatch.
* A revert is refused (409) when it cannot be expressed: M2M changes, masked or
  redacted fields, truncated values, fields invisible to the form, or a
  delete-restore whose primary key is already taken.
* Reverting a change set replays its entries newest-first in one transaction;
  any failure rolls everything back.

## Signals

```python
from obox_builder.contrib.history import signals

signals.pre_record.connect(
    receiver
)  # (sender=RowChange, changes=[PendingChange]); may prune the live list
signals.post_record.connect(receiver)  # (sender=RowChange, rows=[dict]); after the history INSERT
```

`post_record` fires right after the INSERT joins the transaction (not after
commit) so receivers observe the exact write set; treat it as observability,
not as a commit hook. Use it for metrics/shipping (Prometheus, OTel, SIEM).

## Guarantees and failure policy

* **Atomicity** — history rows are inserted in the same transaction as the
  data change; a rollback removes both.
* `HISTORY_FAILURE_MODE = "closed"` (default) — a history failure aborts the
  transaction (the data change rolls back too).
* `HISTORY_FAILURE_MODE = "open"` — the failure is logged and swallowed; on
  backends with savepoints the data change still commits. Check `W202` warns
  about this mode.
* `HISTORY_BULK_POLICY` — bulk `UPDATE`/`DELETE` statements bypass the unit of
  work and cannot be attributed. `"warn"` (default) logs, `"raise"` refuses
  them (`BulkWriteNotTrackedError`), `"ignore"` stays silent. Use
  `history.bulk_update(db, Model, {"field": value}, where=Model.x == 1)` when
  you want one entry per matched row:

```python
from obox_builder.contrib.history import bulk_update

visited = await bulk_update(db, Post, {"status": "archived"}, where=Post.age > 365)
await db.commit()  # each changed row got its own `update` entry
```

* The table itself is **append-only at the database level**: after `migrate`,
  a `post_migrate` receiver installs triggers that reject `UPDATE`/`DELETE`
  (`TRUNCATE` too on PostgreSQL). Maintenance runs (purge) bypass them via
  `SET LOCAL obox.history_maintenance = 'on'`; on SQLite `DELETE` stays
  allowed so local purges keep working. The real boundary is DB roles
  (`REVOKE UPDATE/DELETE`); the trigger is the accident guardrail.
* Secrets never enter the payload: `secret` fields are stored as redacted
  markers. `HISTORY_ENABLED = False` turns off capture globally (listeners
  stay attached but do nothing).

## Retention and exports

```bash
python manage.py history_purge --days 365          # or HISTORY_RETENTION_DAYS
python manage.py history_purge --model blog.post --dry-run
python manage.py history_export --format ndjson --since 2026-01-01 --output audit.ndjson
python manage.py history_export --format csv --actor alice
```

* `history_purge` resolves retention per model (`--days` beats
  `__history__["retention_days"]`, which beats `HISTORY_RETENTION_DAYS`),
  deletes in batches of at most 10 000 rows with a pause between them, and
  logs every batch to `obox.history` (never to the history table). Models
  without retention are reported as skipped.
* `history_export` streams matching entries (keyset paginated) as NDJSON or
  CSV to stdout or `--output`; CSV cells starting with `=`, `+`, `-` or `@`
  are prefixed with `'` to defuse spreadsheet formula injection. Every run
  logs one line to `obox.history.audit`.
* `history_anonymize` erases identifying data **without deleting entries**:

```bash
python manage.py history_anonymize --actor-id 42 --dry-run
python manage.py history_anonymize --actor-id 42      # blank IP/UA, label=anonymous
python manage.py history_anonymize --model blog.post --pk 17
```

  The actor scope blanks `actor_id`/`actor_label`/`ip`/`user_agent`; the object
  scope erases recorded field/mask/M2M values and the `object_repr` (the
  who/when/which-fields skeleton survives, and erased names show up under
  `redacted`). Like purge, it bypasses the append-only trigger for the run
  (`SET LOCAL obox.history_maintenance` on PostgreSQL; the SQLite trigger is
  dropped and recreated) and logs every batch to `obox.history.audit`.
* `history_relabel` rewrites the `model_label` of existing entries when a model
  moves (app renamed, module split) so its timeline survives:

```bash
python manage.py history_relabel --from shop.product --to catalog.product --dry-run
python manage.py history_relabel --from shop.product --to catalog.product
```

  Rewrites in keyset batches (default 10 000 rows), under the same maintenance
  guard as purge, logging each batch to `obox.history.audit`. Labels must match
  `app.model`.

## Reconcile blind spots

Raw SQL, another service or Core bulk DML bypasses ORM capture. Reconcile
replays each row's entries (the create snapshot followed by recorded diffs) and
compares the reconstruction with the live table:

```bash
python manage.py history_reconcile                     # report only (default)
python manage.py history_reconcile --model blog.post   # one label
python manage.py history_reconcile --fix               # append repair entries
```

* Kinds: `missing` (history exists, row gone, last op isn't `delete`),
  `resurrected` (last op is `delete`, row is back), `unexpected` (row without
  any history — blind insert) and `stale` (recorded values differ).
* `--fix` appends a repair entry per drift (`source="db_trigger"`,
  `actor_label="history_reconcile"`), so the timeline shows reality again and
  the next run is clean. Secret/masked fields and values whose plain form is
  unknown (`redacted`/`truncated`) are never compared or repaired.
* Labels that only exist in history (renamed/unregistered models) are reported
  as skipped; use `history_relabel` or `history_anonymize` for them.

## Tamper evidence (seal blocks)

`history_seal` hashes entries into chained blocks; `history_verify` re-derives
every leaf and fails (exit 1) when entries were edited, deleted or blocks
rewritten:

```bash
python manage.py history_seal                          # entries older than 10 min
python manage.py history_seal --lag 3600 --dry-run
python manage.py history_verify
```

Each block stores a Merkle root over its entry hashes plus `prev_block_hash`,
and every seal logs its `block_hash` to `obox.history.audit` — anchor those
lines outside the database (WORM storage, log service, transparency log) to
defeat a malicious DBA. Hashes are never written back into entries, so sealing
does not touch the append-only table.

## Partitioning and DROP-PARTITION retention (PostgreSQL)

Beyond per-row purge, large deployments can partition by month and let
retention `DROP` whole partitions (O(1), no bloat). Converting a live table is
a maintenance-window operation: the command **prints reviewed DDL** instead of
running it:

```bash
python manage.py history_partition --sql --cutoff 2026-12 > convert.sql
psql -d obox -f convert.sql            # one short ACCESS EXCLUSIVE window
python manage.py history_partition --ensure --months 3    # cron: monthly
python manage.py history_partition --drop-before 2025-01  # retention
```

* `--sql` outputs the full conversion: online pre-steps (composite PK
  `(id, occurred_at)`, validated CHECK bound for the old table), then a single
  transaction that renames the old table to `history_row_change_legacy`,
  creates the partitioned parent (same columns, PK, checks, indexes) and
  attaches the legacy table as its first partition. The serial sequence is
  shared (`OWNED BY NONE`) so dropping the legacy partition never breaks
  inserts.
* `--ensure` creates the current + coming months
  (`history_row_change_YYYYMM`) plus `history_row_change_default`, so inserts
  never fail when the monthly job drifts. It never moves rows already sitting
  in `DEFAULT`; create partitions ahead of time.
* `--drop-before YYYY-MM` drops only partitions whose entire range ends at or
  before that month — never `DEFAULT`. Use `--dry-run` to preview names and
  row counts. Partitions dropped this way bypass the trigger by design
  (`DROP TABLE` doesn't fire row triggers); run it with a role that owns the
  partitions.

## Hardening with database roles

The append-only trigger and `obox.history_maintenance` GUC are accident
guardrails; the real security boundary is the role the app connects as:

```sql
-- Runtime role: append + read, never mutate history rows.
GRANT SELECT, INSERT ON history_row_change TO obox_app;
GRANT USAGE, SELECT ON SEQUENCE history_row_change_id_seq TO obox_app;
REVOKE UPDATE, DELETE, TRUNCATE ON history_row_change FROM obox_app;

-- Maintenance role (purge/anonymize/relabel/partition) only.
GRANT UPDATE, DELETE ON history_row_change TO obox_maintenance;
GRANT ALL ON history_row_change, history_seal_block TO obox_maintenance;

-- Auditor role: history only, no business tables.
GRANT SELECT ON history_row_change, history_seal_block TO obox_auditor;
```

Ownership/migrations run as a separate owner role. The CLI maintenance
commands run as the maintenance role and still need the GUC/trigger handling
they already do; `history_verify` only needs `SELECT`.

Model checks (`python manage.py check`):

| Check | Severity | Meaning |
| --- | --- | --- |
| `obox.history.E201` | error | tracked model has a composite primary key |
| `obox.history.E202` | error | `relations` entry is not a many-to-many |
| `obox.history.E203` | error | unknown field name in `fields`/`exclude`/`mask` |
| `obox.history.W201` | warning | tracked model has no `VersionedMixin` (no optimistic locking, no revert-conflict detection) |
| `obox.history.W202` | warning | `HISTORY_FAILURE_MODE='open'` downgrades integrity |

## Limitations (roadmap)

* **Blind spots**: raw SQL, other services and bulk DML outside the ORM are
  not captured as they happen (the bulk guard only sees ORM statements;
  the trigger blocks tampering but does not reconstruct the change).
  `history_reconcile --fix` repairs the trail afterwards, but only for values
  it can still see: secret/masked fields and `redacted`/`truncated` payloads
  are never reconstructed.
* **Tamper evidence is detective, not preventive**: a privileged actor can
  still edit rows; `history_verify` detects it only if block hashes were
  anchored outside the database.
* **Concurrent writers** are protected when the model uses `VersionedMixin`
  (stale flushes raise `StaleDataError`, reverted entries are conflict-checked);
  unversioned models can still record an `old` value that no longer matches at
  commit time (`W201` flags them).
