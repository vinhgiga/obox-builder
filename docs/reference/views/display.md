# Display views

Import `GridView`, `ListView`, `DetailView`, and `ViewConfig` from
`obox.views.generic`. Declare a `resource` or mapped `model`. Use generic
parameters such as `GridView[Product]` and `DetailView[Product]` to type the
records returned by these views.

## `GridView` and `ListView`

`GridView` renders a sheet with numbered rows. `ListView` renders aligned
columns with an Open action beside the primary field. Both views share the
same collection query and display the first field as the primary field.

| Attribute | Default | Description |
| --- | --- | --- |
| `fields` | `None` | Displayed field names in order. Defaults to resource-readable fields. |
| `search_fields` | `()` | Fields searched by `q`. Prefix a name with `^` for starts-with or `=` for exact matching. |
| `page_size` | `None` | Uses the resource size or 50. HTML pages have a maximum of 100 records. |
| `view` | `ViewConfig()` | Default filter, sort, grouping, and browser layout. |
| `actions` | `()` | Bulk action names or callables. A collection with actions accepts `POST`. |
| `editable` | `()` | Reserved for inline editing in v1.1. |
| `slug` | `"grid"` or `"list"` | URL key when registering an additional collection view. |

`get_queryset()` supplies a SQLAlchemy `Select`. Put security scopes on
`ModelResource.get_queryset()` so they apply to every collection and record
lookup. Column visibility and ordering in the browser are presentation settings.
The primary field joins the configured layout at the beginning and stays visible.

### Collection query

| Parameter | Behavior |
| --- | --- |
| `filter` | JSON group with `conjunction` (`and` or `or`) and `children`. Children are groups or `{field, op, value}` terms. Maximum depth 3, 50 nodes, and 32,000 characters. |
| `<field>` or `<field>__exact` | Equality conditions combined with AND when `filter` is empty or absent. |
| `ordering` | Comma-separated names with optional `-` for descending. Maximum five unique fields. |
| `group` | Same syntax as `ordering`, with at most three unique fields. Group fields are sorted first. |
| `q` | At most ten words. Each word must match a configured search field. |
| `page` | Page number beginning at `1`. An out-of-range page returns `404`. |

A non-empty `filter` parameter controls the complete filter, including an
explicit group with no children. If the URL supplies neither filter form, the
view uses `ViewConfig.filter`. Sort and group URL parameters override their
defaults. Without a view sort or URL ordering, the collection uses resource
ordering. Query controls can use readable scalar fields hidden from the layout.
Invalid query input returns `422`. Group counts use the full filtered result.

Custom templates receive `object_list`, `rows`, `columns`, `query`, `layout`,
`page`, `total`, `actions`, `add_url`, and pagination links. The default template
suffixes are `_grid.html` and `_list.html`. Set `template_name` for a separate
template.

### Bulk actions

Use `@action` and include the method in `actions`. A method receives the view,
request, and the selected SQLAlchemy query. The default permission is `change`.
Specify permissions such as `permissions=("delete",)` when needed. The built-in
`delete_selected` action requires a resource.

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

`get_queryset()` supplies the record query and `await get_object()` returns the
typed record. The default detail template suffix is `_detail.html`.
