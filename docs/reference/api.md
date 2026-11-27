# Model REST API

`ModelApi` builds typed FastAPI routes around a SQLAlchemy model or a shared
`ModelResource`. A resource keeps record visibility, writable fields, validation, and
write policy consistent between HTML views and JSON operations.

```python
from obox.api import ModelApi
from obox.resources import ModelResource

from .models import Product


class Products(ModelResource[Product]):
    model = Product
    fields = ("id", "name", "price", "created_at")
    ordering = ("name",)


class ProductApi(ModelApi[Product]):
    resource = Products
    prefix = "/v0/product"
    search_fields = ("name",)
    ordering_fields = ("name", "created_at")
```

Register an API subclass through an installed app's `apis` sequence, or mount a
router returned by `ProductApi.as_router()` or
`ProductApi().build_router("/v0/product")`.

## Generated routes

`obox.register(Product)` uses `API_PREFIX` (default `/v0`) and the model's
table name. For `__tablename__ = "product"`, use `/v0/product`. Set `slug`
or `api_suffix` on the registration to override the API suffix.
Registration through `AppConfig` can also include the app's API prefix.

By default, Obox generates the following operations. When `update_schema` is
omitted, `PUT` uses a schema inferred from the resource's writable fields.

| Operation | Method and path | Success status | Default model permission |
| --- | --- | --- | --- |
| `list` | `GET /{prefix}` | `200` | `app.view_model` |
| Groups | `GET /{prefix}:groups` | `200` | Same as `list` |
| `retrieve` | `GET /{prefix}/{id}` | `200` | `app.view_model` |
| `create` | `POST /{prefix}` | `201` | `app.add_model` |
| `update` | `PUT /{prefix}/{id}` | `200` | `app.change_model` |
| `patch` | `PATCH /{prefix}/{id}` | `200` | `app.change_model` |
| `delete` | `DELETE /{prefix}/{id}` | `204` | `app.delete_model` |

`app` and `model` in the permission names are the model's application label and
lower-case model name. `include_router_methods` can select a unique sequence of
`list`, `retrieve`, `create`, `update`, `patch`, and `delete`. An explicit
sequence replaces the default route set.
The groups route is generated with `list` when at least one field can be grouped.

The `{id}` parameter is the model's primary key. Resources used by this API
require a single primary key of type `int`, `str`, or `UUID`. A successful
create response includes `Location` when the retrieve route is enabled.

## Class attributes

| Attribute | Default | Description |
| --- | --- | --- |
| `model` | required unless `resource` is set | SQLAlchemy model to expose. |
| `resource` | `None` | `ModelResource` class shared with HTML views. Set either `model` or `resource`, not both. |
| `prefix` | required | Absolute route prefix such as `/v0/product`. |
| `slug` | `None` | Registration suffix used when mounted through `AppConfig`. |
| `include_router_methods` | `None` | Generated operations. Defaults to all operations, except page pagination which defaults to list and retrieve. |
| `permissions` | `{}` | Additional named permission required for an operation, keyed by operation name. |
| `read_schema` | generated | `ApiResponse` subclass used for list and retrieve results. |
| `create_schema` | generated from resource | `ApiRequest` subclass for create input. |
| `update_schema` | `None` | Optional `ApiRequest` subclass for `PUT` input; otherwise generated from writable resource fields. |
| `patch_schema` | generated from resource | `ApiRequest` subclass for partial `PATCH` input. |
| `dependencies` | `()` | FastAPI dependencies applied to the generated router. |
| `search_fields` | `None` | Readable text fields. Prefix a field with `^` for starts-with or `=` for exact matching. |
| `ordering_fields` | `None` | Readable scalar fields. |
| `filter_fields` | `None` | Readable scalar fields and attachments. |
| `group_fields` | `None` | Readable strings, enums, booleans, integers, and dates. |
| `default_limit` | `50` | Default collection page size. |
| `max_limit` | `100` | Cap for the collection and groups result size. |

Use Python field names in these declarations. `None` infers fields from the
read schema and `()` disables that control. The API accepts their response
aliases in `filter`, `order`, and `by`. JSON fields are excluded from all
four controls. Attachments support `is.null` and `not.is.null` in filters.

## Schemas

Obox generates a read schema from the resource's readable fields and generates
create, update, and patch schemas from its writable fields when explicit schemas
are not provided. With a bare `model`, fields can be inferred from explicit API
schemas; otherwise use a `ModelResource` to declare the transport fields and
shared policy.

Read schemas must subclass `ApiResponse`. Write schemas must subclass
`ApiRequest`. Both base classes use camelCase aliases at the JSON boundary;
application code continues to use Python field names. Write schemas accept
aliases, reject unknown fields, and may only name readable, writable resource
fields. A read schema may only name resource fields that are exposed to the API.

```python
from decimal import Decimal

from obox.api import ApiRequest, ApiResponse


class ProductRead(ApiResponse):
    id: int
    name: str
    price: Decimal


class ProductCreate(ApiRequest):
    name: str
    price: Decimal


class ProductApi(ModelApi[Product]):
    resource = Products
    prefix = "/v0/product"
    read_schema = ProductRead
    create_schema = ProductCreate
```

Use `include_router_methods` to expose only operations whose schemas and
permissions are configured for the application.

## Collection parameters and response

The list operation accepts:

| Parameter | Default | Constraints |
| --- | --- | --- |
| `limit` | `default_limit` | Positive integer. Values above `max_limit` are capped. |
| `offset` | `0` | Integer from `0` through `2147483647`. |
| `includeTotal` | `false` | Include a count in `total` when `true`. |
| `filter` | omitted | Up to 4,000 characters and 50 conditions or groups. |
| `search` | omitted | Up to 256 characters and ten whitespace-separated words. |
| `order` | resource ordering | Up to five unique comma-separated field aliases with optional `.asc` or `.desc`. |

Each search word must match at least one searchable field. Search is
case-insensitive and matches substrings by default. Case conversion follows
the database, so SQLite folds ASCII by default. Search and filter both narrow
the same record selection. An explicit `order` uses ascending order when the
direction is omitted and puts nulls last in both directions. The primary key
breaks sort ties and provides a default order when the resource has none.

The collection response is an object with `items`, `limit`, `offset`,
`hasMore`, and `total`. Advance the offset by the returned `limit` when
`hasMore` is true. `total` is `null` unless `includeTotal=true`.
Numbered lists cap `page_size` at `max_page_size` and return the effective size.

`PATCH` and a generated `PUT` update only fields present in the request body.
An explicit `update_schema` defines the `PUT` request contract.

### Filter expressions

Write conditions as `field.operator.value`. Commas at the top level join
conditions with AND. Use `and(...)`, `or(...)`, `not.and(...)`, or
`not.or(...)` for up to two explicit levels. Percent-encode query values.
Quote literals containing commas, parentheses, or double quotes, and escape
quotes and backslashes inside quoted literals.

| Operators | Supported fields and behavior |
| --- | --- |
| `eq`, `neq`, `isdistinct` | Scalars. `isdistinct` includes nulls when comparing to a non-null value. |
| `gt`, `gte`, `lt`, `lte` | Numbers, dates, datetimes, and times. |
| `in.(one,two)` | Scalars except booleans. Accepts up to 100 values. `in.()` matches no rows. |
| `like`, `ilike` | Text. `*` and `%` match any sequence, `_` matches one character, and `\` escapes the next character. |
| `is.null`, `is.empty` | `null` tests nulls. On text, `empty` matches both nulls and empty strings. |
| `is.true`, `is.false` | Booleans. |

Prefix an operator with `not.` to negate it. `neq` and `not.eq` exclude null
rows. On a text field, `eq.null` matches the literal string `null`.

Dates use `YYYY-MM-DD`. Datetimes use an RFC 3339 timestamp with an offset, or
a date representing the UTC interval from midnight up to the next midnight.
`eq` and `in` match within the interval, `neq` matches outside it, and
`isdistinct` also matches nulls. For date-only ranges, `gte` and `lt` compare to
the start of the day. `gt` and `lte` compare to the start of the next day.
Times use `HH:MM` or `HH:MM:SS` with optional microseconds and are interpreted
as UTC for time columns with an offset.

```text
GET /api/customers?filter=country.in.(USA,Canada),state.not.is.null&order=lastName.asc
GET /api/invoices?filter=invoiceDate.eq.2021-01-01
```

### Groups

`GET /api/customers:groups?by=country,state&order=count.desc&limit=20`
returns one item per distinct combination of the one to three aliases in
`by`. Each item contains a `key` object with all requested aliases and a
record `count`. Filters, search, and list permissions apply to records before
grouping. The group key distinguishes nulls and empty strings.

Use `order=key.asc` (default), `key.desc`, `count.asc`, or `count.desc`.
Keys compare in the order listed in `by`, with nulls last at each position.
Count ties use ascending keys. The `limit`, `offset`, and `hasMore` response
fields apply to groups. `includeTotal=true` adds `total` with the number of
groups before pagination. To fetch records for a group, send its key as a
list filter, for example `filter=country.eq.USA,state.is.null`.

## Permissions, authentication, and errors

Every generated operation requires the corresponding model permission shown
above. Add a second permission for an operation with `permissions`, or add
FastAPI dependencies to the router with `dependencies`. The request context is
available to overrides through `Context`.

Cookie-authenticated writes require the Obox CSRF token in `X-CSRF-Token`. When
the Auth feature enables API tokens, bearer tokens can be used for JSON API
requests and are checked against their scopes.

Errors use `application/problem+json`. Invalid request data returns `422`,
missing or unavailable records return `404`, denied access returns `403`, and
database or resource conflicts return `409`. See the [REST contract](../api-spec.md)
for the complete response contract. Generated model responses use
`Cache-Control: private, no-store`.

## Extension hooks

Override these methods on a `ModelApi` subclass to customize operations. They
may be synchronous or asynchronous; database access is asynchronous.

| Hook | Description |
| --- | --- |
| `configure_router(router)` | Add custom FastAPI endpoints before generated model routes. |
| `get_queryset(ctx, *, action)` | Return the starting SQLAlchemy `Select` for an operation. The default uses the bound resource query. |
| `list(ctx, queryset, *, limit, offset, include_total)` | Return rows, `has_more`, and optional total count. |
| `retrieve(ctx, obj)` | Customize a loaded object before serialization. |
| `create(ctx, values)` | Create a record from validated writable fields. |
| `update(ctx, obj, values)` | Apply full update values. |
| `patch(ctx, obj, values)` | Apply partial update values; defaults to `update(..., partial=True)`. |
| `delete(ctx, obj)` | Delete a loaded record. |

`BaseApi.permission_required(permission=None)` returns the API context
dependency for routes that need an active identity and optional named
permission.
