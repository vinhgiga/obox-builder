# Python API

The package root exports the types most applications need to compose an Obox
runtime. Optional plugins live in `obox_builder.contrib`.

## Application composition

| Symbol | Purpose |
| --- | --- |
| `Obox` | Connects the host FastAPI app, settings, apps, plugins, routes, and resource services |
| `AppConfig` | Groups resource builders, components, pages, and routers under an app label |
| `Plugin` | Structural contract for a plugin with a name, dependencies, and setup hook |
| `Settings` | Database, auth, routing, templates, API limits, and installed app configuration |
| `Page` | Declares an application page and its component layout |

`Obox` loads `AppConfig` classes listed in `Settings.installed_apps`. Put each
resource builder in the config's `builders` tuple. Routes and schemas are
compiled before startup; changing registrations after startup is unsupported.

```python
from obox_builder import AppConfig, ModelBuilder


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text",)


class PollsConfig(AppConfig):
    name = "app.polls"
    label = "polls"
    builders = (QuestionBuilder,)
```

## Resources and authorization

| Symbol | Purpose |
| --- | --- |
| `ModelBuilder[T]` | Shared model fields, search, filters, policy, and surface configuration |
| `Form`, `View`, `API` | Default presentation and API configuration base classes |
| `Policy[T]` | Operation, record scope, read field, write field, and aggregate authorization |
| `RBAC` | Permission-name policy used by the Auth integration |
| `Principal` | Simple built-in identity value with explicit permissions |
| `ResourceService[T]` | Authorized resource operations available outside HTTP |
| `Context` | Current principal, SQLAlchemy session, optional request, and source |

Import the base form/view/API classes from `obox_builder.builder` or use the
`builder` namespace exported from the package root.

## Components and events

`Component` and `Use` declare reusable components and page placements.
`ComponentQuery` and `ComponentRecords` describe component input and bounded
record drill. `DomainEvent` is the shared event passed through resource hooks.

## Optional plugins

| Import | Purpose |
| --- | --- |
| `obox_builder.contrib.auth.Auth` | Browser sessions, roles, permissions, and optional JSON sessions/tokens |
| `obox_builder.contrib.activity.Activity` | Resource revisions and comments |
| `obox_builder.contrib.files.Files` | Resumable uploads and model attachment fields |
| `obox_builder.contrib.analytics.Analytics` | Charts and pivots |
| `obox_builder.contrib.idempotency.Idempotency` | Idempotency receipts for supported writes |
| `obox_builder.contrib.webhooks.Webhooks` | Durable signed change notifications |

Jobs are registered through `obox_builder.contrib.jobs.task` and run with the
standalone CLI worker.

## Authorization

`Policy[T]` fails closed by default. Its synchronous `scope` predicate narrows
the rows available to an action; `read_fields` and `write_fields` narrow field
access. `RBAC` checks explicit `app.resource.action` grants. `StaffOnly` remains
in the package for compatibility but raises a configuration error; use RBAC or
a custom policy.
