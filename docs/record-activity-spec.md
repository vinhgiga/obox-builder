# Record activity and comments

The optional Activity contrib app records configured ORM changes and exposes a
JSON timeline with plain-text comments. It does not add record CRUD, HTML record
pages or relationship endpoints; use the existing [Model REST API](api-spec.md)
for those operations.

## Setup

Enable `obox.contrib.activity` and configure tracked models through `ACTIVITY`,
or declare `__history_fields__` on a model. The Activity tables must be included
in the host's migrations.

```python
INSTALLED_APPS = ["obox.contrib.auth", "obox.contrib.activity", "app.products"]
ACTIVITY = {"models": {Article: ("title", "status")}}
```

The API model path uses the app label and lowercased model class name, followed
by the record ID. With the default API prefix, an `Article` in app `products`
has a path such as `/api/activity/products.article/42/activity`.

## Routes

All routes require an active identity and the `activity.access_history`
permission. Reads also require `view` access to the parent model and record.
Comment mutations require the model's `comment` action permission and the
authenticated author must own the comment.

| Method | Path | Query/body | Result |
| --- | --- | --- | --- |
| GET | `/api/activity/{model}/{id}/activity` | `kind`, `limit`, `cursor` | Revision and/or comment timeline |
| GET | `/api/activity/{model}/{id}/comments` | `limit`, `cursor` | Comments only |
| GET | `/api/activity/{model}/{id}/comments/{comment_id}` | None | One comment and its `ETag` |
| POST | `/api/activity/{model}/{id}/comments` | `{"body": "..."}` | Creates a comment; returns `201` and `Location` |
| PATCH | `/api/activity/{model}/{id}/comments/{comment_id}` | `{"body": "..."}` and `If-Match` | Updates the author's comment |
| DELETE | `/api/activity/{model}/{id}/comments/{comment_id}` | Empty body and `If-Match` | Soft-deletes the author's comment; returns `204` |

Unsafe comment requests require the host's CSRF token when cookie-authenticated.
Comment creation rejects `If-Match`, `If-None-Match` and
`If-Unmodified-Since`. Update and delete require the strong `ETag` returned by
comment retrieval; missing or stale conditions return precondition errors.
Comment deletion removes the body from reads while retaining the activity record.

## Timeline and paging

The activity route accepts `kind=all`, `kind=revision` or `kind=comment`; the
default is `all`. `limit` defaults to 50 and must be between 1 and 200.
`cursor` is a signed continuation token bound to the model, record, identity,
kind and visible field set. Reusing a cursor with a different context is
rejected. The comments route accepts only `limit` and `cursor`.

Responses use camelCase JSON names. Timeline items have an `id`, `kind`, `actor`,
`source` and `occurredAt`. Revision items also include `action` and `changes`;
comment items include a `comment` projection. Comment objects expose `id`,
`author`, `body`, `createdAt`, `editedAt` and author-only `permissions` hints.
Collection responses contain `items` and a `page` object with `limit`, `offset`,
`hasMore`, `nextCursor` and `total`. Activity and comment routes send
`Cache-Control: private, no-store`.

Tracked ORM inserts, updates and deletes create revision events in the same
transaction. Only configured fields are included, and unreadable related values
are redacted. Bulk writes against tracked models are rejected because they
bypass row-level history capture. Comments are plain text: line endings are
normalized, whitespace-only bodies are rejected, and markup has no special
meaning. Edits update the existing comment event; they do not create revisions.
