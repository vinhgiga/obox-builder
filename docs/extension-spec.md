# Contrib integration contract — v0.1

Contrib apps retain product-specific capabilities alongside the shared core
settings, forms, generic views and `ModelApi` REST interface.

Enable optional applications in INSTALLED_APPS. AppConfig.ready prepares their
service configuration, and Obox attaches those services to the FastAPI host.
Application configs can declare resources, components, pages and product routers
in ready after model import. Product service lifetimes follow the host lifespan.

Analytics components use the shared component service and model-backed data
sources. Files, Activity, jobs, webhooks and idempotency keep their own feature
services and persistence models. These capabilities are optional and enabled
through `INSTALLED_APPS`.

Charts, pivots, custom components and pages remain in contrib. Their public
configuration is separate from core Form/ModelForm, generic views and REST.
There is no separate public plugin installation interface or
`obox.contrib.resources` namespace.

See the [REST contract](api-spec.md), [component query filters](component-query.md)
and [record activity contract](record-activity-spec.md) for the current public
HTTP behavior.
