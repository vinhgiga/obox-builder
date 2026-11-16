# HTTP APIs

The standardized REST API is declared through serializers, viewsets and routers.
Read [the REST contract](../api-spec.md) for defaults, errors, statuses and URLs.
It uses serializer field names, native IDs, trailing slashes and optional DRF
page-number pagination.

The optional [contrib resource protocol](../contrib-resource-api.md) owns custom
queries, batch/upsert, metadata and record concurrency. Its camelCase envelopes
and custom method URLs remain product-specific. Installing it is explicit.
