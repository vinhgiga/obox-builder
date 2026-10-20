# Row comments

`obox_builder.contrib.comments` adds threaded discussion to any admin row.
Comments live in their own tables, keep a snapshot of the author's label, and
are deliberately **not** written to `history_row_change`: comments are mutable
user content, while history is an audit trail. Design background lives in
ADR-0002.

## Install

```python
INSTALLED_APPS = [
    "obox_builder.contrib.comments",
    "obox_builder.contrib.auth",  # required for authors, mentions and permissions
    "obox_builder.contrib.admin",  # optional: the UI needs it
    # ...
]
```

Generate and apply the migrations for `comments_row_comment` and
`comments_comment_mention`:

```bash
python manage.py makemigrations
python manage.py migrate
```

The comments app is standalone: without `contrib.admin` you still get the
schema, policy and `CommentService`; the page, form card, list badge and
mention autocomplete require `contrib.admin` and `contrib.auth`.

## Enable a model

Mirror of `__history__` and `__permissions__`: opt in on the model itself.

```python
class Ticket(Model, TimestampMixin):
    title: Mapped[str] = mapped_column(sa.String(200))

    __comments__ = True  # or a mapping with options
```

```python
class Ticket(Model, TimestampMixin):
    __comments__ = {
        "max_length": 5_000,  # overrides COMMENTS_MAX_LENGTH (hard cap 20 000)
        "allow_replies": True,  # one-level replies; False disables them
        "edit_window_seconds": 3_600,  # 0 locks editing; omit to inherit the setting
    }
```

Models with a composite primary key cannot be commentable: comments address
rows by one stringified key (`obox.comments.E301`). Invalid declarations are
`obox.comments.E302`; installing comments without `contrib.auth` is
`obox.comments.E303`; a commentable model that is not registered in the admin
gets `obox.comments.W301` (no UI for its comments).

## Permissions

| Permission | Grants |
| --- | --- |
| `comments.view_row_comment` | Read threads on rows the user may view. |
| `comments.add_row_comment` | Post, reply, edit one's own comments, delete one's own comments. |
| `comments.moderate_row_comment` | Delete anyone's comment, see tombstones, resolve/reopen. |
| `comments.export_row_comment` | Reserved for Phase 3. |

Every action additionally requires the target model's `view` permission
(namespaced, e.g. `shop.view_order`). Resolving a thread also needs either
authorship, the model's `change` permission, or moderation.

## Behaviour

* Threads are one level deep. A reply must point at a live root on the same
  target row; replies cannot be resolved.
* Edits update `edited_at` and rewrite mention rows. The edit window is
  `__comments__["edit_window_seconds"]` or `COMMENTS_EDIT_WINDOW_SECONDS`
  (`None` means unlimited).
* Deletes are tombstones: the body is retained for moderators and exports, and
  live comments always keep a body. `COMMENTS_DELETED_RETENTION_DAYS` is
  reserved for the Phase 2 purge.
* Writes use a savepoint. `pre_comment`/`post_comment` fire with `action`
  (`add`, `edit`, `delete`, `resolve`, `reopen`), `target`, `comment` and
  `actor`; an exception rolls the operation back.
* A stale edit (optimistic `version` mismatch) becomes `CommentConflict`.
* `COMMENTS_RATE_LIMIT_PER_MINUTE` limits each author's newest comments
  (`0` disables it). The service raises `CommentRateLimited`; the admin
  returns `429` with `Retry-After`.
* `idempotency_key` is unique per author. Replaying the same key on the same
  target returns the existing comment; reusing it on another target is a
  conflict.

## Mentions

Compose mentions in the body as `@[u:<id>]`. The server requires every
mentioned user to be active and to hold both the target model's `view`
permission and `comments.view_row_comment`; otherwise the write fails with a
validation error. The autocomplete endpoint returns only eligible users:

```text
GET /admin/comments/mentionable?model=shop.order&q=an
```

It requires `comments.add_row_comment`, the model's view permission, and at
least two characters. Rendering resolves a token through the mention table,
so renamed users keep their new display name; unknown tokens render as
`@unknown`.

## Rendering

`obox_builder.contrib.comments.render.render_body()` renders plain text safely:

* every text node is escaped with markupsafe;
* only `http://`, `https://` and `mailto:` URLs become links
  (`rel="noopener noreferrer nofollow ugc"`);
* newlines become `<br>`;
* mention tokens become `@Display Name` spans.

Templates must not apply `safe` to comment data; render the returned
`Markup` as-is.

## Admin UI and routes

Routes live under each `ModelAdmin`'s path (the examples assume the default
`/admin/<app>/<model>`):

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/{pk}/comments` | Standalone thread page (keyset paginated via `?before=`). |
| `POST` | `/{pk}/comments` | Add a root comment or a reply (`parent_id`). |
| `POST` | `/{pk}/comments/{comment_id}/edit` | Edit a comment. |
| `POST` | `/{pk}/comments/{comment_id}/delete` | Tombstone a comment. |
| `POST` | `/{pk}/comments/{comment_id}/resolve` | Resolve a root thread. |
| `POST` | `/{pk}/comments/{comment_id}/reopen` | Reopen a root thread. |

All POSTs require a CSRF token and redirect with `303`. The edit page gets a
**Comments** card with the latest threads; the list page gets a comment
badge, a comments link and a context-menu item. If the target row was deleted
but comments remain, the page is read-only and uses the snapshot
`object_repr`.

## Service API

Use the service from API handlers, commands or jobs:

```python
from obox_builder.contrib.comments.registry import get_options
from obox_builder.contrib.comments.service import CommentService
from obox_builder.contrib.comments.targets import CommentTarget

target = CommentTarget(model_label="shop.order", object_pk="42", object_repr="Order #42")
service = CommentService(db, policy, source="api")

comment = await service.add(target, get_options(Order), {"body": "Looking into it."})
await service.edit(target, get_options(Order), comment.id, {"body": "Fixed."})
await service.delete(target, comment.id)
await service.set_resolved(target, comment.id, resolved=True)
```

Reads return permission-aware views:

```python
from obox_builder.contrib.comments.queries import comment_count, latest_threads, thread_page

page = await thread_page(db, target, policy, options, before=None, limit=50)
total = await comment_count(db, target)
threads = await latest_threads(db, target, policy, limit=5)
```

`counts_for_page()` powers the list badges with one grouped query.

## Settings

| Setting | Default | Purpose |
| --- | --- | --- |
| `COMMENTS_ENABLED` | `True` | Master switch. |
| `COMMENTS_MAX_LENGTH` | `10_000` | Default body limit (database hard cap `20_000`). |
| `COMMENTS_MAX_MENTIONS` | `20` | Maximum distinct mentions per comment. |
| `COMMENTS_EDIT_WINDOW_SECONDS` | `None` | Default edit window; `None` is unlimited. |
| `COMMENTS_RATE_LIMIT_PER_MINUTE` | `20` | Author rate limit; `0` disables it. |
| `COMMENTS_DELETED_RETENTION_DAYS` | `30` | Reserved for the Phase 2 purge. |
| `COMMENTS_NOTIFICATION_RETENTION_DAYS` | `90` | Reserved for Phase 2 notifications. |
| `COMMENTS_AUTO_SUBSCRIBE_MODE` | `"all"` | Reserved for Phase 2 subscriptions. |
| `COMMENTS_NOTIFY_EMAIL_INCLUDE_BODY` | `False` | Reserved for Phase 2 email. |
| `COMMENTS_OUTBOX_MAX_ATTEMPTS` | `8` | Reserved for the Phase 2 outbox. |

## Phase 1 scope

Included: one-level threads, mentions, edit/soft-delete/resolve, policy and
permissions, safe renderer, standalone page, form card, list badge, context
menu, mentions autocomplete, validation, rate limiting and idempotency.

Not yet included: notifications, subscriptions, edit revisions, polling/real
time, exports and purge jobs.
