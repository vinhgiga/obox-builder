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
    prefix = "/api/products"
    search_fields = ("name",)
    ordering_fields = ("name", "created_at")
```

Register an API subclass through an installed app's `apis` sequence, or mount a
router returned by `ProductApi.as_router()` or
`ProductApi().build_router("/api/products")`.

## Generated routes

By default, Obox generates the following operations. `PUT` is included only
when `update_schema` is defined.

| Operation | Method and path | Success status | Default model permission |
| --- | --- | --- | --- |
| `list` | `GET /{prefix}` | `200` | `app.view_model` |
| `retrieve` | `GET /{prefix}/{id}` | `200` | `app.view_model` |
| `create` | `POST /{prefix}` | `201` | `app.add_model` |
| `update` | `PUT /{prefix}/{id}` | `200` | `app.change_model` |
| `patch` | `PATCH /{prefix}/{id}` | `200` | `app.change_model` |
| `delete` | `DELETE /{prefix}/{id}` | `204` | `app.delete_model` |

`app` and `model` in the permission names are the model's application label and
lower-case model name. `include_router_methods` can select a unique sequence of
`list`, `retrieve`, `create`, `update`, `patch`, and `delete`. An explicit
sequence replaces the default route set.

The `{id}` parameter is the model's primary key. Resources used by this API
require a single primary key of type `int`, `str`, or `UUID`. A successful
create response includes `Location` when the retrieve route is enabled.

## Class attributes

| Attribute | Default | Description |
| --- | --- | --- |
| `model` | required unless `resource` is set | SQLAlchemy model to expose. |
| `resource` | `None` | `ModelResource` class shared with HTML views. Set either `model` or `resource`, not both. |
| `prefix` | required | Absolute route prefix such as `/api/products`. |
| `slug` | `None` | Registration suffix used when mounted through `AppConfig`. |
| `include_router_methods` | `None` | Generated operations. Defaults to all except `update` when no `update_schema` is declared. |
| `permissions` | `{}` | Additional named permission required for an operation, keyed by operation name. |
| `read_schema` | generated | `ApiResponse` subclass used for list and retrieve results. |
| `create_schema` | generated from resource | `ApiRequest` subclass for create input. |
| `update_schema` | `None` | `ApiRequest` subclass for full `PUT` input; defining it enables `update` by default. |
| `patch_schema` | generated from resource | `ApiRequest` subclass for partial `PATCH` input. |
| `dependencies` | `()` | FastAPI dependencies applied to the generated router. |
| `search_fields` | `()` | Readable text fields available to list search, named with Python field names. |
| `ordering_fields` | `()` | Readable fields available to list ordering, named with Python field names. |
| `default_limit` | `50` | Default collection page size. |
| `max_limit` | `100` | Largest accepted collection page size. |

## Schemas

Obox generates a read schema from the resource's readable fields and generates
create and patch schemas from its writable fields when explicit schemas are not
provided. With a bare `model`, fields can be inferred from explicit API schemas;
otherwise use a `ModelResource` to declare the transport fields and shared policy.

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
    prefix = "/api/products"
    read_schema = ProductRead
    create_schema = ProductCreate
```

Use `include_router_methods` to expose only operations whose schemas and
permissions are configured for the application.

## Collection parameters and response

The list operation accepts:

| Parameter | Default | Constraints |
| --- | --- | --- |
| `limit` | `default_limit` | Integer from `1` through `max_limit`. |
| `offset` | `0` | Non-negative integer. |
| `includeTotal` | `false` | Include a count in `total` when `true`. |
| `search` | omitted | Available when `search_fields` is non-empty; at most 256 characters. |
| `order` | resource ordering | Available when `ordering_fields` is non-empty; comma-separated field aliases with optional `.asc` or `.desc`. |

Ordering accepts only fields listed in `ordering_fields`. Obox appends the
primary key as a stable tie-breaker when it is not part of the requested order.
Search fields must be readable SQL string columns; JSON fields cannot be used
for ordering.

The collection response is an object with `items`, `limit`, `offset`,
`hasMore`, and `total`. `total` is `null` unless `includeTotal=true`. `PATCH`
updates only fields present in the request body. `PUT` sends the configured
full update schema.

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
