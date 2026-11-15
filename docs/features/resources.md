# Resource builders

Register a `ModelBuilder[Model]` through an [`AppConfig`](../reference/python-api.md#application-composition).
The builder describes the resource once; the runtime creates its admin and API
routes from the same metadata.

## Declare the public fields

`fields` is an explicit allowlist. A resource without a field list is rejected
during configuration. Use `read_only_fields` to keep selected fields visible
while blocking writes, and configure `API.read_fields` and `API.write_fields`
when the JSON surface needs a narrower view.

```python
class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "pub_date")
    read_only_fields = ("pub_date",)
    search_fields = ("question_text",)
    filter_fields = ("pub_date",)
    ordering = ("-pub_date", "-id")
```

Field declarations are validated against the SQLAlchemy mapper at startup.
Unsupported or unknown columns fail configuration instead of appearing as
partially working controls.

## Customize each surface

`Form`, `View`, and `API` are nested configuration classes. The defaults provide
create/edit forms, list/detail views, and standard CRUD operations. Override
only the parts a resource needs:

```python
from obox_builder import ModelBuilder, builder


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "pub_date")

    class Form(builder.Form):
        labels = {"question_text": "Question"}

    class View(builder.View):
        list_display = ("question_text", "pub_date")
        operations = ("create", "update", "delete")

    class API(builder.API):
        operations = ("list", "retrieve", "create", "update")
```

The UI and API have separate read and write controls. Hiding a field in the UI
does not replace API or policy checks.

## Add the builder to an app

```python
from obox_builder import AppConfig
from app.polls.builders import QuestionBuilder


class PollsConfig(AppConfig):
    name = "app.polls"
    label = "polls"
    builders = (QuestionBuilder,)
```

List the config's import path in `INSTALLED_APPS`. App configs load before the
application starts, and their builders contribute routes under that app label.
See [Register a resource](../examples/features/register-resource.md) for a complete
example.

## Extend behavior

Use builder methods for domain validation and actions, a custom `Policy` for
authorization, and `ResourceService` for non-HTTP operations. These hooks share
the request context and service transaction behavior. The [core design
specification](../reference/index.md#design-specifications) describes the
intended contracts; check each section's status before depending on a proposed
extension.
