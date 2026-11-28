# Model REST API

`ModelApi` builds typed FastAPI routes for a shared `ModelResource`. Register the
resource through Obox or include `ModelApi().build_router(prefix=...)` directly.

The collection supports GET and POST. `/{id}` supports GET, PATCH and DELETE.
The list route accepts `q`, `ordering`, and JSON `filter` on allowed fields.
`GET /{prefix}:groups` accepts `group` and uses the same scope and filters.
Both collection routes support `limit`, `offset` and `includeTotal`.
See the [query reference](reference/api.md#collection-parameters-and-response).

JSON request and response fields use camelCase. Declare a shared `write_schema`
on the Resource. PATCH validates submitted fields against the current record
and persists only the submitted fields.

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
