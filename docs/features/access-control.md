# Access control

Obox Builder applies authorization in the service layer, so the admin UI, JSON
API, components, and direct service calls use the same policy rules. Identity
resolution and business-specific row scopes remain under host application
control.

## Install Auth

The Auth plugin supplies browser sessions, user and role management, an
identity dependency, and a default RBAC policy:

```python
from obox_builder import Obox
from obox_builder.contrib.auth import Auth

obox = Obox(app, settings="setting", plugins=(Auth(secure=not setting.DEBUG),))
```

The default policy requires explicit grants. A superuser can administer
resources, while other accounts need the relevant permissions. In production,
keep secure cookies enabled, set a private secret key, and use the generated
password validators or configure your own.

JSON session endpoints are enabled by default. Personal API tokens are opt-in
with `Auth(api_tokens=True)` and require the Auth token tables in the host's
migrations. Token scopes can narrow the owner's current permissions; they do
not create new grants.

## Limit fields and records

Declare only fields a resource should expose:

```python
class QuestionBuilder(ModelBuilder[Question]):
    fields = ("question_text", "owner_id")
```

Policies can further restrict operations, rows, and fields for the current
principal. A tenant policy must apply its tenant predicate to each relevant
operation; the default RBAC policy does not provide tenant isolation.

```python
from obox_builder.auth import RBAC


class QuestionPolicy(RBAC[Question]):
    def scope(self, action: str, ctx: Context):
        return Question.owner_id == ctx.principal.id
```

The policy API includes a typed SQLAlchemy predicate and field-level read/write
methods. See [Python API](../reference/python-api.md#authorization) for the
public classes and [the core specification](../reference/index.md#design-specifications)
for the broader contract.

## Host authentication

If the host already has authentication, supply `principal_dependency` and a
`default_policy` through `Settings`. Host values take precedence over plugin
defaults. Custom identities must expose the identity properties required by
the selected policy. See [Settings](../reference/settings.md) and the
[Auth section of the API contract](../api-spec.md#authentication-auth-plugin).
