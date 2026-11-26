# Display views

`ListView` and `DetailView` combine a SQLAlchemy `Select` with a template
response. Import them from `obox.views.generic`.

## `ListView`

`ListView` gets its records from `resource`, `queryset`, `model`, or an overridden
`get_queryset()`. When a `ModelResource` is registered, its query scope and readable
fields apply to the HTML list. Put record visibility rules on the `ModelResource`
when they should also apply to REST routes.

| Attribute | Default | Description |
| --- | --- | --- |
| `model` | `None` | SQLAlchemy model to select. |
| `resource` | `None` | `ModelResource` class or instance shared with the REST adapter. |
| `queryset` | `None` | SQLAlchemy `Select` used as the starting query. |
| `list_display` | `None` | Model attribute names or callables displayed as columns. With a resource, defaults to its readable fields. |
| `list_display_links` | `()` | Columns linked to the detail route. An empty sequence links the first displayed column; `None` disables those links. |
| `list_filter` | `()` | Fields exposed to the list filter controls. |
| `search_fields` | `()` | Text fields searched by the `q` query parameter. |
| `ordering` | `None` | Default ordering. A leading `-` on a field name requests descending order. |
| `paginate_by` | `None` | Page size. Must be a positive integer when set. |
| `page_kwarg` | `"page"` | Query or path parameter that selects the page. |
| `page_start` | `1` | First page number accepted by the view. |
| `allow_empty` | `True` | Whether an empty result is rendered or returns `404`. |
| `context_object_name` | `None` | Name for the `object_list` context value. |
| `actions` | `()` | Bulk action methods or callables; enables `POST` for the list view. |
| `add_url` | `None` | Optional URL shown for the add action. |
| `template_name_suffix` | `"_list"` | Suffix used by the default template lookup. |

`get_queryset()` returns a SQLAlchemy `Select`. It applies the configured search,
filter, and ordering parameters to the query. `get_search_results(queryset,
term)`, `get_list_display()`, `get_list_filter()`, and `get_actions(model)` are
available to customize those parts. Await database work in asynchronous
overrides.

The list context includes `object_list`, `page_obj`, `is_paginated`, `rows`,
`list_display`, `list_display_links`, `actions`, `search_term`, `filters`, and
previous/next page URLs. With a resource, rows and filter choices are limited to
its readable fields.

When `template_name` is unset, templates are tried in this order:
`<app>/<model>_list.html`, then `obox/generic/list.html`.

### Bulk actions

Mark a method with `@action` and include its name in `actions`. The default
permission is `change`; pass `permissions=("delete",)` or another set of model
operations when needed. `description` supplies the label displayed to users.
Actions receive the view, request, and selected SQLAlchemy query. The built-in
`delete_selected` action requires a `ModelResource`.

```python
from obox.views.generic import ListView, action


class ProductList(ListView):
    actions = ("archive_selected",)

    @action(permissions=("change",), description="Archive selected products")
    async def archive_selected(self, request, queryset):
        ...
```

## `SingleObjectMixin`

`SingleObjectMixin` provides record lookup and context behavior for detail and
editing views.

| Attribute or method | Default | Description |
| --- | --- | --- |
| `model`, `resource`, `queryset` | `None` | Record source. Define one or override `get_queryset()`. |
| `pk_url_kwarg` | `"pk"` | URL parameter used for primary-key lookup. |
| `slug_url_kwarg` | `"slug"` | URL parameter used for slug lookup when no primary key is present. |
| `slug_field` | `"slug"` | Model attribute compared to the slug. |
| `get_queryset()` | — | Return a SQLAlchemy `Select`. |
| `get_object(queryset=None)` | — | Load the record identified by the configured URL parameter. |
| `context_object_name` | `None` | Override the default object context name. |

When `resource` is supplied, lookups use its scoped query and write permission
checks where applicable. An unavailable record returns `404`.

## `DetailView`

`DetailView` loads one record on `GET`. Its context contains both `object` and a
lower-case model-name key by default, along with `title`, serialized readable
`values` for a registered resource, and `change_url` when the user may edit the
record.

Set `template_name` to choose a template. Otherwise Obox tries
`<app>/<model>_detail.html`, then `obox/generic/detail.html`.
