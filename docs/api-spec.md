# Model REST API

`ModelApi` builds typed FastAPI routes for a shared `ModelResource`. Register the
resource through Obox or include `ModelApi().build_router(prefix=...)` directly.

The default routes are GET and POST on the collection, and GET, PUT, PATCH and
DELETE on `/{id}`. A generated PUT schema is inferred from writable model fields;
an explicit `update_schema` can define its request contract. PUT does not create
a record when the requested ID is unavailable.
The API returns a paginated object with `items`, `limit`, `offset`, `hasMore`
and `total`. Request `includeTotal=true` to include a count.

Schemas use camelCase by default. Use `read_schema`, `create_schema` or
`patch_schema` for an explicit transport contract. The resource still enforces
the selected fields, write permission and any shared business schema.

Model operations require the corresponding model permission. Cookie-authenticated
writes require `X-CSRF-Token`. The auth plugin can accept scoped bearer tokens.
Errors use `application/problem+json`, with field validation reported as 422
and database conflicts as 409.
