# Changelog

## 0.1.0 — unreleased

- Registered resources default to `GridView`. Use `ListView` for a flat row
  layout and `ModelViewSet.collection_views` for additional views. Replace
  `list_display` with `fields`, `paginate_by` with `page_size`, and `p=0` with
  `page=1`. The default Grid template is `obox/generic/grid.html`.
- HTML collections resolve filter, sort, group, and search together. A JSON
  filter overrides flat filters. Invalid queries and invalid forms return `422`.
- HTML detail and edit lookups use `{pk}` and `ModelResource.lookup_field`.
  Detail pages show edit and delete links according to write permissions.
- Use `ModelForm[Schema, Model]` for a typed instance and save result. Unsupported
  `Meta` options now raise a configuration error.
- Model REST lists now support bounded filters and sorting on readable fields,
  plus a `:groups` route with the same record scope and permissions as list.
- REST search now splits on whitespace and requires every word to match a
  searchable field. A `None` field declaration infers eligible readable
  fields. An empty tuple disables the corresponding control.
- Oversized `limit` and `page_size` values are capped. Responses report the
  effective size.
- Replace `Page` with `AppConfig.routes` and template-owned component placement via
  `component("package.module.Class")`.
- Template component data routes now live under the owning app's UI prefix and
  require `<app>.components.access`, the component permission, and source policy.
- Route names use the `obox_route_<app>.<name>` namespace. Update reverse calls
  that referenced `obox_page_*` names.
- The login form renders on the first request and issues its CSRF cookie with
  the page. Login redirects preserve an explicit language prefix.
- Wheels store shared framework assets once and include compiled translations.

### Redesign

Obox restarts its public contract as a small subset of Django 5.2 LTS and
DRF 3.16.1. Install `obox-builder` and import `obox`. The earlier
`obox_builder` development changelog is available in the Git history.

- Settings use lazy uppercase configuration, OBOX_SETTINGS_MODULE, DATABASES,
  TEMPLATES and REST_FRAMEWORK. AppConfig owns discovery and startup order.
- Form and ModelForm are independent classes with awaited validation and save,
  Django-shaped cleaning/errors, Meta configuration and commit=False.
- Generic HTML views use per-request instances, native requests, unevaluated
  Select querysets and Django-shaped context. Invalid forms return 422 and
  successful writes redirect with 303.
- REST uses independent serializers, viewsets and SimpleRouter; PUT/PATCH are
  distinct and DELETE returns 204. Model REST routes use `/v0/<table_name>`
  by default and keep a class-derived alias when the suffix differs.
- ModelBuilder is an explicit-model ModelAdmin subset and builds admin HTML only.
- Product resources, batch/upsert, metadata and custom concurrency remain in
  contrib. Auth/files/activity/analytics/jobs/webhooks use the AppConfig integration.
- Scaffold, demo, documentation, tests and package assets use the new contract.

This is a breaking redesign. Previous namespace, Settings dataclass, combined
ModelView/ModelApi/ModelSurface APIs, generic model inference, surface aliases and
public plugin lifecycle are removed.
See [migration guidance](docs/release-notes.md).
