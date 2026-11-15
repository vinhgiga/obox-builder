# Register a resource

`ModelBuilder[T]` controls how one SQLAlchemy model appears in the generated
admin and JSON API. Put builders in an installed application and keep the
`fields` allowlist explicit.

```python
from obox_builder import ModelBuilder, builder
from models import Question


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "pub_date")
    read_only_fields = ("pub_date",)
    search_fields = ("question_text",)
    filter_fields = ("pub_date",)
    ordering = ("-pub_date", "-id")

    class Form(builder.Form):
        labels = {"question_text": "Question"}

    class View(builder.View):
        list_display = ("question_text", "pub_date")

    class API(builder.API):
        operations = ("list", "retrieve", "create")
```

Add the builder to an app configuration:

```python
from obox_builder import AppConfig
from app.polls.builders import QuestionBuilder


class PollsConfig(AppConfig):
    name = "app.polls"
    label = "polls"
    builders = (QuestionBuilder,)
```

Then list the config's import path in `INSTALLED_APPS`:

```python
INSTALLED_APPS = ("app.polls.apps.PollsConfig",)
```

The `fields` allowlist is shared by the form, view, and API; each surface can
narrow what it reads or writes. `read_only_fields` prevents writes to selected
fields. The API declaration above exposes list, retrieve, and create operations
for the resource.

See [Resource builders](../../features/resources.md) for configuration details.
