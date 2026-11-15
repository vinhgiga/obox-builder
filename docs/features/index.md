# Features

Obox Builder compiles registered SQLAlchemy resources into an admin UI and a
permission-aware JSON API. Optional capabilities are installed only when an
application needs them.

| Capability | What it provides | Setup |
| --- | --- | --- |
| Resource builders | Forms, lists, detail pages, shared field metadata, and CRUD APIs | Register `ModelBuilder` classes in `AppConfig.builders` |
| Access control | Browser sessions, users and roles, RBAC, row scopes, and field permissions | Install `Auth` or provide host identity and policy settings |
| JSON API | OpenAPI schemas, filters, sorting, cursor pagination, relationships, and batch writes | Enabled for each registered resource by default |
| Activity | Record revisions and plain-text comments | Install the `Activity` plugin |
| Files | Resumable uploads and permission-checked attachments | Install `Files` and declare attachment fields |
| Analytics | Charts, pivots, and data components over authorized resources | Install `Analytics` |
| Background work | Database-backed jobs and durable signed webhooks | Configure workers and the required database tables |
| Extensions | Apps, pages, components, hooks, and host FastAPI routers | Add an `AppConfig` or ordinary FastAPI integration |

All optional features are included in the package. They do not require separate
packages, and a plugin does not add its routes until installed.

## Feature guides

- [Resource builders](resources.md)
- [Access control](access-control.md)
- [JSON API](api.md)
- [Activity and files](activity-files.md)
- [Analytics](analytics.md)
- [Background work and extensions](extensions.md)

## Shared design

A registered resource uses one model builder and one service for the HTML and
JSON surfaces. A policy determines allowed operations, record scope, readable
fields, and writable fields. Optional renderers and plugins extend this shared
contract while the host application remains a FastAPI app.
