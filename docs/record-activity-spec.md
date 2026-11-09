# OBOX Record Page and Activity API Specification

**Status:** Implemented for review. Every endpoint in the overview has an Input,
Output, and Example section. Record CRUD and `:queryOptions` reuse the core
services. Inline editing requires SQLAlchemy record versioning; activity and
comments require the Activity plugin. Existing host databases must migrate
`obox_comment.version` before using versioned comments.

## Endpoint overview

Paths have no trailing slash. JSON properties and revision field references use
camelCase; path field identifiers retain their configured snake_case names.
`/admin` represents the configured UI prefix.

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
resources. Record-page relationship cards remain read-only. Opt-in collection
membership write routes are documented separately in the
[API specification](api-spec.md#collection-relationships).

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

HTML updates and deletes carry concurrency and CSRF tokens; comment creation
requires CSRF but does not require a record version. Attachments reuse the Files
plugin's existing endpoints. Threads, mentions, and notifications are deferred.

## Shared contract

These rules apply to every detail below unless an endpoint explicitly overrides
them. Examples use a versioned `people` resource, caller `42`, record `17`, and
the configured UI prefix `/admin`. Example ETags and cursors are illustrative
opaque values; clients must use values returned by the server.

### Paths, authentication, and permissions

| Parameter | Type | Meaning |
| --- | --- | --- |
| `slug` | string | Registered resource collection name. JSON routes use the API collection name; HTML routes use the configured UI resource slug. They need not be identical. |
| `targetSlug` | string | Referenced resource's API collection name. |
| `id` | string | Record primary key in its resource-specific format, encoded as one path segment. |
| `field` | string | Configured snake_case field or relationship identifier, not a JSON property alias. |
| `commentId` | UUID string | Comment identifier, scoped to the parent record and current tenant. |

Use a same-origin session cookie or host-supported bearer authentication.
Bearer authentication requires the corresponding host integration/plugin.
HTML UI examples use a session. No endpoint accepts identity or tenant overrides
from the request. `actor`, comment author, event time, and execution `source`
come from trusted server context.

Parent-record `retrieve` permission, its row scope, and object checks apply to
every nested route. A missing record, invalid record ID, or record outside
scope returns `404 not_found`. Operation/object denial after a scoped lookup
returns `403 permission_denied`. A comment belonging to another parent or
tenant returns `404`, even if its UUID is known. Disabled resource operations,
unavailable plugin routes, and unconfigured fields/relationships return `404`.

The Activity plugin is required for activity and comment routes. `history`
controls revisions; `comment` controls reading and creating comments. Both
actions apply their own row scope and object checks in addition to retrieve.
An active authenticated author with current comment access may update/delete
their own comments. Record update/delete permission does not confer comment
ownership; no moderator override is proposed here.

### JSON requests and responses

| Header | Rule |
| --- | --- |
| `Accept` | Optional; use `application/json` for JSON routes. |
| `Content-Type` | Required for a JSON body: `application/json`, optionally with UTF-8 charset. |
| `Accept-Language` | Optional; selects translated labels and error messages. |
| `X-Request-ID` | Optional; up to 128 characters from `[A-Za-z0-9._-]`. Generated when omitted or invalid; echoed in the response. |
| `X-CSRF-Token` | Required on unsafe JSON requests carrying cookies, following the current shared API contract. |

CSRF and same-origin checks follow [the API specification](api-spec.md).
GET requests require no CSRF token. A token-authenticated request without
cookies requires no CSRF token. This draft does not change the existing rule
that unsafe requests carrying cookies undergo CSRF checks.

JSON object properties use camelCase, including record fields. Path `field` uses configured snake_case identifiers.
Revision `changes[].field` uses the public camelCase alias from the core API;
selector query references follow the existing shared query contract. Arbitrary JSON-column dictionary keys retain their
original casing. Record/FK identifiers follow the shared serialization rules;
examples use string IDs. Decimal and BigInteger values are strings, and date/
time values use the shared ISO 8601 formats. Activity/comment timestamps are UTC
with a `Z` suffix. Absent fields and explicit null have different meanings.

Reject unknown JSON properties and snake_case aliases for named request
properties. Except for the documented `:queryOptions` filter keys, reject
unknown query parameters and repeated single-value controls with `422`.
An endpoint declaring no query parameters accepts an empty query string only.
No GET or DELETE below takes a request body.

Responses include `X-Request-ID`, `Content-Language`, and
`Vary: Cookie, Authorization, Accept-Language`, including errors. Record and
selector JSON responses use `Cache-Control: private, no-cache`; nested
relationship, activity, and comment responses use `private, no-store`.
No endpoint below promises conditional GET/`304` handling. ETags on detail
responses are write validators; `If-None-Match` and `If-Unmodified-Since` are
outside this draft's contract.

### Concurrency and transaction boundaries

Record-page inline editing requires a resource backed by SQLAlchemy
`version_id_col`; configuring editable record-page fields without durable
versioning must fail configuration validation;
unversioned resources outside this workflow retain the existing CRUD contract.
Comments require an independently maintained durable version for safe writes.
Comment versions and record versions are separate: adding/editing/deleting a
comment does not change the parent record ETag.

`GET` record/comment detail returns a strong, quoted `ETag`. JSON PATCH/DELETE
requires `If-Match` for that same object. Comparison and the write must be
atomic; a concurrent write cannot pass the same old version twice. Missing
conditions return `428 precondition_required`, stale or weak-only tags return
`412 precondition_failed`, and malformed conditions return
`422 invalid_precondition`. Accept a valid entity-tag list or `*` following
HTTP semantics. `*` checks existence only; the UI always sends a specific strong
tag so stale edits are detected. Never reuse a record ETag for a comment or
reuse one comment's ETag for another comment.

All mutation authorization and field/target checks run again at save time.
Resource hooks, persistence, and revision/comment activity writes share one
transaction: a failure rolls back all of them. Record updates create a revision
only when recorded field values actually change. Rejected writes and no-op
comment edits do not create activity entries. Comment creation creates one
timeline entry; edits update that entry's current body without reordering it;
deletion removes it from reads. Comment edits/deletes do not create record
revisions. After parent deletion, nested routes return `404`; this API provides
no deleted-record history browser or restore operation.

No idempotency-key deduplication is introduced. A repeated comment POST may
create another comment. After an uncertain PATCH/DELETE result, refetch before
deciding whether to retry; never replace a stale token and silently resubmit.

### Pagination and shared objects

Activity, comment, and relationship collections use the following `Page` shape:

| Field | Type | Meaning |
| --- | --- | --- |
| `limit` | integer or null | Effective limit; null means unpaginated. No enforced maximum unless configured. |
| `offset` | integer | Always `0` for these cursor-only collections. |
| `hasMore` | boolean | More results or unscanned candidates remain. |
| `nextCursor` | string or null | Opaque continuation token, or null. |
| `total` | null | Exact counts are not supported for these collections. |

`limit` is a nonnegative integer, defaulting to the configured default or
unlimited. `cursor` requires a positive limit and unchanged query context.
Tokens bind to the caller, tenant, parent record, collection, and relevant
query/permission context. Reject invalid/incompatible tokens with
`422 invalid_cursor`. Access is rechecked on each page; this is not a snapshot.
Continue using `nextCursor` even when filtering leaves an empty page.
`limit=0` returns no items/cursor and indicates whether readable results exist.
Unpaginated responses have `hasMore=false` and `nextCursor=null`.
`:queryOptions` retains its existing offset/count/cursor rules instead.

`Identity` is `{id: string | null, displayName: string}`. Use a safe neutral
identity when unavailable; do not expose a hidden principal ID through labels.
`CommentRead` is the object used in `ActivityItem.comment` below:

| Field | Type | Meaning |
| --- | --- | --- |
| `id` | UUID string | Comment ID, distinct from the activity ID. |
| `author` | Identity | Safe author projection. |
| `body` | string | Current nonempty plain-text comment. |
| `createdAt` | UTC timestamp | Original creation time. |
| `editedAt` | UTC timestamp or null | Latest actual body change; null before the first edit. |
| `permissions` | object | Required `update` and `delete` booleans; hints only, never authorization tokens. |

Comment input is exactly `{body: string}`. Normalize CRLF/CR line endings to LF,
reject empty or whitespace-only content, and otherwise preserve the text and
line breaks. Null, numbers, arrays, and unknown properties are invalid. There
is no framework-imposed body-length cap; an explicitly configured field
validator may impose one. Markdown, HTML, mentions, file attachment properties,
and reply/thread identifiers have no special meaning. Render all user text
with HTML escaping; linkification must never introduce executable URLs.

### Errors

JSON failures use RFC 9457 `application/problem+json` with required `type`,
`title`, `status`, `code`, `detail`, `instance`, `traceId`, and `errors`.
`instance` is the request path and `traceId` matches `X-Request-ID`. Field
errors use public JSON aliases and JSON pointers such as `#/body`; query
errors use the shared query-pointer format. Never return hidden values,
tokens, SQL, or stack traces in error details.

| Status | Code | Meaning |
| --- | --- | --- |
| 400 | `malformed_json` | JSON body cannot be parsed. |
| 401 | `unauthenticated` | Authentication is required. |
| 403 | `permission_denied` | Operation, object, field, or ownership check denied. |
| 403 | `csrf_invalid`, `origin_rejected` | CSRF or same-origin check failed. |
| 404 | `not_found` | Route/object unavailable or outside its authorized scope. |
| 406 | `not_acceptable` | Requested response media type is unsupported. |
| 409 | `conflict` | A database/business constraint prevents the mutation. |
| 412 | `precondition_failed` | Supplied write validator no longer matches. |
| 415 | `unsupported_media_type` | Request body media type is unsupported. |
| 422 | `validation_error`, `unknown_parameter` | Invalid body/query or unsupported parameter. |
| 422 | `invalid_cursor`, `invalid_precondition` | Invalid continuation or conditional header. |
| 428 | `precondition_required` | Required object validator is missing. |
| 500 | `internal_error` | Unexpected error with no internal exception details. |

For example, a stale comment edit returns:

```http
HTTP/1.1 412 Precondition Failed
Content-Type: application/problem+json
Cache-Control: private, no-store
X-Request-ID: req-comment-edit-17

{
  "type": "urn:obox:problem:precondition_failed",
  "title": "Precondition Failed",
  "status": 412,
  "code": "precondition_failed",
  "detail": "The comment has changed. Reload it before editing.",
  "instance": "/api/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa",
  "traceId": "req-comment-edit-17",
  "errors": []
}
```

### HTML transport and fragments

HTML routes return `text/html; charset=utf-8`, use `private, no-store`, and
share the identity, locale, correlation, and authorization rules above. Their
responses also vary on `X-Obox-Fragment` wherever it changes the representation.
The proposed `X-Obox-Fragment: true` request header distinguishes progressive
form submissions from ordinary navigation; it is a transport hint, not an
access control. Fragment routes work with the project's own JavaScript and
require no particular fragment library.

| DOM root | Contents / replacement rule |
| --- | --- |
| `#record-content` | Fields, relationship sections, and authorized activity section for one record; replaces the whole content after a successful inline field save. |
| `#record-field-{field}` | One displayed field or its editor; either representation replaces the same root. |
| `#record-relationship-{field}` | One read-only related-card collection. |
| `#record-activity` | Timeline, pagination links, and permitted comment controls. |
| `#record-comment-{commentId}` | An individual comment or its author editor. |

HTML GET subroutes return their documented fragment without requiring the
fragment header. The page route returns a full document. On POST success,
fragment submissions receive `200` plus the documented replacement root;
ordinary form submissions receive `303 See Other` to the parent record page,
with `#activity` for comment operations. Redirect targets are server-generated.
Every field save refreshes all record-content regions so dependent labels,
computed values, permissions, concurrency tokens, and timeline remain current.
Comment saves refresh the activity head, discarding its previous pagination
position, without changing unrelated field editors or their record version.

Forms use `application/x-www-form-urlencoded`, with snake_case control names.
Every POST requires `csrf_token`; field saves and comment edits/deletes also
require `if_match`, containing that object's quoted ETag. Inline HTML forms
accept one specific strong tag, not `*` or a tag list. Missing `if_match` returns
`428`; malformed values return `422`; stale values return `412`. Do not accept
two different validators from the body and headers: these HTML routes use only
the form control, and reject conditional headers as unsupported input.
Reject unexpected or repeated scalar form controls.

On `422`, return the submitted form/editor with accessible field errors and
preserved user input. On `412`, preserve the unsaved input, show a conflict
message and an explicit reload action, and do not silently replace the
submitted token with a current token. Fragment clients must render these error
bodies even though their status is non-2xx. Ordinary submissions render the
same form in a full page on failure. Other HTML failures use an escaped error
fragment/full page with the real HTTP status, including `401`, `403`, `404`,
`409`, and `428`; they never return a successful replacement or JSON problem
body. `406`/`415` apply to unsupported HTML response/form media types.

## Endpoint detail

### GET `/api/{slug}/{id}`

Retrieve the currently readable record and its write validator. Requires the
resource API retrieve operation and current retrieve access.

#### Input

- **Path:** required `slug` and `id` from the shared path contract.
- **Headers:** shared JSON read headers/authentication. No CSRF token.
- **Query/body:** none; `select` and `expand` are not supported here.

#### Output

**200 OK** — one record object without an envelope. Include only readable API
fields, with camelCase aliases and the primary key. Deny with `403` if its
primary key cannot be exposed. Include a strong `ETag` for a versioned
record-page resource and shared record-response headers. The response is the
record projection, not the field UI layout, relationship cards, or timeline.

Errors: shared `401`, `403`, `404`, `406`, `422`, and `500` cases. A subsequent
GET can change the readable projection or ETag when access/record state changes.

#### Example

```http
GET /api/people/17 HTTP/1.1
Accept: application/json

HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-cache
ETag: "person-17-v4"

{"id":"17","name":"Henry Adams","startDate":"2026-11-01","projectId":"3"}
```

### PATCH `/api/{slug}/{id}`

Update permitted record fields using the existing field-assignment PATCH
contract, extended with a required record precondition for record-page resources.
Requires current update and retrieve access for this workflow.

#### Input

- **Path:** required `slug`, `id`.
- **Headers:** shared JSON mutation headers, `Content-Type: application/json`,
  and required `If-Match` from record detail.
- **Query:** none.
- **Body:** object containing permitted camelCase record properties, without a wrapper.

Omitted properties retain their values; explicit null clears a nullable field;
an empty string stays an empty string when valid. JSON-column values replace
the entire column value. `{}` runs the existing update flow, including trusted
presets/hooks, but produces no revision if recorded values do not change.
Primary keys, version counters, read-only values, and server-owned presets are
not client-writable. Reject forbidden assignments rather than dropping them.

Set a scalar FK through its JSON property, such as `projectId`. Recheck target
retrieve access and row scope on every supplied non-null reference. Missing
and inaccessible targets produce the same permission failure. Null clears a
nullable FK; it does not delete the target. Nested creates, collection writes,
JSON Patch, and upsert are outside this contract.

#### Output

**200 OK** — the current readable record, with the same shape as GET record
detail, plus its resulting `ETag`. All assignments and the revision commit
atomically. No creation `Location` is returned. No-op assignments can preserve
the validator if neither persistence nor the representation changes.

Errors: shared mutation failures, including `403` for dynamically forbidden
fields/targets, `409 conflict`, `412`, `422`, and `428`. Static unknown or
excluded properties return `422` with `unknown_field` field errors. No partial
field save or revision is committed on failure.

#### Example

```http
PATCH /api/people/17 HTTP/1.1
Content-Type: application/json
If-Match: "person-17-v4"
X-CSRF-Token: csrf-example

{"name":"Henry Adam","projectId":"4"}

HTTP/1.1 200 OK
Content-Type: application/json
ETag: "person-17-v5"

{"id":"17","name":"Henry Adam","startDate":"2026-11-01","projectId":"4"}
```

### DELETE `/api/{slug}/{id}`

Delete a record through the existing resource service, with its record-page
write precondition. Requires current delete and retrieve access in this workflow.

#### Input

- **Path:** required `slug`, `id`.
- **Headers:** shared JSON mutation headers and required record `If-Match`.
- **Query/body:** none; a nonempty body returns `422 validation_error`.

No client flags can enable cascade deletion, bypass constraints, or choose a
different deletion policy. Existing configured ORM/database behavior applies.

#### Output

**204 No Content** — empty body, no `ETag` or `Location`. Deletion, configured
cascades, and any recorded deletion event share the transaction. A constraint
failure returns `409` and leaves the record and activity unchanged. A retained
deletion event does not make the deleted record's timeline publicly retrievable.

Errors: shared `401`, `403`, `404`, `409`, `412`, `422`, and `428` cases.
A repeated delete returns `404` after the first deletion has committed.

#### Example

```http
DELETE /api/people/17 HTTP/1.1
Accept: application/json
If-Match: "person-17-v5"
X-CSRF-Token: csrf-example

HTTP/1.1 204 No Content
Cache-Control: private, no-cache
```

### GET `/api/{targetSlug}:queryOptions`

Retrieve options for a scalar FK editor. This reuses the complete existing
[:queryOptions contract](api-spec.md#get-apislugqueryoptions); there is no
record-page-specific selector endpoint or implicit source context.

#### Input

- **Path:** required `targetSlug`, identifying the referenced API resource.
- **Headers:** shared JSON read headers/authentication; no CSRF token.
- **Body:** none.

| Query | Type / default | Meaning |
| --- | --- | --- |
| `search` | string / omitted | Literal substring search over configured readable search fields. |
| `<field>`, `and`, `or`, `not.and`, `not.or` | filter expressions / omitted | Shared GET filter grammar and allowlists; field references remain snake_case. |
| `order` | string / resource default | Shared permitted ordering, including a primary-key tie-breaker. |
| `limit` | integer >= 0 / configured or unlimited | Candidate page size; configured optional maximum applies. |
| `offset` | integer >= 0 / `0` | Candidates skipped; cannot be combined with cursor. |
| `cursor` | opaque string / omitted | Shared query cursor; requires explicit limit. |
| `count` | `exact` / omitted | Request the SQL-scoped candidate count. |

Both target list and retrieve operations/permissions must be available. Apply
both row scopes and per-object retrieve checks. No `sourceSlug`, `field`, `ids`,
`select`, or `expand` parameter exists. Resolve selected values through an
allowed primary-key filter, such as `id=in.(3,4)`. Returned options do not
authorize a later FK assignment; save rechecks source and target permissions.

#### Output

**200 OK** — `{items: [{id, label}], page}`. IDs follow target identifier
serialization. Labels use readable configured label fields, falling back to
permitted primary-key text; labels are plain text. No extra target fields.
`page` has `limit`, `offset`, `hasMore`, `nextCursor`, and `total`; `total` is
null unless `count=exact`. Counts/offsets describe SQL-scoped candidates before
per-object omissions. Follow pagination metadata even after an empty page;
use offset continuation when a cursor cannot be provided.

Errors: shared read failures plus existing `422 unknown_field`,
`unsupported_operator` or `invalid_cursor`.
An unreadable target primary key returns `403`.

#### Example

```http
GET /api/projects:queryOptions?search=Launch&limit=10 HTTP/1.1
Accept: application/json

HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-cache

{
  "items":[{"id":"3","label":"Launch website"},{"id":"4","label":"Launch mobile"}],
  "page":{"limit":10,"offset":0,"hasMore":false,"nextCursor":null,"total":null}
}
```

### GET `/api/{slug}/{id}/relationships/{field}`

List cards for one configured collection relationship of the current record.
This is a read-only projection, not an arbitrary relationship traversal API.

#### Input

- **Path:** required `slug`, `id`, and configured relationship `field`, such as `tasks`.
- **Headers:** shared JSON read headers/authentication; no CSRF token.
- **Query:** optional `limit` and `cursor`, using the shared cursor-only rules.
- **Body:** none. Search, custom order, offset, exact count, and expansion are unsupported.

Require parent retrieve access and target list/retrieve access. Apply the
configured relationship join and target row scopes before pagination, then
per-object and field checks. Never accept a client-supplied join, parent FK,
target slug, or filter capable of broadening the relationship. Only explicitly
configured collection relationships are exposed. Scalar FKs use the field
display/editor routes and `:queryOptions` instead.

#### Output

**200 OK** — `Content-Type: application/json`, `private, no-store`.

| Field | Type | Meaning |
| --- | --- | --- |
| `field` | string | Configured snake_case relationship identifier. |
| `targetSlug` | string | Target API collection name. |
| `items` | object[] | Readable related-record cards. |
| `items[].id` | scalar | Target primary key with shared identifier serialization. |
| `items[].label` | string | Safe plain-text label with readable-ID fallback. |
| `items[].url` | string or null | Server-generated target detail UI URL when available and permitted. |
| `items[].values` | object | Configured readable card fields with JSON aliases; hidden fields are omitted. |
| `page` | Page | Shared cursor-only pagination; no exact total. |

Order by the target primary key ascending, using its native database type.
Deduplicate target IDs before pagination when joins produce duplicates. Omit
cards whose primary key cannot be exposed. Inaccessible related records and
their metadata never appear; an authorized relationship with no readable
children returns `items: []`. No ETag or mutation controls are returned.

Errors: shared read failures, `403` for denied target operations, `404` for
unconfigured relationships, and `422` for query/cursor errors. Permission
denial is not represented as a misleading empty successful collection.

#### Example

```http
GET /api/people/17/relationships/tasks?limit=10 HTTP/1.1
Accept: application/json

HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-store

{
  "field":"tasks",
  "targetSlug":"tasks",
  "items":[
    {"id":"91","label":"Review launch plan","url":"/admin/tasks/91","values":{"status":"open","dueDate":"2026-11-10"}}
  ],
  "page":{"limit":10,"offset":0,"hasMore":false,"nextCursor":null,"total":null}
}
```

### GET `/api/{slug}/{id}/activity`

Retrieve the record's revisions and basic comments in newest-first, stable event
order: activity ID descending, not client timestamps or comment edit times.
Requires the Activity plugin and current record retrieve permission,
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
| `changes[].field` | string | Public JSON field alias, such as `startDate`. |
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

### GET `/api/{slug}/{id}/comments`

List current comments without revision entries. Requires the Activity plugin,
parent retrieve access, and current comment permission/scope/object checks.

#### Input

- **Path:** required `slug`, `id`.
- **Headers:** shared JSON read headers/authentication; no CSRF token.
- **Query:** optional `limit` and `cursor` using shared cursor-only pagination.
- **Body:** none. `kind`, search, offset, custom order, and count are unsupported.

#### Output

**200 OK** — `{items: CommentRead[], page: Page}`, with `private, no-store`.
Each item is the comment object itself, without an activity wrapper. Order by
the original comment activity ID descending, matching the comment-only
timeline. Edits do not move entries; deleted comments are omitted. Permission
flags reflect the current caller, including ownership. No ETag is returned for
the collection or its entries; retrieve a comment detail before a JSON write.

Errors: shared `401`, `403`, `404`, `406`, and `422` read/cursor cases. No
comment permission returns `403`, rather than an empty list.

#### Example

```http
GET /api/people/17/comments?limit=20 HTTP/1.1
Accept: application/json

HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-store

{
  "items":[{
    "id":"d4fb93cf-798b-4b1f-949d-9ec2c17a5daa",
    "author":{"id":"42","displayName":"Henry"},
    "body":"Please review the project deadline.",
    "createdAt":"2026-11-05T08:55:00Z",
    "editedAt":null,
    "permissions":{"update":true,"delete":true}
  }],
  "page":{"limit":20,"offset":0,"hasMore":false,"nextCursor":null,"total":null}
}
```

### POST `/api/{slug}/{id}/comments`

Create a plain-text comment attributed to the current authenticated principal.
Requires current parent retrieve/comment access, not record update permission.

#### Input

- **Path:** required `slug`, `id`.
- **Headers:** shared JSON mutation headers and `Content-Type: application/json`.
- **Query:** none.
- **Body:** exactly `{body: string}`, with the shared plain-text validation.

No record/comment `If-Match` is required for creation. Reject supplied
conditional headers with `422 unsupported_precondition` rather than implying
that a parent precondition was checked. Author, timestamps, permissions,
comment ID, and source are server-controlled and cannot be supplied.

#### Output

**201 Created** — one `CommentRead`, with `ETag` for the new comment,
`Location: /api/{slug}/{id}/comments/{commentId}`, and `private, no-store`.
The location includes any configured root/API prefix and an encoded ID.
Persist the comment and its single activity entry atomically. Do not return the
activity ID or a changed record ETag. A retry is a new creation request.

Errors: shared mutation failures, especially `401` for an unauthenticated
author, `403` for comment denial, `404` for an unavailable parent, and `422`
for empty/non-string bodies or extra properties. No comment/event survives a
failed transaction.

#### Example

```http
POST /api/people/17/comments HTTP/1.1
Content-Type: application/json
X-CSRF-Token: csrf-example

{"body":"Please review the project deadline."}

HTTP/1.1 201 Created
Content-Type: application/json
Cache-Control: private, no-store
Location: /api/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa
ETag: "comment-d4fb93cf-v1"

{
  "id":"d4fb93cf-798b-4b1f-949d-9ec2c17a5daa",
  "author":{"id":"42","displayName":"Henry"},
  "body":"Please review the project deadline.",
  "createdAt":"2026-11-05T08:55:00Z",
  "editedAt":null,
  "permissions":{"update":true,"delete":true}
}
```

### GET `/api/{slug}/{id}/comments/{commentId}`

Retrieve a currently visible comment and its own validator. Any caller with
parent retrieve/comment access may read it; authorship is only required for writes.

#### Input

- **Path:** required `slug`, `id`, `commentId`.
- **Headers:** shared JSON read headers/authentication; no CSRF token.
- **Query/body:** none.

Invalid UUIDs, deleted comments, missing comments, and comments belonging to a
different parent or tenant return the same `404 not_found`.

#### Output

**200 OK** — one `CommentRead`, strong comment `ETag`, and `private, no-store`.
This is the authoritative JSON source for a comment write token. Another
author's readable comment has `permissions.update=false` and
`permissions.delete=false`; it is still a successful read.

Errors: shared `401`, `403`, `404`, `406`, and `422` read cases. No revision
history of the comment's earlier bodies is returned.

#### Example

```http
GET /api/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa HTTP/1.1
Accept: application/json

HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-store
ETag: "comment-d4fb93cf-v1"

{
  "id":"d4fb93cf-798b-4b1f-949d-9ec2c17a5daa",
  "author":{"id":"42","displayName":"Henry"},
  "body":"Please review the project deadline.",
  "createdAt":"2026-11-05T08:55:00Z",
  "editedAt":null,
  "permissions":{"update":true,"delete":true}
}
```

### PATCH `/api/{slug}/{id}/comments/{commentId}`

Replace the caller's own comment body. Requires an active authenticated author,
current parent retrieve/comment access, and the comment's current validator.

#### Input

- **Path:** required `slug`, `id`, `commentId`.
- **Headers:** shared JSON mutation headers, `Content-Type: application/json`,
  and required comment `If-Match`.
- **Query:** none.
- **Body:** exactly `{body: string}`; `body` is required even though the verb is PATCH.

Use the same text normalization/validation as creation. `{}`, null, blank
bodies, and changes to author/timestamps/parent/permissions are rejected.
The caller cannot move a comment between records.

#### Output

**200 OK** — updated `CommentRead` with the resulting comment `ETag` and
`private, no-store`. An actual normalized-body change sets `editedAt` to server
UTC time and advances the comment version. Preserve ID, author, `createdAt`,
original activity ID, and event order. A normalized body identical to the
current body is a no-op: preserve edit time/version and emit no new event.
Still validate ownership and `If-Match` for a no-op.

Errors: shared mutation failures, `403` for another author's comment, `404`
for deleted/out-of-parent comments, `412` for a stale token, `428` for a missing
token, and `422` for invalid body/precondition. The parent record ETag is unchanged.

#### Example

```http
PATCH /api/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa HTTP/1.1
Content-Type: application/json
If-Match: "comment-d4fb93cf-v1"
X-CSRF-Token: csrf-example

{"body":"Please review the updated project deadline."}

HTTP/1.1 200 OK
Content-Type: application/json
ETag: "comment-d4fb93cf-v2"

{
  "id":"d4fb93cf-798b-4b1f-949d-9ec2c17a5daa",
  "author":{"id":"42","displayName":"Henry"},
  "body":"Please review the updated project deadline.",
  "createdAt":"2026-11-05T08:55:00Z",
  "editedAt":"2026-11-05T09:10:00Z",
  "permissions":{"update":true,"delete":true}
}
```

### DELETE `/api/{slug}/{id}/comments/{commentId}`

Delete the caller's own comment with its current version. Uses the same
ownership and current parent retrieve/comment checks as comment PATCH.

#### Input

- **Path:** required `slug`, `id`, `commentId`.
- **Headers:** shared JSON mutation headers and required comment `If-Match`.
- **Query/body:** none; reject a nonempty body with `422 validation_error`.

#### Output

**204 No Content** — empty body, `private, no-store`, no `ETag` or `Location`.
Tombstone the comment atomically: set `deletedAt`, remove its body from the
live comment row, and exclude it from comment detail/list and activity reads.
This is not a guarantee of removal from backups or an undelete API. The parent
record ETag is unchanged; no separate record revision or deletion timeline
entry is emitted.

Errors: shared mutation failures, particularly `403` ownership, `412` stale
version, `428` missing version, and `404` for a missing/already-deleted comment.
Concurrent edit/delete requests cannot both succeed with the same old token.

#### Example

```http
DELETE /api/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa HTTP/1.1
Accept: application/json
If-Match: "comment-d4fb93cf-v2"
X-CSRF-Token: csrf-example

HTTP/1.1 204 No Content
Cache-Control: private, no-store
```

### GET `/admin/{slug}/{id}`

Render the record page with permitted fields, read-only relationship cards,
and the authorized activity/comment UI. Requires current retrieve access and
an enabled detail view; the JSON API need not be enabled for basic HTML detail.

#### Input

- **Path:** required UI `slug`, record `id`; `/admin` is the configured UI prefix.
- **Headers:** session/authentication, optional `Accept: text/html`,
  `Accept-Language`, and `X-Request-ID`.
- **Query/body:** none. Activity/relationship pagination uses its own fragment route.

#### Output

**200 OK** — a full HTML document containing `#record-content`. Within it:

- Show only currently readable view fields; enable inline editors only for
  supported, currently writable form fields on a versioned resource.
- Render only configured, authorized relationship sections, with detail links
  only when target navigation is available and permitted.
- Include a timeline if at least one of history/comment is permitted. Include
  the comment composer only when current comment creation is permitted.
- Include CSRF controls and current per-object write tokens where a mutation
  form is rendered; never embed hidden record/field values as UI metadata.

Activity plugin absence or denied optional history/comment/relationship
sections does not prevent the permitted record page from loading; omit those
sections. Direct requests to their subroutes still enforce their own errors.
Unexpected database/render failures are errors, not silently omitted sections.
Initial collection sizes follow configured UI pagination defaults; no default
security cap is introduced.

Errors: HTML `401`, `403`, `404`, `406`, `422`, or `500`. Reading/rendering the
page emits no revision. The full document has no aggregate record/comment ETag.

#### Example

```http
GET /admin/people/17 HTTP/1.1
Accept: text/html

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
Cache-Control: private, no-store
```

Abbreviated document body; each populated fragment follows its detail below:

```html
<main id="record-content">
  <h1>Henry Adams</h1>
  <section id="record-field-name"><span>Henry Adams</span></section>
  <section id="record-relationship-tasks"><h2>Tasks</h2></section>
  <section id="record-activity"><h2 id="activity">Activity</h2></section>
</main>
```

### GET `/admin/{slug}/{id}/fields/{field}`

Render one currently readable field. This is also the cancel/reload target for
an inline editor; it never saves the editor's unsaved content.

#### Input

- **Path:** required UI `slug`, `id`, configured snake_case `field`, such as `start_date`.
- **Headers:** shared HTML read headers/session.
- **Query/body:** none.

#### Output

**200 OK** — `#record-field-{field}` with a localized label and escaped display
value. Dates/numbers use the current UI locale; null has a neutral empty
display, distinct from an empty string in the underlying value. A scalar FK
shows only a permitted target label/link; an inaccessible reference displays
an unavailable marker without target identifiers or metadata. File values
reuse authorized Files rendering/download access.

Include an Edit affordance only when the field is presently editable. Hidden
fields return `403`; an unknown/unconfigured field returns `404`. Other shared
HTML read errors apply. No mutation or activity event occurs.

#### Example

```http
GET /admin/people/17/fields/start_date HTTP/1.1
Accept: text/html

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

```html
<section id="record-field-start_date">
  <h2>Start date</h2>
  <time datetime="2026-11-01">Nov 1, 2026</time>
  <a href="/admin/people/17/fields/start_date/edit">Edit</a>
</section>
```

### GET `/admin/{slug}/{id}/fields/{field}/edit`

Render an inline editor with current record concurrency and CSRF tokens.
Requires parent retrieve/update access and field readability/writability.

#### Input

- **Path:** required UI `slug`, `id`, and configured editable scalar/file `field`.
- **Headers:** shared HTML read headers/session.
- **Query/body:** none.

Primary keys, computed/read-only values, version counters, server-controlled
preset fields, and relationship collections are not inline editable. A
configured but forbidden editor returns `403`; an unknown field returns `404`.

#### Output

**200 OK** — `#record-field-{field}` containing the type-appropriate form with
`csrf_token`, current record `if_match`, and the field control named `value`.
Nullable fields also have `set_null`, explicitly distinguishing null from an
empty string/empty file list. Use canonical machine-readable control values,
not localized display text. FK options use the target `:queryOptions` contract;
do not enumerate inaccessible targets. If target API options are unavailable,
the asynchronous selector is unavailable; do not substitute an unrestricted
target query.

The editor may contain opaque hidden file-upload tokens under the Files
plugin's existing field contract; uploading does not save the record field.
Errors: shared HTML read errors and `403` for denied update/field access. No
record ETag is assigned to this HTML fragment's HTTP response; `if_match`
identifies the underlying record representation, not the editor markup.

#### Example

```http
GET /admin/people/17/fields/start_date/edit HTTP/1.1
Accept: text/html

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

```html
<section id="record-field-start_date">
  <form method="post" action="/admin/people/17/fields/start_date">
    <input type="hidden" name="csrf_token" value="csrf-example">
    <input type="hidden" name="if_match" value="&quot;person-17-v4&quot;">
    <label>Start date <input type="date" name="value" value="2026-11-01"></label>
    <label><input type="checkbox" name="set_null" value="1">Clear value</label>
    <button type="submit">Save</button>
    <a href="/admin/people/17/fields/start_date">Cancel</a>
  </form>
</section>
```

### POST `/admin/{slug}/{id}/fields/{field}`

Save one inline field through the same authorized resource update service.
The form targets only the path field; trusted hooks/presets can change dependent values.

#### Input

- **Path:** required UI `slug`, `id`, `field`.
- **Headers:** `Content-Type: application/x-www-form-urlencoded`, optional
  `X-Obox-Fragment: true`, and shared HTML/session headers.
- **Query:** none.

| Form control | Required | Meaning |
| --- | --- | --- |
| `csrf_token` | Yes | Current signed CSRF token. |
| `if_match` | Yes | Specific strong record ETag from the editor. |
| `value` | Unless `set_null=1` | Canonical field value; an explicitly empty string is not null. |
| `set_null` | No | `1` clears a nullable field. Omitted or `0` assigns `value`. |

For `set_null=1`, ignore the ordinary rendered `value` control and assign null;
reject null for a non-nullable field. Boolean editors submit a single `value`
of `true` or `false`. Numeric/date controls use canonical formats; scalar FK
controls submit the target key. A JSON field's `value` is JSON text replacing
the entire column value. Files use the plugin's serialized field value; binary
content is uploaded through existing Files endpoints, not through this POST.
Repeated `value` controls and arbitrary additional record assignments are invalid.

Recheck update/retrieve access, field write permission, FK/file ownership, and
the record version at save time. Use form validation and resource validation;
never pass an unvalidated form dictionary directly to the ORM.

#### Output

**200 OK** for fragment submissions — refreshed `#record-content`, including
current field values, dependent relationship cards, authorized timeline head,
and fresh tokens in any rendered mutation controls. The client replaces the
whole root, so other unsaved inline editors would be discarded; the proposed
UI permits only one active record-field editor at a time. Comment drafts must
be preserved client-side across this replacement.

**303 See Other** for ordinary forms — `Location: /admin/{slug}/{id}`.
Actual recorded changes create one revision atomically; a no-op creates none.
Shared HTML mutation errors apply: `412` preserves the editor and unsaved
value, `422` shows field errors, `428` indicates a missing token, and `409`
rolls back the save. No error response refreshes the page as though saving succeeded.

#### Example

```http
POST /admin/people/17/fields/start_date HTTP/1.1
Content-Type: application/x-www-form-urlencoded
X-Obox-Fragment: true

csrf_token=csrf-example&if_match=%22person-17-v4%22&value=2026-11-02

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

Abbreviated replacement; production responses include all permitted content:

```html
<main id="record-content">
  <h1>Henry Adams</h1>
  <section id="record-field-start_date"><time datetime="2026-11-02">Nov 2, 2026</time></section>
  <section id="record-relationship-tasks"><h2>Tasks</h2></section>
  <section id="record-activity"><h2 id="activity">Activity</h2><p>Start date changed.</p></section>
</main>
```

### GET `/admin/{slug}/{id}/relationships/{field}`

Render one related-record card collection using the same authorization and
membership rules as the JSON relationship endpoint, projected through readable
view fields. No create/reassignment controls are offered.

#### Input

- **Path:** required UI `slug`, `id`, configured collection relationship `field`.
- **Headers:** shared HTML read headers/session.
- **Query:** optional `limit` and `cursor`, with shared cursor-only rules.
- **Body:** none. Other collection controls are unsupported.

The target's view list/retrieve access must be available; API enablement is not
required for server-rendered cards. This must not broaden membership or
permissions compared with the corresponding JSON projection.

#### Output

**200 OK** — `#record-relationship-{field}` with escaped readable labels/card
values, authorized target links, and a Load more link when `nextCursor`
exists. A cursor GET replaces the section with that returned page; any client
that appends cards must retain one section root and avoid duplicate IDs.
Empty authorized results show an empty-state message. No exact total or
mutation token is exposed. Shared HTML read errors apply, including `403`
for target denial, `404` for unknown relationships, and `422` for invalid cursors.

#### Example

```http
GET /admin/people/17/relationships/tasks?limit=10 HTTP/1.1
Accept: text/html

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

```html
<section id="record-relationship-tasks">
  <h2>Tasks</h2>
  <article><a href="/admin/tasks/91">Review launch plan</a><p>Status: Open</p></article>
</section>
```

### GET `/admin/{slug}/{id}/activity`

Render the authorized combined activity timeline, using the same event order,
redaction, and comment visibility rules as JSON activity reads.

#### Input

- **Path:** required UI `slug`, `id`.
- **Headers:** shared HTML read headers/session.
- **Query:** `kind` (`all`, `revision`, `comment`; default `all`), optional
  `limit`, and `cursor`, following the JSON activity contract.
- **Body:** none.

Require parent retrieve access and at least one permitted activity kind;
explicit kinds require their permission. The server-side projection uses
readable view fields, with the same restrictions on historical references and
files. No JSON API enablement is needed for this HTML adapter.

#### Output

**200 OK** — `#record-activity`, containing escaped revision differences,
current comments, safe actor labels, timestamps, edited markers, permitted
author controls, and cursor navigation. Show a composer only to an active
authenticated caller with comment access. Do not expose hidden changes in
HTML attributes or serialized data. A permitted empty timeline shows an
empty state, not an authorization error.

Author delete forms include CSRF and that comment's current specific
`if_match`. Edit links load the comment editor to obtain a fresh validator.
These server-rendered form tokens do not add tokens to the JSON activity
schema. Comment editing does not change original event position. A mutation
refreshes the head; a cursor GET returns that page and its next-page link.
Clients preserve an unsent composer draft during refresh/pagination and must
not interpret cursor navigation as a live change stream.

Errors: shared HTML read errors; `403` when no requested kind is permitted,
`404` when Activity is unavailable, and `422` for query/cursor errors.

#### Example

```http
GET /admin/people/17/activity?kind=all&limit=20 HTTP/1.1
Accept: text/html

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

```html
<section id="record-activity">
  <h2 id="activity">Activity</h2>
  <article data-activity-id="381"><p>Henry changed Name from Henry Adams to Henry Adam.</p></article>
  <article id="record-comment-d4fb93cf-798b-4b1f-949d-9ec2c17a5daa">
    <p>Henry: Please review the project deadline.</p>
    <a href="/admin/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa/edit">Edit</a>
    <form method="post" action="/admin/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa/delete">
      <input type="hidden" name="csrf_token" value="csrf-example">
      <input type="hidden" name="if_match" value="&quot;comment-d4fb93cf-v1&quot;">
      <button type="submit">Delete</button>
    </form>
  </article>
  <form method="post" action="/admin/people/17/comments">
    <input type="hidden" name="csrf_token" value="csrf-example">
    <label>Comment <textarea name="body" required></textarea></label>
    <button type="submit">Comment</button>
  </form>
</section>
```

### POST `/admin/{slug}/{id}/comments`

Create a plain-text comment from the record-page composer. Shares JSON comment
creation validation, ownership attribution, and atomic activity persistence.

#### Input

- **Path:** required UI `slug`, `id`.
- **Headers:** form content type, shared HTML/session headers, optional
  `X-Obox-Fragment: true`.
- **Query:** none.
- **Form:** required `csrf_token` and string `body`; no concurrency token on creation.

Requires current parent retrieve/comment access and an active authenticated
caller. Reject extra controls including author, parent ID, and `if_match`.
Repeated submissions are separate creations; disable accidental double submit
in the UI without promising server-side deduplication.

#### Output

**200 OK** for fragments — refreshed `#record-activity` at the head, including
the created comment and an empty composer with a fresh CSRF token.
Clear the submitted draft only after success. No record ETag changes.

**303 See Other** for ordinary forms —
`Location: /admin/{slug}/{id}#activity`.

On `422`, return `#record-activity` with the submitted composer text and field
errors, preserving the current timeline. Shared HTML authentication,
authorization, CSRF, media-type, and transaction errors apply. Failure must not
create an orphan comment or activity entry.

#### Example

```http
POST /admin/people/17/comments HTTP/1.1
Content-Type: application/x-www-form-urlencoded
X-Obox-Fragment: true

csrf_token=csrf-example&body=Please+review+the+project+deadline.

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

Abbreviated timeline replacement:

```html
<section id="record-activity">
  <h2 id="activity">Activity</h2>
  <article id="record-comment-d4fb93cf-798b-4b1f-949d-9ec2c17a5daa">
    <p>Henry: Please review the project deadline.</p>
  </article>
  <form method="post" action="/admin/people/17/comments">
    <input type="hidden" name="csrf_token" value="csrf-example-new">
    <label>Comment <textarea name="body" required></textarea></label>
    <button type="submit">Comment</button>
  </form>
</section>
```

### GET `/admin/{slug}/{id}/comments/{commentId}/edit`

Render the current author's comment editor with its independent write token.
Requires current retrieve/comment access and authenticated authorship.

#### Input

- **Path:** required UI `slug`, `id`, and `commentId`.
- **Headers:** shared HTML read headers/session.
- **Query/body:** none.

#### Output

**200 OK** — `#record-comment-{commentId}` containing the escaped current
plain-text body in a textarea, `csrf_token`, and the specific comment
`if_match`. The form posts to the same `/edit` URL. Cancel reloads the parent
activity fragment or navigates to the record page's activity anchor; there is
no separate HTML comment-detail GET in this proposal.

Shared HTML read errors apply. Another author's comment returns `403` even
when its body is readable; missing/deleted/out-of-parent comments return
`404`. Rendering does not mark the comment edited or change its activity position.

#### Example

```http
GET /admin/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa/edit HTTP/1.1
Accept: text/html

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

```html
<article id="record-comment-d4fb93cf-798b-4b1f-949d-9ec2c17a5daa">
  <form method="post" action="/admin/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa/edit">
    <input type="hidden" name="csrf_token" value="csrf-example">
    <input type="hidden" name="if_match" value="&quot;comment-d4fb93cf-v1&quot;">
    <label>Comment <textarea name="body" required>Please review the project deadline.</textarea></label>
    <button type="submit">Save</button>
    <a href="/admin/people/17#activity">Cancel</a>
  </form>
</article>
```

### POST `/admin/{slug}/{id}/comments/{commentId}/edit`

Save an author's plain-text comment edit with the same rules as JSON comment PATCH.

#### Input

- **Path:** required UI `slug`, `id`, `commentId`.
- **Headers:** form content type, shared HTML/session headers, optional
  `X-Obox-Fragment: true`.
- **Query:** none.
- **Form:** required `csrf_token`, specific comment `if_match`, and `body`.

Recheck current parent access, active authenticated ownership, normalized body
validation, and comment version. The parent record's ETag cannot satisfy this check.

#### Output

**200 OK** for fragments — refreshed `#record-activity` at the head, containing
the edited body, edited marker, and current author controls/tokens. Preserve
the comment's original activity order and creation time. A no-op preserves
the comment edit time/version. Preserve unrelated unsent composer drafts.

**303 See Other** for ordinary forms —
`Location: /admin/{slug}/{id}#activity`.

On `412` or `422`, return `#record-comment-{commentId}` as an editor with the
submitted text and conflict/field message. Keep the old submitted token on
conflict and provide explicit reload; never silently overwrite. `403`, `404`,
`409`, `428`, and the shared HTML mutation errors apply. No new timeline entry
or record revision is emitted.

#### Example

```http
POST /admin/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa/edit HTTP/1.1
Content-Type: application/x-www-form-urlencoded
X-Obox-Fragment: true

csrf_token=csrf-example&if_match=%22comment-d4fb93cf-v1%22&body=Please+review+the+updated+project+deadline.

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

Abbreviated replacement; the complete response includes the other permitted events:

```html
<section id="record-activity">
  <h2 id="activity">Activity</h2>
  <article id="record-comment-d4fb93cf-798b-4b1f-949d-9ec2c17a5daa">
    <p>Henry: Please review the updated project deadline.</p>
    <small>Edited Nov 5, 2026, 09:10 UTC</small>
  </article>
</section>
```

### POST `/admin/{slug}/{id}/comments/{commentId}/delete`

Delete an author's comment using the same version and tombstone semantics as
JSON comment DELETE. The UI asks for confirmation before submission; a GET
link must never perform the deletion.

#### Input

- **Path:** required UI `slug`, `id`, `commentId`.
- **Headers:** form content type, shared HTML/session headers, optional
  `X-Obox-Fragment: true`.
- **Query:** none.
- **Form:** exactly required `csrf_token` and specific comment `if_match`; no body text.

Requires current parent retrieve/comment access and active authenticated
ownership. A delete button rendered earlier does not grant lasting permission.

#### Output

**200 OK** for fragments — refreshed `#record-activity` without the deleted
comment. Render the empty state if nothing visible remains. Preserve an
unrelated composer draft; the record ETag and other field editors are unchanged.

**303 See Other** for ordinary forms —
`Location: /admin/{slug}/{id}#activity`.

On `412`, return the comment root with a conflict/reload message and leave it
in the timeline; do not remove it optimistically as a successful deletion.
`403`, `404`, `409`, `422`, `428`, and shared HTML mutation errors apply.
A repeated deletion after success returns `404`.

#### Example

```http
POST /admin/people/17/comments/d4fb93cf-798b-4b1f-949d-9ec2c17a5daa/delete HTTP/1.1
Content-Type: application/x-www-form-urlencoded
X-Obox-Fragment: true

csrf_token=csrf-example&if_match=%22comment-d4fb93cf-v2%22

HTTP/1.1 200 OK
Content-Type: text/html; charset=utf-8
```

When no visible entries remain, the response includes the empty state and
the authorized composer:

```html
<section id="record-activity">
  <h2 id="activity">Activity</h2>
  <p>No activity yet.</p>
  <form method="post" action="/admin/people/17/comments">
    <input type="hidden" name="csrf_token" value="csrf-example-new">
    <label>Comment <textarea name="body" required></textarea></label>
    <button type="submit">Comment</button>
  </form>
</section>
```

## Review decisions and compatibility

These decisions describe the implemented record-page contract:

| Topic | Decision / compatibility impact |
| --- | --- |
| Record concurrency | Require durable versioning for editable record pages; reuse the versioned record ETag/If-Match support documented in `docs/api-spec.md`. |
| Comment concurrency | Maintain a durable independent comment version; JSON detail/create/update expose its validator. Existing host databases must migrate `obox_comment.version`. |
| Revision field names | Use the core API public camelCase aliases in `changes[].field`; preserve the existing wire contract and its regression coverage. |
| Relationship scope | Explicitly configured record-page collection cards remain read-only, with native target-PK ordering, cursor-only pagination, and no exact counts. Opt-in API membership writes use `API.write_relationships`; see the shared API specification. |
| Comment access | Parent retrieve plus comment permission; active authors edit/delete only their own comments. No moderator override, threads, mentions, or notifications. |
| Comment content/deletion | Plain text, normalized line endings, no framework length cap; tombstone deletion removes the live body and hides the entry. No edit-history endpoint. |
| HTML transport | Introduce `X-Obox-Fragment: true` for POST replacements; ordinary success uses 303. Field saves replace record content; comment mutations replace activity. Error status/body must be handled explicitly by the client. |
| Draft preservation | One active record-field editor; preserve comment drafts across field/activity replacements. Reload after a conflict requires explicit user action. |

## References

- [Shared API specification](api-spec.md): query syntax, field serialization,
  RFC 9457 errors, authentication, and existing CRUD/selector behavior.
- [Core specification](core-spec.md): policies, row/field access, resource
  service, transactions, and generated UI routes.
- [HTTP If-Match](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/If-Match)
  and [428 Precondition Required](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Status/428):
  strong comparison, wildcard semantics, and missing/stale preconditions.
- [RFC 9457](https://www.rfc-editor.org/rfc/rfc9457.html): Problem Details for HTTP APIs.
