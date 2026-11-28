# Model REST API

`ModelApi` builds typed FastAPI routes for a shared `ModelResource`. Register the
resource through Obox or include a subclass's router with
`ProductApi.as_router("/v0/product")`.

The collection supports GET and POST. `/{id}` supports GET, PATCH and DELETE.
The list route accepts `q`, `ordering`, and JSON `filter` on allowed fields.
`GET /{prefix}:groups` accepts `group` and uses the same scope and filters.
Both collection routes support `limit`, `offset` and `includeTotal`.
See the [query reference](reference/api.md#collection-parameters-and-response).

JSON request and response fields use camelCase. Declare a shared `write_schema`
on the Resource. PATCH validates submitted fields against the current record
and persists only the submitted fields.

Model operations require the corresponding model permission by default. Errors use
`application/problem+json`, with field validation reported as 422 and database
conflicts as 409.

## Custom actions and write hooks

| Task | Declaration |
| --- | --- |
| Choose CRUD endpoints and permissions | `ModelApi.actions` and `permissions` |
| Add a named API operation | `@route` on a `ModelApi` method |
| Add behavior shared by REST and HTML writes | `ModelResource.perform_create`, `perform_update` or `perform_destroy` |
| Reuse a business operation across routes, jobs and commands | `ModelResource.service_class` |

Register a callback from `Context` or a bound `ModelResource` using
`on_commit(fn, *args, **kwargs)`. Callbacks run in registration order after
the unit of work commits and closes its session. Snapshot values needed by
the callback before the write. Obox discards callbacks registered within
rolled-back savepoints and logs callback failures.

```python
async def perform_destroy(self, obj: Product) -> None:
    product_id = obj.id
    await super().perform_destroy(obj)
    self.on_commit(invalidate_product_cache, product_id)
```

| Delivery | Use |
| --- | --- |
| Best-effort notification or cache invalidation | `on_commit` with a short sync or async callback. HTTP callbacks complete before the response is sent. |
| Work that must survive a process restart | Await `obox.contrib.jobs.services.enqueue` with the current session inside the write transaction. |

## CSRF protection

For a cookie-authenticated write, fetch `GET /v0/auth/csrf`, keep its cookies
and pass the response's `csrfToken` as `X-CSRF-Token`. The token expires and is
bound to the identity and cookie values. Fetch a fresh token after signing in
or when the cookies change. The `/v0` segment follows `API_PREFIX`.

Scoped bearer-token requests can write without a CSRF token on routes that
accept them. Generic HTML form submissions use the cookie and masked token
described in [access control](features/access-control.md#csrf-protection).
