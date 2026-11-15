# Add a custom action

Actions expose a named domain operation on selected resource records. Declare
the action and its permission, then decorate the builder method:

```python
from collections.abc import Sequence

from obox_builder import Context, ModelBuilder, builder


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "published")
    permissions = ("publish",)
    actions = ("publish",)

    @builder.action(description="Publish selected questions", permission="publish")
    async def publish(self, objects: Sequence[Question], ctx: Context) -> None:
        for question in objects:
            question.published = True
```

With app label `polls`, the route is `POST /api/polls/questions:publish`. Send
the selected IDs in the request body:

```json
{"recordIds": ["12", "14"]}
```

The service checks the declared `publish` permission and policy for each record
inside one transaction. The response reports how many records were processed.
See the [API contract](../../api-spec.md) for the full action request behavior.
