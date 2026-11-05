# OBOX API Specification

**Status:** This document describes the proposed HTTP contract. Availability depends on the
implementation phase, enabled resource operations, plugins, and permissions.
The [core specification](core-spec.md) defines the shared service and policy contracts.

## Endpoint overview

Paths use the proposed `/api` prefix and have no trailing slash.
`{slug}` is a resource's plural slug, such as `questions`; `{id}` is its primary
key; `{name}` is a registered action name. Resource API prefixes may be customized.

### Resources

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/{slug}` | List records. |
| POST | `/api/{slug}` | Create a record. |
| GET | `/api/{slug}/{id}` | Retrieve a record. |
| PATCH | `/api/{slug}/{id}` | Update selected fields of a record. |
| DELETE | `/api/{slug}/{id}` | Delete a record. |
| POST | `/api/{slug}/_query` | List records using a JSON query. |
| GET | `/api/{slug}/_options` | List record IDs and labels for relationship selectors. |
| GET | `/api/_meta` | Describe resources available to the current principal. |
| GET | `/api/{slug}/_meta` | Describe a resource's fields, permissions, and operations. |
| POST | `/api/{slug}/actions/{name}` | Run an action on selected records. |

`_query` is planned for P1; `_options` and `_meta` are planned for P2.

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
| POST | `/api/{slug}/_batch` | Apply create, update, delete, or upsert operations atomically. |
| GET | `/api/_events` | Stream resource change notifications. |

CSV export uses the list path with `Accept: text/csv`. Long-running operations
may return a task location at `/api/_tasks/{id}`; the task endpoint's method
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
| `cursor` | string | Omitted | Opaque token from `page.next_cursor`; requires an explicit `limit`. |
| `count` | string | Omitted | Set to `exact` to include the total matching record count. |
| `select` | string | All readable API fields | Comma-separated field names, such as `id,question_text`; `*` selects all readable API fields. The primary key is always included. |
| `expand` | string | Omitted | Comma-separated registered FK relationships whose IDs and labels appear in `_expanded`. One level only. |
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
`_expanded.<relationship>: {"id": ..., "label": ...}`. A related record outside
the target resource's scope produces `null`; an unregistered target is rejected.

#### Output

**200 OK** — `Content-Type: application/json`.

| Field | Type | Description |
| --- | --- | --- |
| `items` | object[] | Matching records with permitted fields; empty when no records match. |
| `page` | object | Pagination metadata; every key below is always present. |
| `page.limit` | integer or null | Effective limit; null when unpaginated. |
| `page.offset` | integer | Applied offset; defaults to zero. |
| `page.has_more` | boolean | Whether more matching records remain. |
| `page.next_cursor` | string or null | Continuation token; null when exhausted or cursor pagination is unsupported. |
| `page.total` | integer or null | Count after filtering and row scoping, before pagination; null unless `count=exact`. |

Field names use snake_case. Decimal and BigInteger values are strings;
integer primary keys exceeding JavaScript's safe integer range are also strings.
Dates and times use ISO 8601; datetimes use UTC with a `Z` suffix.

Response headers include `X-Request-ID`, `Cache-Control: private, no-cache`,
and `Vary: Cookie, Authorization, Accept-Language`.

**Errors** use `Content-Type: application/problem+json` with
`type`, `title`, `status`, `code`, `detail`, `instance`, `trace_id`, and a flat
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
    {"id": 42, "question_text": "Favorite color?", "pub_date": "2026-03-01T09:00:00Z"},
    {"id": 41, "question_text": "Favorite language?", "pub_date": "2026-02-20T09:00:00Z"}
  ],
  "page": {
    "limit": 2,
    "offset": 0,
    "has_more": true,
    "next_cursor": "eyJ2IjoxLC4uLn0.k3J9",
    "total": 137
  }
}
```

For the next offset page, repeat the query with `offset=2`. For cursor
continuation, send the returned opaque token as `cursor`, keep `limit=2` and
the same filters and effective order, and omit `offset`.
