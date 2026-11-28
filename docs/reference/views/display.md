# Display views

Import `GridView`, `DetailView`, and `ViewConfig` from `obox.views.generic`.
Set `resource` to a `ModelResource` class for standalone routes, or configure
the view through a `ModelViewSet`.

## `GridView`

`GridView` renders a sheet with numbered rows. Set `fields` to select and order
the displayed columns, `page_size` to cap each page, and `template_name` for a
custom layout. `ViewConfig` supplies default `filter`, `sort` and `group` values.
URL parameters override these values. Use `filter=` to clear the default filter.

Grid uses the shared [collection query](../api.md#collection-parameters-and-response)
for `q`, `ordering`, `filter` and `group`, and uses `page` for the page number.
Set query permissions on the Resource. Group counts describe the full scoped
and filtered collection. Invalid queries return 422 and an unavailable page
returns 404.

Templates receive `rows`, `columns`, `query`, `page`, `total`, `object_list`,
`add_url`, and the previous and next page URLs. The default template suffix is
`_grid.html`.

## `DetailView`

Use `fields` to choose and order readable values. The default is every readable
field. The context contains `object`, serialized `values`, `change_url`, and
`delete_url`. The built-in edit and delete links require their registered route,
the corresponding model permission, and the resource's `can()` approval.

Direct detail, update, and delete routes accept one `{pk}` path parameter. Its
value uses `ModelResource.lookup_field`, which defaults to the primary key.
Records absent from the resource scope return `404`. A denied action on an
available record returns `403`. Direct routes can supply their model permission
dependency via `as_route()` or FastAPI.

The Resource supplies the record query and `await get_object()` returns the
typed record. The default detail template suffix is `_detail.html`.
