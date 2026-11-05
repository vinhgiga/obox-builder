# OBOX Record Page and Activity API Specification

**Status:** Draft for review. Only the activity endpoint is detailed below.

## Endpoint overview

Paths have no trailing slash. JSON properties use camelCase; field references
retain their configured snake_case names. `/admin` represents the configured UI prefix.

### JSON API

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/{slug}/{id}` | Retrieve the record and its ETag. |
| PATCH | `/api/{slug}/{id}` | Update permitted fields; require `If-Match` for record-page resources. |
| DELETE | `/api/{slug}/{id}` | Delete the record; require `If-Match` for record-page resources. |
| GET | `/api/{targetSlug}:queryOptions` | Retrieve options for an existing scalar FK editor. |
| GET | `/api/{slug}/{id}/relationships/{field}` | List readable linked-record cards. |
| GET | `/api/{slug}/{id}/activity` | List revisions and comments in one timeline. |
| GET | `/api/{slug}/{id}/comments` | List comments only. |
| POST | `/api/{slug}/{id}/comments` | Create a plain-text comment. |
| GET | `/api/{slug}/{id}/comments/{commentId}` | Retrieve a comment and its ETag. |
| PATCH | `/api/{slug}/{id}/comments/{commentId}` | Edit the caller's comment; require its `If-Match`. |
| DELETE | `/api/{slug}/{id}/comments/{commentId}` | Delete the caller's comment; require its `If-Match`. |

Record CRUD and `:queryOptions` reuse existing routes. ETag/If-Match support is
an extension to the current [API specification](api-spec.md) for record-page
resources. Relationship cards are read-only; no Add record or reassignment endpoints.

### HTML UI

| Method | Path | Description |
| --- | --- | --- |
| GET | `/admin/{slug}/{id}` | Render the record page. |
| GET | `/admin/{slug}/{id}/fields/{field}` | Render a field value. |
| GET | `/admin/{slug}/{id}/fields/{field}/edit` | Render an inline editor with a concurrency token. |
| POST | `/admin/{slug}/{id}/fields/{field}` | Save a field and refresh affected fragments. |
| GET | `/admin/{slug}/{id}/relationships/{field}` | Render linked-record cards. |
| GET | `/admin/{slug}/{id}/activity` | Render the activity timeline. |
| POST | `/admin/{slug}/{id}/comments` | Create a comment. |
| GET | `/admin/{slug}/{id}/comments/{commentId}/edit` | Render the author's comment editor. |
| POST | `/admin/{slug}/{id}/comments/{commentId}/edit` | Save a comment edit. |
| POST | `/admin/{slug}/{id}/comments/{commentId}/delete` | Delete a comment. |

HTML mutations carry concurrency and CSRF tokens. Attachments reuse the Files
plugin's existing endpoints. Threads, mentions, and notifications are deferred.

## Endpoint detail

### GET `/api/{slug}/{id}/activity`

Retrieve the record's revisions and basic comments in newest-first, stable event
order. Requires the Activity plugin and current record retrieve permission,
including row scope and object checks. Revision entries additionally require
`history` permission; comment entries require `comment` permission.

#### Input

**Path parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `slug` | string | Yes | Registered resource's API collection name, such as `people`. |
| `id` | string | Yes | Record primary key in its resource-specific format, URL-encoded as one path segment. |

**Headers**

Use the same-origin session cookie or a supported bearer token when authentication
is required. This read operation requires no CSRF token.

| Name | Required | Description |
| --- | --- | --- |
| `Accept` | No | `application/json`. |
| `Accept-Language` | No | Preferred language for display labels and errors. |
| `X-Request-ID` | No | Correlation ID using the shared API rules; generated when omitted or invalid. |

**Query parameters** — no request body.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `kind` | string | `all` | `all`, `revision`, or `comment`. |
| `limit` | integer >= 0 | Configured default, otherwise unlimited | Maximum returned entries; no framework-enforced maximum unless configured. |
| `cursor` | string | Omitted | Opaque `page.nextCursor` from a previous response; requires a positive `limit`. |

Unknown or repeated query parameters return `422`. Search, offset, custom
ordering, and exact counts are not supported. Cursor requests preserve `kind`
and `limit`; tokens are bound to the caller, record, tenant, and query context.
Forged, invalid, or incompatible tokens return `422 invalid_cursor`.

For `kind=all`, include only permitted kinds; return `403` if neither is
permitted. Explicit `kind=revision` or `kind=comment` requires that kind's permission.
Apply current field and target-record access to historical values. Omit revisions
with no readable changes and omit deleted comments. Hidden IDs, labels, filenames,
and URLs must not appear in values. Permissions are rechecked on every page.

#### Output

**200 OK** — `Content-Type: application/json`.

| Field | Type | Description |
| --- | --- | --- |
| `items` | ActivityItem[] | Visible timeline entries; empty when none are returned. |
| `page.limit` | integer or null | Effective limit; null when unpaginated. |
| `page.offset` | integer | Always `0`; offset requests are unsupported. |
| `page.hasMore` | boolean | Whether additional results or unscanned candidates remain. |
| `page.nextCursor` | string or null | Continuation token, or null when no continuation is available. |
| `page.total` | null | Exact counts are unsupported. |

The response uses the shared `{items, page}` envelope. Every pagination property
above is present. Continue using `nextCursor`, including after an empty page
caused by permission filtering. With `limit=0`, return no items and no cursor;
`hasMore` indicates whether visible entries exist. Without a limit, return all
visible entries with `hasMore=false` and `nextCursor=null`.

**ActivityItem** — shared fields for both entry kinds:

| Field | Type | Description |
| --- | --- | --- |
| `id` | string | Stable activity ID, serialized as a string to preserve integer precision. |
| `kind` | string | `revision` or `comment`; determines the additional payload below. |
| `actor` | object | Safe identity projection: nullable string `id` and string `displayName`. Use a neutral display name when unavailable. |
| `source` | string | Trusted execution origin, such as `ui`, `api`, `job`, or `cli`. |
| `occurredAt` | string | Original event time in ISO 8601 UTC, ending in `Z`. |

**Revision entry** — adds `action` and `changes`, with no `comment` property:

| Field | Type | Description |
| --- | --- | --- |
| `action` | string | `create`, `update`, or `delete`. |
| `changes` | object[] | Nonempty list of readable field differences. |
| `changes[].field` | string | Configured snake_case field identifier, such as `start_date`. |
| `changes[].kind` | string | Value kind used for display, such as `text`, `number`, `reference`, or `files`. |
| `changes[].before` | JSON value | Previous permitted value; absent for creation. |
| `changes[].after` | JSON value | New permitted value; absent for deletion. Explicit null remains a value. |

Values follow the API's field serialization: Decimal and BigInteger values are
strings; dates/times use ISO 8601. Inaccessible related values use a neutral
unavailable representation without revealing their identifiers or metadata.

**Comment entry** — adds `comment`, with no `action` or `changes` properties:

| Field | Type | Description |
| --- | --- | --- |
| `comment.id` | string | Comment UUID, distinct from the activity ID. |
| `comment.author` | object | Safe identity projection with the same shape as `actor`. |
| `comment.body` | string | Current plain-text body. |
| `comment.createdAt` | string | Creation time in ISO 8601 UTC. |
| `comment.editedAt` | string or null | Latest edit time, or null when never edited. |
| `comment.permissions.update` | boolean | Whether the caller may edit this comment. |
| `comment.permissions.delete` | boolean | Whether the caller may delete this comment. |

Editing a comment updates its body and edit time without moving its activity
entry. Capability flags are hints; mutation endpoints enforce authorization.
Refreshing reloads the timeline head. Pagination is not a snapshot or a lossless
change stream; concurrent commits may become visible on refresh.

**Response headers**

| Header | Value |
| --- | --- |
| `Cache-Control` | `private, no-store`. |
| `Vary` | `Cookie, Authorization, Accept-Language`. |
| `X-Request-ID` | Accepted or generated correlation ID. |
| `Content-Language` | Selected response language. |

This endpoint returns no ETag or write token; retrieve the record or comment
through its detail endpoint to obtain that object's validator.

**Errors** use the shared RFC 9457 `application/problem+json` envelope from the
API specification, including `code`, `traceId`, and `errors`.

| Status | Code | Description |
| --- | --- | --- |
| 401 | `unauthenticated` | Authentication is required. |
| 403 | `permission_denied` | Retrieve or requested activity-kind permission is denied. |
| 404 | `not_found` | Activity route unavailable, invalid record ID, missing record, or record outside the authorized row scope. |
| 422 | `unknown_parameter`, `validation_error` | Unknown/repeated parameters, invalid kind/limit, or cursor without a positive limit. |
| 422 | `invalid_cursor` | Invalid token or incompatible caller, record, tenant, or query context. |

#### Example

Read revisions and comments for a person:

```http
GET /api/people/17/activity?kind=all&limit=20 HTTP/1.1
Accept: application/json
X-Request-ID: req-activity-17
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-store
Vary: Cookie, Authorization, Accept-Language
Content-Language: en
X-Request-ID: req-activity-17
```

```json
{
  "items": [
    {
      "id": "381",
      "kind": "revision",
      "actor": {"id": "42", "displayName": "Henry"},
      "source": "ui",
      "occurredAt": "2026-11-05T09:00:00Z",
      "action": "update",
      "changes": [
        {"field": "name", "kind": "text", "before": "Henry Adams", "after": "Henry Adam"}
      ]
    },
    {
      "id": "380",
      "kind": "comment",
      "actor": {"id": "42", "displayName": "Henry"},
      "source": "ui",
      "occurredAt": "2026-11-05T08:55:00Z",
      "comment": {
        "id": "d4fb93cf-798b-4b1f-949d-9ec2c17a5daa",
        "author": {"id": "42", "displayName": "Henry"},
        "body": "Please review the project deadline.",
        "createdAt": "2026-11-05T08:55:00Z",
        "editedAt": null,
        "permissions": {"update": true, "delete": true}
      }
    }
  ],
  "page": {"limit": 20, "offset": 0, "hasMore": false, "nextCursor": null, "total": null}
}
```
