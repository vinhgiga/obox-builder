# Row history

`obox_builder.contrib.history` records **who changed what, when and from
where** for every tracked row. Entries are append-only facts written inside
the same transaction as the data change, and surface in the admin as a
timeline card on the edit page plus a full per-row history page.

Design background lives in [ADR-0001](adr/0001-row-history.md) (Vietnamese).

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
        "relations": ["tags"],  # M2M recorded as id sets (Phase 2)
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
  "m2m": {},
  "redacted": ["secret_token"],
  "truncated": {},
  "unknown_old": []
}
```

Large values are replaced with `{len, sha256}` under `truncated`
(`HISTORY_MAX_VALUE_BYTES`, default 8 KiB). `unknown_old` marks fields whose
previous value was never loaded (`lazy="raise"`, expired attributes).

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
  them (`BulkWriteNotTrackedError`), `"ignore"` stays silent.
* Secrets never enter the payload: `secret` fields are stored as redacted
  markers. `HISTORY_ENABLED = False` turns off capture globally (listeners
  stay attached but do nothing).

Model checks (`python manage.py check`):

| Check | Severity | Meaning |
| --- | --- | --- |
| `obox.history.E201` | error | tracked model has a composite primary key |
| `obox.history.E202` | error | `relations` entry is not a many-to-many |
| `obox.history.E203` | error | unknown field name in `fields`/`exclude`/`mask` |
| `obox.history.W202` | warning | `HISTORY_FAILURE_MODE='open'` downgrades integrity |

## Limitations (roadmap)

* **M2M** relationship sets are declared (`relations`) but not yet diffed —
  Phase 2, together with a global feed and masking by permission.
* **Blind spots**: raw SQL, other services, and bulk DML outside the ORM are
  not captured (the bulk guard only sees ORM statements). DB triggers arrive
  in Phase 4 as a safety net.
* **Retention**: `HISTORY_RETENTION_DAYS` is reserved; purge tooling
  (`history_purge`) ships with Phase 2. Partitioning is Phase 4 (Postgres).
* **Concurrent writers** can produce an `old` value that no longer matches at
  commit time unless the model uses optimistic versioning.
