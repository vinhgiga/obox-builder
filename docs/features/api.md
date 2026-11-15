# JSON API

Each registered resource exposes a typed JSON API. Routes use the configured
prefix (`/api` by default) and the collection name. When an app label is
configured, it appears in the path, for example `/api/polls/questions`.

## Resource operations

The standard operations are list, retrieve, create, update, and delete. A
builder can disable operations or expose a narrower set. Additional routes
support structured queries, relationship selectors, resource descriptions,
registered actions, and atomic batch writes.

| Method | Path | Use |
| --- | --- | --- |
| `GET` | `/api/{app}/{collection}` | List records |
| `POST` | `/api/{app}/{collection}` | Create a record |
| `GET` | `/api/{app}/{collection}/{id}` | Retrieve one record |
| `PATCH` | `/api/{app}/{collection}/{id}` | Update selected fields |
| `DELETE` | `/api/{app}/{collection}/{id}` | Delete a record |
| `POST` | `/api/{app}/{collection}:query` | Run a structured query |
| `GET` | `/api/{app}/{collection}:describe` | Describe fields, operations, and permissions |

Paths omit the `{app}/` portion when resources are registered outside an
application catalog. The [API contract](../api-spec.md) lists all implemented
and planned endpoints separately.

## Naming and schemas

Python and SQLAlchemy fields use `snake_case`. JSON property names use
`camelCase`; for example, `question_text` is sent as `questionText`. Request
models reject unknown properties. Response and error payloads have explicit
Pydantic schemas and OpenAPI definitions.

## Lists and queries

List routes support configured search and filters, field projection, ordering,
offset or signed cursor pagination, and relationship expansion. A continuation
cursor is opaque and tied to its query. Example:

```http
GET /api/polls/questions?select=questionText&order=questionText&limit=20
```

`API.default_limit` and `API.max_limit` default to `None` in the library and can
opt in to result limits. The generated scaffold sets `API_DEFAULT_LIMIT=100`
and `API_MAX_LIMIT=1000`. Configure limits for the workload and infrastructure
instead of assuming the API imposes a universal page-size cap.

## Writes and concurrency

Create and update requests accept only writable, permitted fields. Batch writes
reuse resource validation and permission checks within one transaction. For
models configured with SQLAlchemy `version_id_col`, resource update and delete
routes use strong ETags and require `If-Match` to prevent stale writes.

For request and response shapes, filters, error codes, upload endpoints, and
batch behavior, see the [full Resource API reference](../api-spec.md). For
record-page updates and comments, see [Record and Activity API](../record-activity-spec.md).
