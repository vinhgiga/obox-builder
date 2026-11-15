# Query a resource

Assume `QuestionBuilder` is registered in the app whose label is `polls`. A
list request can select fields, order results, and ask for a page:

```http
GET /api/polls/questions?select=questionText&order=questionText&limit=20
```

The list response contains `items` and a `page` object. If more records are
available, use its `nextCursor` value in the next request. Cursors are opaque
and tied to the query.

Create a record with public camelCase field names:

```http
POST /api/polls/questions
Content-Type: application/json

{"questionText": "What is Obox Builder?"}
```

The caller must have permission for the operation and fields. Unknown request
properties are rejected. Auth JSON sessions are enabled by default when the
Auth plugin is installed.

See the [Resource API contract](../../api-spec.md) for filters, expansions,
batch writes, errors, and concurrency headers.
