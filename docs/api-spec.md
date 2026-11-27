# Model REST API

`ModelApi` builds typed FastAPI routes for a shared `ModelResource`. Register the
resource through Obox or include `ModelApi().build_router(prefix=...)` directly.

The default routes are GET and POST on the collection, and GET, PUT, PATCH and
DELETE on `/{id}`. A generated PUT schema is inferred from writable model fields;
an explicit `update_schema` can define its request contract. PUT does not create
a record when the requested ID is unavailable.
The API returns a paginated object with `items`, `limit`, `offset`, `hasMore`
and `total`. Request `includeTotal=true` to include a count.
The list route accepts `filter`, `search`, and `order` on allowed readable
fields, and caps the requested `limit` at the configured maximum. The
`GET /{prefix}:groups` route uses list permissions and filters to count
records in each group. See the [query reference](reference/api.md#collection-parameters-and-response).

Schemas use camelCase by default. Use `read_schema`, `create_schema` or
`patch_schema` for an explicit transport contract. The resource still enforces
the selected fields, write permission and any shared business schema.

Model operations require the corresponding model permission. Errors use
`application/problem+json`, with field validation reported as 422 and database
conflicts as 409.

## CSRF protection

For a cookie-authenticated write, fetch `GET /v0/auth/csrf`, keep its cookies
and pass the response's `csrfToken` as `X-CSRF-Token`. The token expires and is
bound to the identity and cookie values. Fetch a fresh token after signing in
or when the cookies change. The `/v0` segment follows `API_PREFIX`.

Scoped bearer-token requests can write without a CSRF token on routes that
accept them. Generic HTML form submissions use the cookie and masked token
described in [access control](features/access-control.md#csrf-protection).
