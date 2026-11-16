# Access control

Core generic views leave identity and row visibility to the host application.
AdminSite requires active staff and checks Django model permission names.
REST uses DRF permission classes; configure IsAuthenticated explicitly when
anonymous access is not appropriate. The DRF default remains AllowAny.

Enable `obox.contrib.auth` in INSTALLED_APPS for accounts, sessions, roles,
password validation, admin account pages and optional API tokens. Configure
`AUTH = {'api_tokens': True}` to expose token routes. Auth tables must be migrated.
Create an administrator with `obox createsuperuser`.

Contrib resources use RBAC/Policy for action, row and field access. Source policy
also constrains analytics, relationships, files and activity. Caller identity
and trusted presets come from server context. Queryset restrictions and object
permissions remain separate checks; list endpoints must enforce row scope.

HTML forms and authenticated REST sessions require CSRF on unsafe requests.
See [REST](../api-spec.md) and [contrib protocol](../contrib-resource-api.md)
for their distinct transport/error contracts.
