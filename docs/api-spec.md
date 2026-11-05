# OBOX API Specification

**Status:** This document describes the proposed HTTP contract. Availability depends on the
implementation phase, enabled resource operations, plugins, and permissions.
The [core specification](core-spec.md) defines the shared service and policy contracts.

## Naming convention

Database, SQLAlchemy, Python, and Pydantic field names use `snake_case`.
HTTP JSON request and response properties and API-facing TypeScript identifiers
use `camelCase`. For example, Python `question_text` is JSON `questionText`.
This applies to records, pagination, errors, metadata, and nested response objects.

Convert casing only at the Pydantic API boundary through shared request and
response base models with `alias_generator=to_camel`:

| Schema | Boundary behavior |
| --- | --- |
| Request | Accept aliases only (`validate_by_alias=True`, `validate_by_name=False`); reject unknown properties (`extra="forbid"`). |
| Response | Accept internal Python names (`validate_by_name=True`), serialize aliases (`serialize_by_alias=True`), and support ORM attributes (`from_attributes=True`); reject unknown properties (`extra="forbid"`). |

Every JSON route, including error responses, must have a concrete Pydantic
response schema. Nested objects use the shared response base too; generated
resource and expansion models declare their fields before registration.
Alias generation does not rename arbitrary dictionary keys. Keys within a
user-supplied JSON column remain application data and are preserved.
Do not rename keys manually in routes, services, or frontend adapters.
OpenAPI component property names must expose the same camelCase aliases.

URL paths follow the path convention below. Query parameter names and resource
field references in `select`, `order`, filters, and `expand` retain their
configured snake_case names, including inside `:query` pairs. For example,
`select=question_text,pub_date` returns `questionText` and `pubDate` properties.
Stable string values such as error codes (`validation_error`) are unchanged.

Implemented API adapters follow this naming contract. Do not retain snake_case
JSON aliases when updating an endpoint. The temporary legacy list option changes
only the envelope shape.

## Endpoint overview

Paths use the proposed `/api` prefix and have no trailing slash.
`{slug}` is a resource's plural collection name, such as `questions` or
`questionCategories`; `{id}` is its primary key. Collection names use camelCase,
start with a lowercase ASCII letter, and contain only ASCII letters and digits
(`[a-z][a-zA-Z0-9]*`). Customized resource API prefixes follow the same rule.
Record IDs retain their documented resource-specific format.
The top-level paths `auth`, `events`, and `tasks` are reserved and cannot be
used as resource API collection names.

Custom resource methods use `:verb` after the collection path; multiword verbs
use camelCase. `{name}` is a registered action's public camelCase verb, such as
`publish` or `sendReminder`. Read-only methods use GET, or POST when a query body
is needed; methods that mutate resources use POST.

These path rules follow [Google AIP-122](https://google.aip.dev/122) and
[AIP-136](https://google.aip.dev/136). OBOX keeps `/api` without a version segment
and defines its own query, response, and batch contracts.

### Resources

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/{slug}` | List records. |
| POST | `/api/{slug}` | Create a record. |
| GET | `/api/{slug}/{id}` | Retrieve a record. |
| PATCH | `/api/{slug}/{id}` | Update selected fields of a record. |
| DELETE | `/api/{slug}/{id}` | Delete a record. |
| POST | `/api/{slug}:query` | List records using a JSON query. |
| GET | `/api/{slug}:queryOptions` | List record IDs and labels for relationship selectors. |
| GET | `/api:describe` | Describe resources available to the current principal. |
| GET | `/api/{slug}:describe` | Describe a resource's fields, permissions, and operations. |
| POST | `/api/{slug}:{name}` | Run an action on selected records. |

`:query` (P1) and `:queryOptions` (P2) are implemented. The detailed create
contract below is the next review target; a basic create route already exists.
`:describe` remains planned for P2. Custom method names are reserved and
cannot be reused by registered actions on the same collection.

### Authentication — Auth plugin, P2

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/auth/csrf` | Obtain a CSRF token. |
| POST | `/api/auth/login` | Create a session using credentials. |
| POST | `/api/auth/logout` | End the current session. |
| GET | `/api/auth/me` | Retrieve the current user's identity and permissions. |

### Later phase — P3

| Method | Path | Description |
| --- | --- | --- |
| POST | `/api/{slug}:batchWrite` | Apply create, update, delete, or upsert operations atomically. |
| GET | `/api/events` | Stream resource change notifications. |

CSV export uses the list path with `Accept: text/csv`. Long-running operations
may return a task location at `/api/tasks/{id}`; the task endpoint's method
and contract remain to be defined. Upsert is a batch operation, with no separate path.

## Endpoint detail

### GET `/api/{slug}`

List records visible to the current principal. Requires the resource's `list`
permission; row scope and field permissions apply to every request.

The query language follows the documented subset of
[PostgREST v14](https://docs.postgrest.org/en/v14/references/api/tables_views.html)
below. OBOX retains `search`, opaque cursors, the `{items, page}` envelope,
`count=exact`, and RFC 9457 errors. Full PostgREST client compatibility is not
part of this contract; range headers and `Prefer: count=...` are not supported.
This section defines the proposed HTTP query syntax; builder and service
configuration names remain as defined in the core specification.

#### Input

**Path parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `slug` | string | Yes | Registered resource slug. |

**Headers**

Use the same-origin session cookie or `Authorization: Bearer <token>` when
authentication is required. Bearer tokens require the API tokens plugin.
This read operation requires no CSRF token.

| Name | Required | Description |
| --- | --- | --- |
| `Accept` | No | `application/json` for the response documented here. |
| `Accept-Language` | No | Preferred language for error messages and labels. |
| `X-Request-ID` | No | Correlation ID, up to 128 characters from `A-Z`, `a-z`, `0-9`, `.`, `_`, and `-`; generated when omitted. |

**Query parameters** — all optional; no request body.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `search` | string | Omitted | Case-insensitive substring search over configured search fields. |
| `order` | string | Resource default | Comma-separated `<field>[.asc\|.desc][.nullsfirst\|.nullslast]` terms, such as `pub_date.desc,id.asc`. Only configured, readable sort fields are allowed. |
| `limit` | integer ≥ 0 | Configured default, otherwise unlimited | Maximum records to return. No framework-enforced maximum unless configured. |
| `offset` | integer ≥ 0 | `0` | Records to skip. Cannot be supplied with `cursor`. |
| `cursor` | string | Omitted | Opaque token from `page.nextCursor`; requires an explicit `limit`. |
| `count` | string | Omitted | Set to `exact` to include the total matching record count. |
| `select` | string | All readable API fields | Comma-separated field names, such as `id,question_text`; `*` selects all readable API fields. The primary key is always included. |
| `expand` | string | Omitted | Comma-separated registered FK relationships whose IDs and labels appear in `expanded`. One level only. |
| `<field>` | string | Omitted | `<op>.<value>` or `not.<op>.<value>`, such as `pub_date=gte.2026-01-01` or `status=not.eq.draft`. |
| `and`, `or` | string | Omitted | Parenthesized, comma-separated conditions, such as `or=(status.eq.live,priority.gte.3)`. |
| `not.and`, `not.or` | string | Omitted | Negate a parenthesized group, such as `not.and=(price.gte.10,price.lte.20)`. |

| Operator | Applicable fields | Meaning |
| --- | --- | --- |
| `eq`, `neq` | Scalar | Equal or unequal using SQL null semantics. |
| `isdistinct` | Scalar | Unequal, treating null as a comparable value. |
| `gt`, `gte`, `lt`, `lte` | Numeric, decimal, date, datetime, time | Greater than, greater than or equal, less than, or less than or equal. |
| `like`, `ilike` | String | Case-sensitive or case-insensitive pattern match; `*` is an alias for `%`. |
| `in` | Scalar | Membership in a parenthesized list, such as `status=in.(draft,live)`. |
| `is` | Scalar for `null`; boolean for `true`, `false` | Test null or a boolean value, such as `category_id=is.null` or `active=is.true`. |

**Filtering and logic**

Top-level filters and groups are ANDed. A field key may repeat to express
multiple conditions: `price=gte.10&price=lte.20`. Logical group keys may also
repeat, with each group ANDed at the top level. Other parameters, including
`search`, `select`, `order`, `expand`, and pagination controls, may appear only once.

Inside a group, leaves use `<field>.<op>.<value>`; prefix an operator with `not.`
to negate it. Nested groups use `and(...)`, `or(...)`, `not.and(...)`, or
`not.or(...)`, for example
`or=(status.eq.live,and(priority.gte.3,active.is.true))`.
Groups retain the maximum depth of 3 and 100 total nodes; all flat filters and
group conditions contribute to the same node budget.

Filter fields must belong to the configured filter allowlist and be readable
for the current list context. Sort fields must also be permitted in that context.
These checks apply inside every logical group. `select` does not grant permission
to filter, sort, or expand a field. JSON columns cannot be filtered or sorted.

`neq` and `not.eq` exclude null values, following SQL three-valued logic.
Use `isdistinct.live` to include nulls when excluding the non-null value `live`.
Use `is.null` and `not.is.null` for null checks. Empty strings are separate from
nulls: `or=(name.is.null,name.eq."")` matches either; `is_empty` is not an operator.
Negation follows SQL null semantics, so negating a comparison does not implicitly
include null rows.

For string matching, use `name=ilike.*john*` for a case-insensitive substring
and `name=ilike.john*` for a prefix. `%` and `_` are pattern wildcards; escape
literal wildcard characters with a backslash. The `search` parameter treats
the supplied text as a literal substring rather than a wildcard pattern.

Values containing reserved grammar characters (commas, periods, colons, or
parentheses) may be enclosed in double quotes, for example
`name=in.("Doe, Jane","Smith")`. Within quoted values, escape double quotes and
backslashes with a backslash. Clients must URL-encode query values; examples
here show decoded syntax. Decode URL encoding once before parsing the grammar.

Only the operators and syntax documented here are supported. Aliases, casts,
computed expressions, JSON-path operators, full-text search, operator modifiers,
and relationship filters are outside this subset. The HTTP aliases `fields`,
`ordering`, `filter`, `filter.*`, and `filter_op.*` are not supported.

**Ordering and pagination**

Sort direction defaults to `asc`. Without an explicit null modifier, ascending
terms use `nullslast` and descending terms use `nullsfirst`. Append the primary
key in ascending order as a stable tie-breaker unless it is already in the
effective order. This also applies to the resource's default ordering.

Cursor requests must preserve the filters, logical groups, `search`, and
effective order used to obtain the token. Tokens are signed, grant no access,
and are revalidated against the current policy on every request. Nullable
ordering fields do not support cursor pagination, including when a null-order
modifier is supplied.
Use offset pagination to jump between numbered pages and cursor pagination
to continue a sequential list. Pagination parameters are accepted even when
pagination is disabled by default. `limit=0&count=exact` returns only the count
with an empty `items` array.

**Selection and expansion**

Unknown static API fields in `select` are rejected. Fields hidden by contextual
policy are omitted; `null` represents a readable field with no value. Selection
supports field names or `*`; aliases, expressions, and nested relationship
selection such as `select=category(id,name)` are not supported. OBOX always
includes the permitted primary key even if omitted from `select`.
Expanded relationships retain the original FK field and add
`expanded.<relationshipAlias>: {"id": ..., "label": ...}`, where the relationship
property uses its generated camelCase alias. For example, `expand=primary_author`
adds `expanded.primaryAuthor`. Expansion containers and relationship summaries
use typed response models, rather than arbitrary dictionaries. The `expanded`
property is reserved; registration rejects a resource field whose generated
alias conflicts with it. A related record outside the target resource's scope
produces `null`; an unregistered target is rejected.

#### Output

**200 OK** — `Content-Type: application/json`.

| Field | Type | Description |
| --- | --- | --- |
| `items` | object[] | Matching records with permitted fields; empty when no records match. |
| `page` | object | Pagination metadata; every key below is always present. |
| `page.limit` | integer or null | Effective limit; null when unpaginated. |
| `page.offset` | integer | Applied offset; defaults to zero. |
| `page.hasMore` | boolean | Whether more matching records remain. |
| `page.nextCursor` | string or null | Continuation token; null when exhausted or cursor pagination is unsupported. |
| `page.total` | integer or null | Count after filtering and row scoping, before pagination; null unless `count=exact`. |

JSON properties use camelCase. Decimal and BigInteger values are strings;
integer primary keys exceeding JavaScript's safe integer range are also strings.
Dates and times use ISO 8601; datetimes use UTC with a `Z` suffix.

Response headers include `X-Request-ID`, `Cache-Control: private, no-cache`,
and `Vary: Cookie, Authorization, Accept-Language`.

**Errors** use `Content-Type: application/problem+json` with
`type`, `title`, `status`, `code`, `detail`, `instance`, `traceId`, and a flat
`errors` array. Field errors contain `field`, `pointer`, `code`, and `detail`,
with optional `meta`. Clients should branch on stable `code` values.

| Status | Code | Description |
| --- | --- | --- |
| 401 | `unauthenticated` | Authentication is required. |
| 403 | `permission_denied` | The principal cannot list this resource. |
| 422 | `validation_error`, `unknown_parameter` | Invalid values or query grammar, repeated control parameters, or unknown parameters. |
| 422 | `invalid_cursor`, `cursor_unsupported_ordering` | Invalid cursor, changed query, or nullable cursor ordering. |
| 422 | `unsupported_operator`, `unknown_field` | Unsupported filter operator or invalid field selection. |
| 422 | `expand_depth`, `expand_unavailable` | Nested expansion or an unregistered relationship target. |

The proposed envelope becomes the default in 0.5. The temporary
`API.list_envelope=False` compatibility setting returns the legacy bare array
for one minor release.

#### Example

Request the first two matching records and the total count:

```http
GET /api/questions?select=id,question_text,pub_date&pub_date=gte.2026-01-01&order=pub_date.desc&limit=2&count=exact HTTP/1.1
Accept: application/json
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-cache
Vary: Cookie, Authorization, Accept-Language
X-Request-ID: req-42

{
  "items": [
    {"id": 42, "questionText": "Favorite color?", "pubDate": "2026-03-01T09:00:00Z"},
    {"id": 41, "questionText": "Favorite language?", "pubDate": "2026-02-20T09:00:00Z"}
  ],
  "page": {
    "limit": 2,
    "offset": 0,
    "hasMore": true,
    "nextCursor": "eyJ2IjoxLC4uLn0.k3J9",
    "total": 137
  }
}
```

For the next offset page, repeat the query with `offset=2`. For cursor
continuation, send the returned opaque token as `cursor`, keep `limit=2` and
the same filters and effective order, and omit `offset`.

### POST `/api/{slug}`

**Status:** Proposed detailed contract for the next implementation; basic create
is already available. The rules below define the target behavior, including
request validation, explicit null handling, response headers, and error codes.

Create one record in the collection. The route is registered when the resource
API is enabled and `create` is present in `API.operations`. It requires `create`
permission and uses the shared service validation, server presets, hooks, and
post-write policy checks defined in the core specification.

#### Input

**Path parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `slug` | string | Yes | Registered resource's API collection name. |

**Headers**

| Name | Required | Description |
| --- | --- | --- |
| `Content-Type` | Yes | `application/json`; a UTF-8 charset parameter is accepted. |
| `Accept` | No | `application/json`. |
| `Accept-Language` | No | Preferred language for error messages. |
| `X-Request-ID` | No | Same correlation ID rules as GET; generated when omitted or invalid. |
| `X-CSRF-Token` | With cookies | Valid signed token bound to the current principal and request cookies; expires after one hour. |

Authentication uses the same session cookie or host-supported bearer
authentication as GET. Any request carrying cookies requires CSRF validation,
including a bearer request that also carries cookies. Requests without cookies
require no CSRF token. A supplied `Origin` must match the request's scheme, host,
and effective port. For HTTPS requests with cookies and no `Origin`, a same-origin
`Referer` is required. Invalid, missing, or expired CSRF credentials return `403`.

**Query string:** must be empty. Any query-string parameter, including `select`
or `expand`, returns `422 unknown_parameter`.

**JSON body**

The root is one record object, without a `data` wrapper. Arrays and scalar roots
are rejected. Property names use the generated camelCase aliases, such as
`questionText` and `categoryId`; snake_case aliases are rejected. Keys inside
a JSON column remain application data and are preserved.

The resource's generated create schema defines its static input fields and
types. Effective writable fields are narrowed by `API.write_fields` and
`policy.write_fields("create", ctx)`, excluding primary keys, read-only fields,
and fields controlled by server presets. Unknown or statically excluded
properties return `422` with a field error code of `unknown_field`. A property
present in the static schema but forbidden by the current policy or a server
preset returns `403 permission_denied`. Supplied fields are never silently dropped.

| Input | Meaning |
| --- | --- |
| Omitted property | Leave it unset so a server preset, hook, or model/database default can supply the value. |
| Required property | A non-nullable field without a default must be supplied when client-writable, unless a server preset supplies it. Required fields outside the client projection must be supplied by trusted server logic. |
| Explicit `null` | Accepted only for nullable fields and stored as SQL NULL, including when the column has a default. It does not request the default. |
| Empty string | A supplied string value; never implicitly converted to null. Resource validation may reject it. |
| Empty object `{}` | Valid only when all required values can be supplied by server logic or defaults. |

Use JSON values matching the generated schema: strings, numbers, booleans, and
nullable values as applicable. Dates and times use ISO 8601 strings; UUIDs and
enum values use their declared string/value representation. Send decimals and
large integer IDs as strings to preserve precision. Scalar coercion follows
the generated Pydantic schema; this endpoint introduces no global strict mode.
String length and resource validation constraints still apply.

Relationships are assigned through scalar FK properties, such as `categoryId`.
For each supplied non-null FK, the target must have a registered policy and be
retrievable under its operation permission, SQL scope, and per-record checks.
A non-primary reference key must also be readable. A missing or inaccessible
target returns the same `403 permission_denied` without revealing which case
occurred. Target `list` permission is not required. An ID returned by
`:queryOptions` grants no write permission; every create request rechecks access.
Nested relationship writes, bulk create, client-assigned primary keys, and upsert
are outside this endpoint. Objects and arrays remain valid values for JSON columns.

POST is not idempotent. Repeating a successful request can create another record
or encounter a uniqueness conflict. This contract provides no `Idempotency-Key`
deduplication or automatic retry guarantee. A client that loses the response
cannot assume the transaction failed.

#### Output

**201 Created** — `Content-Type: application/json`.

The body is the created record as one object, without `items` or `page`.
It contains only fields permitted by `API.read_fields` and contextual per-record
read permissions, using the same camelCase aliases and value formats as GET.
Readable fields include generated IDs, defaults, and values normalized by
validation or hooks. Unreadable fields are omitted; readable null values remain
`null`. No expansion object is added. `API.list_envelope` has no effect.

Creating a record does not require `list` or `retrieve` permission on the source
resource. Those permissions are checked independently on later GET requests.
If no API fields are readable, the response body is `{}`.

| Header | Value |
| --- | --- |
| `Location` | Created record path, for example `/api/questions/43`, only when its primary key is included in the permitted response projection. |
| `X-Request-ID` | Accepted or generated correlation ID. |
| `Cache-Control` | `private, no-store`, on success and error responses. |
| `Vary` | `Cookie, Authorization, Accept-Language`. |
| `Content-Language` | Selected response language. |

The `Location` follows [RFC 9110 section 9.3.3](https://www.rfc-editor.org/rfc/rfc9110.html#section-9.3.3),
uses the resource's configured API prefix and application mount path, and
percent-encodes the ID as one path segment. It is omitted when the primary key
is hidden, so the header cannot bypass field permissions. A location identifies
the resource; following it still requires an enabled retrieve route and permission.

Validation, server presets, `before_save`, database flush, and the resulting
record's `policy.check("create", instance, ctx)` run in one transaction. A failed
write or policy check rolls back the transaction. Success is sent only after
commit; conflicts raised at commit return `409`. A create event becomes available
to `after_commit` only after success. That callback is best effort: its failure
does not undo the record or turn a committed create into a failed response.

**Errors** use the same RFC 9457 `application/problem+json` envelope as GET.
`instance` is the collection request path; `traceId` matches `X-Request-ID`.

| Status | Code | Description |
| --- | --- | --- |
| 400 | `malformed_json` | The body is not valid JSON. |
| 401 | `unauthenticated` | Authentication is required. |
| 403 | `permission_denied` | Create, field, relationship, CSRF, or resulting-record checks deny the request. |
| 409 | `resource_conflict` | The write conflicts with database constraints or concurrent state. |
| 415 | `unsupported_media_type` | Content-Type is missing or is not `application/json`. |
| 422 | `validation_error` | Missing required input, invalid body shape, unknown properties, invalid values, or resource validation failure. |
| 422 | `unknown_parameter` | A query-string parameter was supplied. |
| 500 | `internal_error` | Unexpected failure; internal exception details are hidden. |

Body field errors use public camelCase names and JSON Pointers, including errors
from service validation. For example, a missing `questionText` uses
`field: "questionText"`, `pointer: "#/questionText"`, and `code: "required"`.
Unknown properties use `unknown_field`, schema type errors use `invalid_type`,
and resource validation errors use `validation_error`. Whole-record errors use
`field: "__all__"` and `pointer: "#"`. Do not include SQL, constraint names,
raw database exceptions, or hidden record values in the response.

#### Example

Create a question on a resource where both supplied fields are writable and
the returned fields are readable. This example uses bearer authentication
without cookies:

```http
POST /api/questions HTTP/1.1
Authorization: Bearer <token>
Content-Type: application/json
Accept: application/json
X-Request-ID: req-create-43

{
  "questionText": "Favorite editor?",
  "pubDate": "2026-11-05T09:00:00Z"
}
```

```http
HTTP/1.1 201 Created
Location: /api/questions/43
Content-Type: application/json
Cache-Control: private, no-store
Vary: Cookie, Authorization, Accept-Language
Content-Language: en
X-Request-ID: req-create-43

{
  "id": 43,
  "questionText": "Favorite editor?",
  "pubDate": "2026-11-05T09:00:00Z"
}
```

### POST `/api/{slug}:query`

**Status:** Implemented (P1).

List records using query parameters carried in a JSON body. Use this endpoint
for long filters that exceed practical URL limits, or queries stored by a client.
The server executes the query for this request; it does not save a view.

The body carries an ordered list of name/value pairs, preserving repeated field
and logical-group keys. Each pair uses exactly the same query syntax as
[GET `/api/{slug}`](#get-apislug), including operators, quoting, field allowlists,
selection, expansion, ordering, and pagination. This format lets both endpoints
share the existing parser and service behavior.

The endpoint is enabled when the resource API is enabled and `list` is present
in `API.operations`. It requires `list` permission, applies the same SQL row
scope and field permissions, and adds no new policy action.

#### Input

**Path parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `slug` | string | Yes | Registered resource slug. |

**Headers**

| Name | Required | Description |
| --- | --- | --- |
| `Content-Type` | Yes | `application/json`; a UTF-8 charset parameter is accepted. |
| `Accept` | No | `application/json`. |
| `Accept-Language` | No | Preferred language for error messages and labels. |
| `X-Request-ID` | No | Same correlation ID rules as GET; generated when omitted or invalid. |

Authentication uses the same session cookie or host-supported bearer
authentication as GET. No CSRF token is required: this endpoint only reads
resources. Existing cookie settings and configured origin/CORS restrictions
still apply. It must never call create, update, delete, or action hooks, or emit
resource mutation events. An `Idempotency-Key` is not required for retries;
results can change between requests as records or permissions change.

**Query string:** must be empty. Any query-string parameter returns
`422 unknown_parameter`; all query input belongs in the body.

**JSON body**

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `query` | array of `[string, string]` pairs | Yes | Query parameter names and values in their original order. |

The root must be an object containing only `query`. Each pair must contain
exactly two strings; numbers, booleans, nulls, nested objects, and arrays used
as pair values are rejected. For example, use `["limit", "25"]`, not
`["limit", 25]`. Missing input and unknown body properties are rejected.

`query` is a JSON property; names embedded inside pairs are query-language
strings. Use `["pub_date", "gte.2026-01-01"]`, not a camelCase field reference.
Returned records still use `pubDate`, as required by the naming convention.

An empty list, `{"query": []}`, is equivalent to GET without query parameters:
resource defaults apply, including the optional default/maximum limit.
Omission is expressed by leaving out a pair. An empty string remains a supplied
value and follows the GET parameter's validation rules.

Pair values are decoded query text. JSON escaping applies; URL percent-decoding
does not. For example, `["search", "a%2Cb"]` searches for the literal text
`a%2Cb`; `["search", "a,b"]` searches for `a,b`. When a filter expression needs
quoted values, retain the query-language quotes inside the JSON string, such
as `["name", "in.(\"Doe, Jane\",\"Smith\")"]`.

All supported GET parameters are accepted as pairs. Field and logical-group
keys may repeat and are ANDed at the top level; control parameters such as
`select`, `order`, `limit`, and `cursor` may appear only once. The same depth
of 3 and budget of 100 filter nodes apply across the complete query.
This endpoint introduces no request-size cap or mandatory result limit;
deployment infrastructure may impose its own limits.

Cursor tokens are interchangeable with GET for the same resource, filters,
logical groups, search, and effective order. Moving a query from a URL to JSON
must preserve pair order and decoded values. A cursor still requires an
explicit `limit` and cannot be combined with `offset`; nullable ordering fields
remain unsupported. Every request rechecks the current principal's permissions.

#### Output

**200 OK** — `Content-Type: application/json`.

The response uses the same record projection, value formats, and pagination
contract as GET: `{"items": [...], "page": {...}}`. All five page keys remain
present. `count=exact` counts after filtering and row scoping, before pagination;
`limit=0&count=exact`, expressed as pairs, returns an empty `items` array and
the total. `API.list_envelope=False` returns the same legacy array as GET.

| Header | Value |
| --- | --- |
| `X-Request-ID` | Accepted or generated correlation ID. |
| `Cache-Control` | `private, no-store`, on success and error responses. |
| `Vary` | `Cookie, Authorization, Accept-Language`. |
| `Content-Language` | Selected response language. |

The endpoint does not return `ETag`, conditional `304` responses, or a resource
creation `Location` header. Responses are not stored by HTTP caches.

**Errors** use the same RFC 9457 envelope as GET. `instance` contains the
`:query` request path, and `traceId` matches `X-Request-ID`.

| Status | Code | Description |
| --- | --- | --- |
| 400 | `malformed_json` | The body is not valid JSON. |
| 401 | `unauthenticated` | Authentication is required. |
| 403 | `permission_denied` | The principal cannot list this resource. |
| 415 | `unsupported_media_type` | Content-Type is missing or is not `application/json`. |
| 422 | `validation_error` | Invalid body shape, pair types, query grammar, values, or repeated controls. |
| 422 | `unknown_parameter` | Query-string input or a legacy/unsupported query parameter. |
| 422 | `unknown_field`, `unsupported_operator` | Invalid selection or a forbidden filter/sort field or operator. |
| 422 | `invalid_cursor`, `cursor_unsupported_ordering` | Invalid cursor, changed query, or unsupported cursor ordering. |
| 422 | `expand_depth`, `expand_unavailable` | Unsupported expansion. |
| 500 | `internal_error` | Unexpected failure; internal exception details are hidden. |

For body-shape errors, field entries use `required`, `invalid_type`, or
`unknown_field` as appropriate, with a JSON Pointer into the request body.
For example, a numeric pair value at index 0 uses `pointer: "#/query/0/1"` and
`field: "query.0.1"`. Query-semantic errors use `pointer: "#/query"` and the
same field name and error code as GET; they may concern multiple pairs or an
entire logical group.

#### Example

List questions within a date range, returning two records and the total:

```http
POST /api/questions:query HTTP/1.1
Content-Type: application/json
Accept: application/json
X-Request-ID: req-query-42

{
  "query": [
    ["select", "id,question_text,pub_date"],
    ["pub_date", "gte.2026-01-01"],
    ["pub_date", "lt.2027-01-01"],
    ["order", "pub_date.desc"],
    ["limit", "2"],
    ["count", "exact"]
  ]
}
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-store
Vary: Cookie, Authorization, Accept-Language
Content-Language: en
X-Request-ID: req-query-42

{
  "items": [
    {"id": 42, "questionText": "Favorite color?", "pubDate": "2026-03-01T09:00:00Z"},
    {"id": 41, "questionText": "Favorite language?", "pubDate": "2026-02-20T09:00:00Z"}
  ],
  "page": {
    "limit": 2,
    "offset": 0,
    "hasMore": true,
    "nextCursor": "eyJ2IjoxLC4uLn0.k3J9",
    "total": 137
  }
}
```

To continue, append `["cursor", "<returned token>"]` to the same `query` list.
For offset pagination, append `["offset", "2"]` instead of the cursor pair.

### GET `/api/{slug}:queryOptions`

**Status:** Implemented (P2).

List IDs and display labels from the target resource for a relationship selector.
For example, a question selector calls `/api/questions:queryOptions`.
This initial contract covers single-column foreign keys targeting the resource's
primary key; selecting non-primary reference keys or composite keys is outside
this endpoint.

The route is registered when the target resource API is enabled and both `list`
and `retrieve` are present in `API.operations`. It requires both permissions,
adds no new policy action, and applies both SQL row scopes before filtering,
counting, and pagination. Each candidate also passes the per-record `retrieve`
check before becoming an option. A denied record is omitted.

The primary key must belong to `API.read_fields` and be readable in the current
list context; otherwise the request returns `403 permission_denied`. A candidate
whose primary key is hidden by per-record field permissions is omitted.
Labels use only readable API fields. Returning an option does not authorize a
later write: source-field and relationship permissions are rechecked on save.

#### Input

**Path parameters**

| Name | Type | Required | Description |
| --- | --- | --- | --- |
| `slug` | string | Yes | Registered target resource's API collection name. |

**Headers and authentication:** same as [GET `/api/{slug}`](#get-apislug),
including `Accept`, `Accept-Language`, and `X-Request-ID`. Use the same session
cookie or host-supported bearer authentication. No CSRF token is required.

**Query parameters** — all optional; no request body.

| Name | Type | Default | Description |
| --- | --- | --- | --- |
| `search` | string | Omitted | Literal, case-insensitive substring search over configured readable search fields. |
| `<field>`, `and`, `or`, `not.and`, `not.or` | string | Omitted | Same filter operators, repeated field/group keys, quoting, and allowlists as GET. |
| `order` | string | Resource default | Same ordering and primary-key tie-breaker as GET. |
| `limit` | integer ≥ 0 | Configured default, otherwise unlimited | Same default and optional maximum as GET; no selector-specific limit. |
| `offset` | integer ≥ 0 | `0` | Candidate records to skip; cannot be combined with `cursor`. |
| `cursor` | string | Omitted | Opaque continuation token; requires an explicit `limit`. |
| `count` | string | Omitted | `exact` includes the SQL-scoped candidate count before pagination. |

Field references remain snake_case. The GET grammar, maximum filter depth of 3,
budget of 100 filter nodes, and single-occurrence control rules apply.
`select` and `expand` are rejected with `422 unknown_parameter`: the output
projection is fixed. Other unsupported or legacy query parameters are rejected
as in GET. There are no source-resource, source-field, or special `ids`
parameters; a configured primary-key filter can resolve selected values, such
as `id=in.(42,41)` when `id` belongs to the filter allowlist.

`search` does not implicitly search `label_field`. A nonempty search requires
configured, readable search fields, as in GET. Ordering does not implicitly
sort by the display label. Cursor validation follows GET, including unchanged
filters/search/effective order and the restriction on nullable ordering fields.

#### Output

**200 OK** — `Content-Type: application/json`.

| Field | Type | Description |
| --- | --- | --- |
| `items` | object[] | Selectable candidates from this page; empty when none are available. |
| `items[].id` | scalar | Target primary-key value, using the same JSON type and safe-integer rules as GET. |
| `items[].label` | string | Readable `label_field` value converted to display text; primary-key text when no readable label is configured. |
| `page` | object | Same five keys as GET: `limit`, `offset`, `hasMore`, `nextCursor`, and `total`. |

A null or empty label also falls back to primary-key text. Labels are plain text;
clients render them as text, never HTML. Do not call the ORM object's `__str__`
or choose an arbitrary field as a fallback. Extra record fields and expansion
objects are not returned. Each option is unique by primary key.

Pagination metadata describes candidates after both SQL scopes and filters,
before per-record permission omissions. Consequently, a page may contain fewer
options than `page.limit`, including an empty page with `page.hasMore=true`.
Clients continue using pagination metadata rather than the number of returned
options. Advance offsets by `page.limit`; use offset pagination when more
candidates remain but no cursor is available. `page.total` is null unless
`count=exact`; `limit=0&count=exact` returns
no options and the candidate count. Offsets count candidates, not returned
options. Cursor tokens must not expose ordering fields hidden by field policy.

The endpoint always returns the envelope, including when
`API.list_envelope=False`; that legacy flag applies only to the record-list
endpoints. Responses include `X-Request-ID`, `Content-Language`,
`Cache-Control: private, no-cache`, and
`Vary: Cookie, Authorization, Accept-Language`, on success and errors.

**Errors** use the same `application/problem+json` envelope, camelCase properties,
and query error pointers as GET. `instance` is the `:queryOptions` request path;
`traceId` matches `X-Request-ID`.

| Status | Code | Description |
| --- | --- | --- |
| 401 | `unauthenticated` | Authentication is required. |
| 403 | `permission_denied` | List/retrieve permission is denied, or the primary key is not readable in the list context. |
| 422 | `validation_error`, `unknown_parameter` | Invalid query input, repeated controls, unsupported projection, or unavailable search. |
| 422 | `unknown_field`, `unsupported_operator` | Forbidden or unknown filter/sort field or operator. |
| 422 | `invalid_cursor`, `cursor_unsupported_ordering` | Invalid continuation or unsupported cursor ordering. |
| 500 | `internal_error` | Unexpected failure; internal exception details are hidden. |

#### Example

Search question labels and request two candidates with the total count. This
resource uses `question_text` as its label and search field and permits sorting
by `id`:

```http
GET /api/questions:queryOptions?search=favorite&order=id.desc&limit=2&count=exact HTTP/1.1
Accept: application/json
X-Request-ID: req-options-42
```

```http
HTTP/1.1 200 OK
Content-Type: application/json
Cache-Control: private, no-cache
Vary: Cookie, Authorization, Accept-Language
Content-Language: en
X-Request-ID: req-options-42

{
  "items": [
    {"id": 42, "label": "Favorite color?"},
    {"id": 41, "label": "Favorite language?"}
  ],
  "page": {
    "limit": 2,
    "offset": 0,
    "hasMore": true,
    "nextCursor": "eyJ2IjoxLC4uLn0.k3J9",
    "total": 137
  }
}
```

For the next page, keep the same search and order and send `offset=2`, or send
the returned `cursor` with `limit=2` and omit `offset`.
