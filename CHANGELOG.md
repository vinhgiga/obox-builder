# Changelog

## Unreleased

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

## 0.1.0 — redesign

Obox restarts its public contract as a small subset of Django 5.2 LTS and
DRF 3.16.1. The installation package remains `obox-builder`; imports use `obox`.
Existing Git history and release tags are preserved.

- Settings use lazy uppercase configuration, OBOX_SETTINGS_MODULE, DATABASES,
  TEMPLATES and REST_FRAMEWORK. AppConfig owns discovery and startup order.
- Form and ModelForm are independent classes with awaited validation and save,
  Django-shaped cleaning/errors, Meta configuration and commit=False.
- Generic HTML views use per-request instances, native requests, unevaluated
  Select querysets, Django-shaped context, status200 errors and302 redirects.
- REST uses independent serializers, viewsets and SimpleRouter; PUT/PATCH are
  distinct, DELETE returns204, URLs have trailing slashes, pagination is opt-in.
- ModelBuilder is an explicit-model ModelAdmin subset and builds admin HTML only.
- Product resources, batch/upsert, metadata and custom concurrency remain in
  contrib. Auth/files/activity/analytics/jobs/webhooks use the AppConfig integration.
- Scaffold, demo, documentation, tests and package assets use the new contract.

This is a breaking redesign. Previous namespace, Settings dataclass, combined
ModelView/ModelApi/ModelSurface APIs, generic model inference, surface aliases and
public plugin lifecycle are removed. No compatibility layer is provided.
See [migration guidance](docs/release-notes.md).
