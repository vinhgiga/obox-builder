# Changelog

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
