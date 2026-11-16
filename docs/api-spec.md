# REST contract — v0.1.0

`obox.rest_framework` implements a subset of DRF 3.16.1 using native FastAPI
requests and SQLAlchemy Select statements. REST registration is independent of
ModelBuilder. See [mapping and test coverage](mapping.md).

## Declaration and lifecycle

Declare `Serializer` fields or `ModelSerializer.Meta.model` plus `fields`/`exclude`.
Use `serializer_class` and an unevaluated `queryset` on `ModelViewSet`; register it
with `SimpleRouter`, then include `router.urls` in the URL configuration.

Database-aware validation and persistence are asynchronous:
`await serializer.is_valid(raise_exception=True)`, `await serializer.save()`.
Fields, `validate_<field>()`, `validate()` and create/update hooks follow DRF
responsibilities. Reading errors before validation raises an assertion.
Serialization uses declared field names and native ID types. Unknown request
properties are ignored as in DRF. Read-only values are not written.

GenericAPIView supports `get_queryset()`, `get_object()`, `get_serializer()`,
`get_serializer_context()`, `filter_queryset()` and pagination hooks.
ModelViewSet provides CRUD and `perform_create()`, `perform_update()` and
`perform_destroy()` hooks. View and serializer instances belong to one request.

## Default CRUD behavior

| Method | Router URL | Action | Success |
| --- | --- | --- | --- |
| GET | `/products/` | list | 200, JSON array |
| POST | `/products/` | create | 201, serialized object |
| GET | `/products/{pk}/` | retrieve | 200, serialized object |
| PUT | `/products/{pk}/` | update | 200, full validation |
| PATCH | `/products/{pk}/` | partial_update | 200, supplied fields only |
| DELETE | `/products/{pk}/` | destroy | 204, empty body |

SimpleRouter uses trailing slashes by default; pass `trailing_slash=False` to
remove them. `basename` supplies URL names. `@action(detail=..., methods=[...])`
adds DRF-shaped custom action routes. Request bodies use JSON.

Validation failures return 400 with field-to-message-list errors. Object errors
use `non_field_errors`. API errors use `{'detail': ...}`. Missing objects return
404; unsupported methods return 405. Authentication failures use 401 with a
challenge or 403 depending on the first authenticator, following DRF.

Defaults are SessionAuthentication, BasicAuthentication, AllowAny, no filtering
and no pagination. `REST_FRAMEWORK` overrides these. Session authentication
requires CSRF for unsafe requests. Basic authentication delegates credentials to
the installed auth backend. Permissions include AllowAny, IsAuthenticated,
IsAdminUser, IsAuthenticatedOrReadOnly and DjangoModelPermissions. Object-level
permission hooks run after lookup; restrict list visibility through get_queryset.

SearchFilter supports `search` and configured `search_fields`; OrderingFilter
supports `ordering` and allowed `ordering_fields`. PageNumberPagination is opt-in:

```json
{"count": 25, "next": "http://test/products/?page=2", "previous": null, "results": []}
```

Set `PAGE_SIZE`, or subclass PageNumberPagination and configure `page_size`.
The default unpaginated list remains a JSON array.

## Transactions and boundary

Request database work shares an asynchronous transaction. Validation does not
persist. Exceptions roll back creates, updates and deletes, including writes made
in perform hooks. SQLAlchemy manages uniqueness and foreign-key constraints;
application migrations own the schema.

This subset has JSON responses and scalar serializers. Django ORM, browsable API,
renderer/parser negotiation suites, nested writable serializers, serializer
relations and bulk update are not included.

Batch/upsert, metadata, custom query expressions, camelCase product envelopes,
ETag/If-Match rules and resource editor protocols belong to
[contrib resources](contrib-resource-api.md). Core CRUD has no implicit dependency
on those features.
