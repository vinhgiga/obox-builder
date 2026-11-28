# Release notes — 0.1.0

Obox uses `ModelResource` to share model CRUD, validation and access rules across
HTML and native FastAPI REST routes.

## Migration

1. Replace the old site registration and URLconf with `obox.register(Model)`
   or register in `AppConfig.ready()` for app-prefixed routes. Attach custom
   views and routers to FastAPI with a named permission.
2. Put shared validation, field selection and visibility on a `ModelResource`.
   Attach HTML customizations through `ModelViewSet` in `AppConfig.views` and
   REST customizations through `AppConfig.apis`.
3. Choose a surface using `views=False` or `api=False`. Direct registration
   uses the model class name for HTML and the table name under `/v0` for REST.
   A `Product` with table `product` uses `/product` and `/v0/product`.
   Declare `write_schema` once for both create and PATCH.
4. Replace staff checks with operation permissions assigned through roles.
   The current superuser flag supplies administrator access.
5. Construct FastAPI with `openapi_url=None`. To publish the schema, add a
   route serving `app.openapi()` with a named permission.

### Forms, Grid and REST

- Use `Form[S]` with `field_options` for HTML controls and
  `ModelResource.create()` or `update()` for writes. Inline formsets and
  automatic ORM relationship selectors have been removed.
- Replace `ListView` and secondary collection layouts with `GridView`.
  Move query fields to the Resource and move bulk operations to application
  routes with their own permissions.
- Use `q`, `ordering` and a JSON `filter` for REST lists. The `:groups` route
  uses `group`. Query field names use Python casing. Use the action names
  `patch` and `delete` in `ModelApi` declarations.
- Use `api_context(permission=..., csrf=True)` for custom REST actions
  requiring a bound request context.

Invalid registered HTML forms return 422 and successful writes redirect with
303. See [access control](features/access-control.md#csrf-protection) for the
tokens required by generic forms, Auth views and cookie-authenticated REST
clients. Framework static assets are served under `/_static/` relative to the
UI prefix. Use `STATIC_ROOT` to configure a directory for collected assets.

See the [quickstart](examples/quickstart.md) and [Python API](reference/python-api.md).
