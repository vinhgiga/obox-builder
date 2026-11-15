# Protect a resource

Install Auth before app resources are loaded. It provides browser sessions,
user and role management, an identity dependency, and a default RBAC policy.

```python
from obox_builder import Obox
from obox_builder.contrib.auth import Auth

obox = Obox(app, settings="setting", plugins=(Auth(),))
```

To restrict a resource to records owned by the current user, specialize RBAC
and add a SQLAlchemy row predicate:

```python
from obox_builder import Context, ModelBuilder
from obox_builder.auth import RBAC


class QuestionPolicy(RBAC[Question]):
    def scope(self, action: str, ctx: Context):
        return Question.owner_id == ctx.principal.id


class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "owner_id")
    policy = QuestionPolicy()
```

Keep the field allowlist and the policy aligned with the data you intend users
to see and change. RBAC grants operations; the scope narrows rows for each
operation. Neither grants tenant isolation automatically.

See [Access control](../../features/access-control.md) for host authentication,
field permissions, and API token behavior.
