# Model REST API

`ModelApi` exposes a `ModelResource` through a typed FastAPI router. Register a
model or Resource with `obox.register()`, or mount a custom adapter with
`ProductApi.as_router("/v0/product")`.

## Generated routes

`obox.register(Product)` uses the table name under `API_PREFIX`, which defaults
to `/v0`. For a table named `product`, the collection is `/v0/product`. Set
`slug` to choose another suffix.

| Action | Method and path | Success | Model permission |
| --- | --- | --- | --- |
| `list` | `GET /v0/product` | 200 | `view` |
| `list` (groups) | `GET /v0/product:groups` | 200 | `view` |
| `retrieve` | `GET /v0/product/{id}` | 200 | `view` |
| `create` | `POST /v0/product` | 201 | `add` |
| `patch` | `PATCH /v0/product/{id}` | 200 | `change` |
| `delete` | `DELETE /v0/product/{id}` | 204 | `delete` |

Response objects and request body fields use camelCase. Set `write_schema` on
the Resource to customize creation and patch validation. PATCH combines the
submitted values with the current record for validation, then writes only the
submitted fields. Create responses include a `Location` header when the
retrieve route is enabled.

## Collection parameters and response

| Parameter | Behavior |
| --- | --- |
| `q` | Up to ten search words in the Resource's search fields |
| `ordering` | List route. Up to five comma-separated field names, prefixed with `-` for descending |
| `filter` | JSON filter group with `conjunction` (`and` or `or`) and `children` |
| `group` | Groups route. One to three comma-separated field names, prefixed with `-` for descending |
| `limit`, `offset` | Pagination. The default limit is 50 and the maximum is 100 |
| `includeTotal` | Request a total count |

Use Python field names in these parameters. The Resource defines the allowed
fields through `search_fields`, `ordering_fields`, `filter_fields`, and
`group_fields`. Its `get_queryset()` applies the scope before the supplied
conditions. Grid uses the same filter, sorting, searching and grouping rules.

For example, URL-encode the following JSON as the `filter` value.

```json
{"conjunction":"and","children":[{"field":"country","op":"in","value":["Canada","USA"]}]}
```

Filter children are terms or nested groups. The limit is three levels, fifty
nodes, and 32,000 characters. Supported operators depend on the type and
include `is`, `is_not`, `contains`, `does_not_contain`, `gt`, `gte`, `lt`, `lte`,
`is_empty`, `is_not_empty`, and `in`. Supply a nonempty JSON array of up to
100 scalar values for `in`. Use `is_empty` or `is_not_empty` with `null` for
empty-value checks.

List responses contain `items`, `limit`, `offset`, `hasMore`, and `total`.
The groups route uses the same filters and returns `by` and an `items` array
with a `key` and `count` for each group. It includes `total` when requested.

## Customization and permissions

Set `ModelApi.actions` to the CRUD actions you want to expose. Each entry in
`permissions` replaces that action's default model permission. Use a string
for one permission or a tuple to require every permission in the tuple.
The `list` permission also applies to `:groups`.

Use `PUBLIC` from `obox.api` for public `list` and `retrieve` actions.
Limit anonymous visibility through the Resource's `get_queryset()` and
`fields`. A custom principal dependency should return an anonymous identity
when credentials are absent.

Declare extra methods with `@route.get`, `@route.post`, `@route.put`,
`@route.patch` or `@route.delete` on the `ModelApi` subclass. The decorator
takes a path relative to the model's API prefix, `permission=` and FastAPI
route options such as `status_code`. A required `ctx: Context` parameter
receives the request context. A GET action supports HEAD and accepts
`permission=PUBLIC`. Custom actions are registered before generated CRUD
routes. Store per-request state on the context or in method locals. See the
[custom action example](../examples/apis/custom-action.md).

Set `default_limit` or `max_limit` to change pagination. Use
`api_context(permission=..., csrf=True)` on standalone FastAPI routes that
need the Obox context.

Cookie-authenticated writes require the CSRF token in `X-CSRF-Token`. Scoped
bearer tokens follow their granted permissions. Errors use
`application/problem+json`. Invalid input returns 422, unavailable records
return 404, denied access returns 403, and write conflicts return 409.
