# REST and product APIs

Use the [serializer → viewset → router example](../examples/apis/query-resource.md)
for standardized REST endpoints. Default URLs have a trailing slash; serializer
names and types control input/output. Validation errors are400 and DELETE is204.
Pagination is disabled until explicitly configured.

Enable contrib resources for custom resource queries, metadata, batch/upsert,
ETag-based concurrency and inline editor APIs. Those product endpoints retain
[their own protocol](../contrib-resource-api.md), independently of REST registration.
