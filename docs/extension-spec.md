# Contrib integration contract — v0.1

Contrib retains existing product behavior while core settings, forms, generic
views, REST and builder move to their Django/DRF subset. This document describes
the implemented boundary; it is not a proposed replacement product API.

Enable optional applications in INSTALLED_APPS. AppConfig.ready prepares their
service configuration, and Obox attaches those services to the FastAPI host.
Application configs can declare resources, components, pages and product routers
in ready after model import. Product service lifetimes follow the host lifespan.

Resource services own policy/context, filtering, metadata, batch/upsert,
concurrency, events and transactional behavior. Analytics sources point to
Resource declarations, never to the new ModelBuilder. File/activity/idempotency/
webhook integration uses the same product resource services.

Charts, pivots, custom components and pages remain in contrib. Their public
configuration is separate from core Form/ModelForm, generic views and REST.
There is no public plugin installation interface and no compatibility namespace.

The detailed [resource protocol](contrib-resource-api.md) and
[record/activity protocol](record-activity-spec.md) remain product contracts.
Regression tests for analytics, components, files, activity, webhooks and jobs
verify their behavior. A later product redesign can change declarations without
expanding the standardized core.
