# Release notes — 0.1.0

Obox uses `ModelResource` to share model CRUD, validation and access rules across
HTML and native FastAPI REST routes.

## Migration

1. Replace the old site registration and URLconf with `obox.register(Model)`
   or register in `AppConfig.ready()` for app-prefixed routes. Attach custom
   views and routers to FastAPI with a named permission.
2. Put shared validation, field selection and visibility on a `ModelResource`.
   Attach HTML customizations with `list_view=`, `detail_view=`, `create_view=`,
   `update_view=` and `delete_view=`, and a REST adapter with `api=ModelApiSubclass`.
3. Choose a surface using `views=False` or `api=False`. Check the new
   collection URLs and the REST OpenAPI contract. PUT uses a generated schema
   from writable model fields unless an explicit `update_schema` is provided.
4. Replace staff checks with operation permissions assigned through roles.
   The current superuser flag supplies administrator access.
5. Construct FastAPI with `openapi_url=None`. To publish the schema, add a
   route serving `app.openapi()` with a named permission.

Registered HTML writes redirect with 303. The refactor uses the existing
database schema. See the [quickstart](examples/quickstart.md) and
[Python API](reference/python-api.md).
