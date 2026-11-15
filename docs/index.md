<div class="hero" markdown>

# Obox Builder

Build permission-aware CRUD applications on your FastAPI and SQLAlchemy models.

[Get started](examples/quickstart.md){ .md-button .md-button--primary }
[Explore features](features/index.md){ .md-button }

</div>

Obox Builder adds generated forms, record views, and JSON endpoints to a native
FastAPI application. You keep control of your models, migrations, application
lifecycle, and ordinary FastAPI routes.

## A resource in a few lines

```python
from obox_builder import ModelBuilder, builder
from models import Question


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "pub_date")
    search_fields = ("question_text",)
    filter_fields = ("pub_date",)

    class View(builder.View):
        list_display = ("question_text", "pub_date")
```

Add `QuestionBuilder` to an [`AppConfig`](reference/python-api.md#application-composition)
and list that app in `INSTALLED_APPS`. Obox compiles its form, list and detail
views, and API from the same field and permission rules.

## Documentation

<div class="grid cards" markdown>

-   :material-rocket-launch-outline: **Start building**

    ---

    Create a project scaffold, run it, and add your first SQLAlchemy resource.

    [Follow the quickstart](examples/quickstart.md)

-   :material-shield-check-outline: **Understand access control**

    ---

    Learn how authentication, RBAC, row scopes, and field allowlists work together.

    [Read the access guide](features/access-control.md)

-   :material-api: **Use the JSON API**

    ---

    Find generated endpoints, query controls, naming rules, and response shapes.

    [Browse API features](features/api.md)

-   :material-book-open-page-variant-outline: **Look up a contract**

    ---

    Go from Python symbols and settings to the detailed HTTP specifications.

    [Open the reference](reference/index.md)

</div>

## Built for existing FastAPI projects

- Register mapped SQLAlchemy models or customize them with `ModelBuilder[T]`.
- Keep FastAPI as the application and use its routers, dependencies, and OpenAPI.
- Extend the runtime with opt-in plugins for auth, activity, files, analytics,
  background jobs, idempotency, and webhooks.
- Apply operation, record-scope, and field-level authorization on the server.

See [Features](features/index.md) for the implemented capabilities and their
plugin requirements.
