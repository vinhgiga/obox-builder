# OBOX Builder — Core Specification v0.4

**Status:** Proposed public API and implementation contract; not a description of a released library.
**Target:** Python 3.11+, FastAPI, SQLAlchemy 2.x, Pydantic v2.
**Primary use case:** Model-driven internal CRUD applications with generated HTML Forms, Views, and REST APIs.
**Principles:** Less code is better · Convention over configuration · Native FastAPI · One resource, one service · Secure by default.

> **Scope decision:** OBOX does **not** implement rate limiting, request-size caps, result-count caps, or enforced maximum page sizes. List endpoints may return all matching records when pagination is not requested. Pagination can be enabled as a UI convenience or requested by API consumers, but is not a security or quota limit. Deployment infrastructure may impose its own constraints; these are outside the OBOX contract.

## 1. Purpose and concepts

OBOX generates a CRUD UI and REST API from a SQLAlchemy mapped class. Developers register the model once, then override only what they need. It extends FastAPI rather than replacing it and does not replace SQLAlchemy or Alembic.

| Concept | Responsibility |
| --- | --- |
| `Obox(FastAPI)` | FastAPI app, builder registration, route generation, application-level defaults |
| `ModelBuilder[T]` | Resource metadata, shared field/query configuration, business hooks |
| `Form` | Create/edit form presentation and form-only validation |
| `View` | List/detail UI, search/filter controls, visible operations |
| `API` | REST operations, static schemas, HTTP mapping |
| `Policy[T]` | Operation/row/field permissions, server-side presets, post-write checks |
| `Context` | Current principal, SQLAlchemy session, optional HTTP request, execution source |
| `ResourceService[T]` | Shared authorized CRUD operations and domain events |
| `Registration` | Immutable result of registering a builder |

**Design constraint:** `ModelBuilder`, `Form`, `View`, and `API` are extensible base classes with usable defaults. No method override is mandatory. Their signatures below define the intended extension API; implementation details marked **TBD** are not frozen contracts.

## 2. Quickstart and registration

### 2.1 Level 1 — Register a SQLAlchemy model

```python
from obox_builder import Obox
from obox_builder.auth import StaffOnly

app = Obox(
    session_factory=AsyncSessionLocal,
    principal_dependency=get_principal,
    default_policy=StaffOnly(),
)

app.register(Question)
app.register(Choice)


@app.get("/health")
async def health():
    return {"ok": True}
```

- `AsyncSessionLocal` is an `async_sessionmaker[AsyncSession]` configured by the host application.
- `get_principal` is a FastAPI dependency that resolves the caller (or an explicit anonymous principal).
- `StaffOnly()` is an example policy for authenticated staff users; it is not multi-tenant isolation.
- When authentication or the default policy is missing, registration/build validation fails instead of silently exposing CRUD operations.
- `app.register(Model)` generates Form, View and API with secure, documented defaults.

### 2.2 Level 2/3 — Customize one model

```python
from obox_builder import builder


class QuestionBuilder(builder.ModelBuilder[Question]):
    fields = ("question_text", "pub_date")
    search_fields = ("question_text",)
    filter_fields = ("pub_date",)
    ordering = ("-pub_date", "-id")

    class Form(builder.Form):
        labels = {"question_text": "Question"}

    class View(builder.View):
        list_display = ("question_text", "pub_date")

    class API(builder.API):
        operations = ("list", "retrieve", "create", "update")


app.register(Question, builder=QuestionBuilder)
```

**Registration contract:** v0.4 deliberately supports one explicit method, `app.register(model, *, builder=None)`. It is **not** also a decorator. Import builders at the application composition root to avoid circular imports. `app.include_router()` remains available for ordinary FastAPI routers.

### 2.3 `Obox` methods

The following is an **interface sketch** (not importable implementation code):

```python
class Obox(FastAPI):
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        principal_dependency: Callable[..., Any],
        default_policy: Policy[Any],
        ui_prefix: str = "/admin",
        api_prefix: str = "/api",
        **fastapi_kwargs: Any,
    ) -> None: ...

    def register(
        self,
        model: type[T],
        *,
        builder: type[ModelBuilder[T]] | None = None,
    ) -> Registration[T]: ...

    def get_builder(self, model: type[T]) -> ModelBuilder[T]: ...

    def service(self, model: type[T]) -> ResourceService[T]: ...

    def check(self, *, strict: bool = False) -> Sequence[Diagnostic]: ...

    def uow(
        self,
        *,
        principal: Principal,
        source: str = "job",
        # Additional arguments: TBD
    ) -> AsyncContextManager[Context]: ...
```

| Method | Behavior |
| --- | --- |
| `register(model, builder=None)` | Inspect metadata, validate builder, compile schemas and include generated routers; return stable `Registration` |
| `get_builder(model)` | Return the active registered builder |
| `service(model)` | Return the resource service for non-HTTP use |
| `check(strict=False)` | Validate registration/routes/security and report exposure diagnostics; strict mode raises on errors |
| `uow(principal, source="job")` | Open an independent async session and unit of work for a task, job or test |

**Lifecycle:** Register before startup; compile and include routes at registration time; verify conflicts and freeze the registry by startup. Runtime registration is not supported. Duplicate models, conflicting slugs and generated route conflicts fail explicitly. A registered builder must not silently override routes defined by the host FastAPI application.

## 3. `ModelBuilder[T]`

**Purpose:** The single place for shared resource configuration and domain hooks. All three surfaces—Form, View and API—share this metadata and call `ResourceService`.

### 3.1 Properties

| Property | Type | Default / meaning |
| --- | --- | --- |
| `model` | `type[T] \| None` | From `app.register(Model)`; may be declared for tooling |
| `slug` | `str \| None` | Stable pluralized model name, e.g. `questions` |
| `fields` | `FieldRefs \| None` | `None` = infer safe supported scalar fields for staff-only resources |
| `read_only_fields` | `FieldRefs` | Includes primary keys and known generated/computed fields |
| `search_fields` | `FieldRefs` | `()`; shared by View and API |
| `filter_fields` | `FieldRefs` | `()`; shared by View and API |
| `sort_fields` | `FieldRefs \| None` | Supported readable scalar fields; can be narrowed |
| `ordering` | `tuple[str, ...] \| None` | Primary-key order by default; stable tie-breaker |
| `policy` | `Policy[T] \| None` | `None` inherits `app.default_policy` |
| `Form` | `type[Form]` | Default Form configuration |
| `View` | `type[View]` | Default View configuration |
| `API` | `type[API]` | Default API configuration |

`FieldRefs` accepts mapped SQLAlchemy attributes and strings, e.g. `("pub_date",)` or `(Question.pub_date,)`. The compiler normalizes and checks them. No `exclude` property is defined in v0.4; use a single allowlist instead.

### 3.2 Methods and hooks

```python
class ModelBuilder(Generic[T]):
    def get_statement(
        self,
        ctx: Context,
    ) -> Select[tuple[T]]: ...

    async def validate_create(
        self,
        data: dict[str, Any],
        ctx: Context,
    ) -> Mapping[str, Any]: ...

    async def validate_update(
        self,
        instance: T,
        changes: dict[str, Any],
        ctx: Context,
    ) -> Mapping[str, Any]: ...

    async def before_save(
        self,
        instance: T,
        changes: Mapping[str, Any],
        ctx: Context,
        *,
        created: bool,
    ) -> None: ...
    async def before_delete(self, instance: T, ctx: Context) -> None: ...
    async def after_commit(self, event: DomainEvent, ctx: Context) -> None: ...
```

| Method | Parameters and intended behavior |
| --- | --- |
| `get_statement(ctx)` | Return an additional **base** SQLAlchemy `Select`, before mandatory policy scope is added. It may never bypass the policy. |
| `validate_create(data, ctx)` | Check/normalize create input; raise `BuilderValidationError` (exact error class name TBD). |
| `validate_update(instance, changes, ctx)` | Check/normalize proposed changes, without committing. |
| `before_save(instance, changes, ctx, *, created)` | Mutate an instance inside the current transaction, before flush. `created` distinguishes a new instance from an update; `changes` contains the validated input plus server presets. |
| `before_delete(instance, ctx)` | Validate/prepare deletion inside the current transaction. |
| `after_commit(event, ctx)` | Optional best-effort callback; the just-committed session must not be reused for new DB work. |

**Async-first:** All I/O hooks should be `async def`. The implementation may accept sync hooks for pure CPU-only logic; sync I/O inside an event loop is not supported. Hooks never call `commit()` or `rollback()` on the shared session. Hooks are not authorization hooks.

## 4. `Form`

**Purpose:** Render and validate HTML create/edit forms; do not duplicate persistence or domain-level validation.

### 4.1 Properties

| Property | Type | Default / meaning |
| --- | --- | --- |
| `enabled` | `bool` | `True` |
| `fields` | `FieldRefs \| None` | Inherit eligible builder fields |
| `fieldsets` | `Sequence[tuple[str \| None, Mapping[str, Any]]] \| None` | `None`; Django-style field groups |
| `labels` | `Mapping[str, str]` | `{}`; field labels |
| `help_texts` | `Mapping[str, str]` | `{}` |
| `widgets` | `Mapping[str, WidgetConfig]` | `{}`; per-field input config |

`fields` order defines the rendered form order; overriding `get_fields()` can reorder fields per action or request. `fieldsets` uses Django's `(title, options)` shape; the current subset supports a flat `fields` list and `description`, and cannot be combined with `fields`. Policy-hidden fields are removed from each group and empty groups are not rendered. Nested field rows and `classes` are not supported yet.

### 4.2 Methods

```python
class Form:
    def get_fields(
        self,
        ctx: Context,
        *,
        action: Literal["create", "update"],
        instance: Any | None = None,
    ) -> Sequence[str]: ...

    def get_initial(self, ctx: Context) -> Mapping[str, Any]: ...

    async def validate(
        self,
        data: Mapping[str, Any],
        ctx: Context,
        *,
        instance: Any | None = None,
    ) -> Mapping[str, Any]: ...

    def get_context(
        self,
        ctx: Context,
        *,
        instance: Any | None = None,
        action: Literal["create", "update"],
    ) -> Mapping[str, Any]: ...
```

**Defaults:** Infer widgets, labels, required fields and `String(200)` length validation from SQLAlchemy metadata; render per-field errors; escape HTML; use CSRF protection for cookie-authenticated write forms. `get_fields()` returns fields **after** policy narrowing, never a way to reintroduce forbidden fields. UI-only checks do not replace `ResourceService` validation.

**Not required in v0.4:** Conditional forms, multi-step forms, file uploads, CAPTCHA, visual form designer.

## 5. `View`

**Purpose:** Render list/detail pages and UI operations while reusing policy-scoped resource queries.

### 5.1 Properties

| Property | Type | Default / meaning |
| --- | --- | --- |
| `enabled` | `bool` | `True` |
| `list_display` | `FieldRefs \| None` | Inherit readable builder fields |
| `detail_fields` | `FieldRefs \| None` | Inherit readable builder fields |
| `operations` | `tuple[str, ...]` | `("create", "update", "delete")`; UI visibility only |
| `page_size` | `int \| None` | `None`; no forced pagination or maximum |

Searchable, filterable and sortable fields come from `ModelBuilder`, not `View`, so the same rules apply to the REST API.

### 5.2 Methods

```python
class View:
    def get_list_display(self, ctx: Context) -> Sequence[str]: ...

    def get_list_filter(self, ctx: Context) -> Sequence[FilterDefinition]: ...

    def get_operations(
        self,
        ctx: Context,
        *,
        instance: Any | None = None,
    ) -> Sequence[OperationDefinition]: ...

    def get_context(
        self,
        ctx: Context,
        *,
        instance: Any | None = None,
    ) -> Mapping[str, Any]: ...
```

**Defaults:** Table list, detail page, create/edit/delete controls as permitted, optional search/filter/sort and optional pagination. A hidden button never substitutes for a server-side permission check. The UI is mounted under `/admin`, not `/`.

## 6. `API`

**Purpose:** Generate REST CRUD routes and OpenAPI input/output schemas from the same resource configuration.

### 6.1 Properties

| Property | Type | Default / meaning |
| --- | --- | --- |
| `enabled` | `bool` | `True` |
| `operations` | `tuple[str, ...]` | `("list", "retrieve", "create", "update", "delete")` |
| `read_fields` | `FieldRefs \| None` | Inherit builder read projection |
| `write_fields` | `FieldRefs \| None` | Inherit builder write projection |
| `prefix` | `str \| None` | Inherit resource `slug` |
| `tags` | `tuple[str, ...] \| None` | Generated from model name |
| `pagination` | `bool` | `False` by default; consumers may request `offset`/`limit` if enabled |

`operations` describes **semantic operations**, not HTTP methods. `GET` applies to both list and retrieve, so `methods=("GET", ...)` is not a suitable contract.

### 6.2 Methods

```python
class API:
    def get_read_schema(
        self,
        *,
        builder: ModelBuilder[Any],
    ) -> type[BaseModel]: ...

    def get_create_schema(
        self,
        *,
        builder: ModelBuilder[Any],
    ) -> type[BaseModel]: ...

    def get_update_schema(
        self,
        *,
        builder: ModelBuilder[Any],
    ) -> type[BaseModel]: ...
```

These schema methods run during registration, not per request. Schemas expose the **static allowed superset**; runtime policy can narrow a response further. Conditional fields must not be unconditionally required by the output schema.

### 6.3 Generated routes

| Method | Route | Operation |
| --- | --- | --- |
| `GET` | `/api/questions` | `list` |
| `POST` | `/api/questions` | `create` |
| `GET` | `/api/questions/{id}` | `retrieve` |
| `PATCH` | `/api/questions/{id}` | `update` |
| `DELETE` | `/api/questions/{id}` | `delete` |

Canonical routes have no trailing slash. Resource slugs are configurable. OBOX does not impose a request rate, request-size quota or result maximum.

**List behavior:** Search, filters and sorting use explicit allowed fields; unrequested pagination returns all matching records. Optional `offset`/`limit` enables pagination without a framework-enforced maximum. No cursor or bulk API is required for v0.4.

**HTTP behavior:** `200/201/204` for success; `401/403/404` for access and lookup failures; `409` for conflicts; `422` for invalid input. Optional optimistic concurrency may use `412/428`. Error body shape: `application/problem+json` preferred; exact error code registry TBD.

## 7. `Policy[T]` — Authorization

**Purpose:** Enforce consistent access across UI, REST API, relationship lookups and direct service calls.

### 7.1 Methods

```python
class Policy(Generic[T]):
    async def allow(
        self,
        action: str,
        ctx: Context,
        *,
        obj: T | None = None,
    ) -> bool: ...

    def scope(
        self,
        action: str,
        ctx: Context,
    ) -> ColumnElement[bool]: ...

    def read_fields(
        self,
        ctx: Context,
        *,
        obj: T | None = None,
    ) -> frozenset[str] | None: ...

    def write_fields(
        self,
        action: str,
        ctx: Context,
        *,
        obj: T | None = None,
    ) -> frozenset[str] | None: ...

    def presets(
        self,
        action: str,
        ctx: Context,
    ) -> Mapping[str, Any]: ...

    async def check(
        self,
        action: str,
        instance: T,
        ctx: Context,
    ) -> bool: ...
```

| Method | Contract |
| --- | --- |
| `allow(action, ctx, obj=None)` | Operation gate, optionally checking an individual record |
| `scope(action, ctx)` | SQL predicate added to list and object lookup queries; never filter protected rows in Python after fetching |
| `read_fields(ctx, obj=None)` | Dynamically narrow readable fields; `None` = no additional narrowing |
| `write_fields(action, ctx, obj=None)` | Dynamically narrow writable fields; `None` = no additional narrowing |
| `presets(action, ctx)` | Trusted server values, e.g. `owner_id`/`tenant_id`; client cannot override |
| `check(action, instance, ctx)` | Validate resulting object after create/update and before commit |

**Default behavior:** A base policy fails closed. `default_policy=StaffOnly()` lets staff use generated resources without writing a policy class for each model. Public resources require explicit opt-in and explicit field projections. A resource policy replaces the app fallback but cannot weaken mandatory global guards, if configured.

**Tenant isolation:** A staff-only policy grants staff rights; it does **not** imply tenant isolation. Tenant-aware apps must implement scoped SQL predicates, server-controlled tenant presets and post-write checks. An object-level gate alone is insufficient for list queries.

## 8. Fields and projections

Field exposure is an **intersection**, never last-write-wins replacement:

```text
resource_fields = builder.fields or supported AUTO columns (staff-only)

API_READ     = resource_fields ∩ API.read_fields  ∩ policy.read_fields(ctx, obj)
API_WRITE    = resource_fields ∩ API.write_fields ∩ policy.write_fields(action, ctx, obj)
               − read_only_fields − server_preset_fields
FORM_CREATE  = resource_fields ∩ Form.fields ∩ policy.write_fields("create", ctx)
               − read_only_fields − server_preset_fields
FORM_UPDATE  = resource_fields ∩ Form.fields ∩ policy.read_fields(ctx, obj)
               ∩ policy.write_fields("update", ctx, obj)
               − read_only_fields − server_preset_fields
VIEW_READ    = resource_fields ∩ View.list_display/detail_fields ∩ policy.read_fields(ctx, obj)
```

Rules:

1. `None` for a surface projection means **inherit**; an explicit empty tuple means expose nothing for that surface.
2. `read_only_fields` applies in Forms, API and direct service writes. Primary keys are never client-writable.
3. Known forbidden writes are rejected; unknown fields produce validation errors; nothing silently assigns arbitrary ORM attributes.
4. `Column.info["obox"] = {"expose": False}` is supported as an optional way to exclude known sensitive fields. Auto-discovery cannot recognize every business secret; explicit fields are strongly recommended for externally accessible resources.
5. Search/filter/sort fields must be permitted for the list/query context. Per-record masking still happens before serialization.
6. `app.check(strict=True)` reports a static exposure manifest. Dynamic permissions are marked conditional rather than treated as a fixed schema.

## 9. Relationships

The minimum supported relationship is a SQLAlchemy `ForeignKey` such as `Choice.question_id` targeting `Question.id`.

- Derive FK widgets from real SQLAlchemy metadata, not column naming heuristics.
- For a registered target resource, a Form can render a relationship selector using the target's scoped query. Relationship selection is subject to authorization.
- Read-only links to related records are available where permitted.
- Collections such as `Question.choices` are **not** automatically included in every payload; enable them explicitly in View configuration.
- No automatic nested create/update/delete in v0.4.
- No unrestricted recursive object serialization; expanded data requires an explicitly declared relationship projection. This is a schema/authorization rule, **not** a numeric result limit.
- Database and ORM cascade rules govern deletion. OBOX does not silently invent cascade behavior.
- Eager loading avoids accidental async lazy-I/O and N+1 issues, where needed.
- When a related model is unregistered and no reference policy/resolver exists, block writable relationship selection rather than assuming access.

## 10. `Context`, `ResourceService[T]`, and transactions

### 10.1 `Context`

| Property | Type | Meaning |
| --- | --- | --- |
| `principal` | `Principal` | Resolved actor, not a client-provided user ID |
| `session` | `AsyncSession` | Session owned by the current unit of work |
| `request` | `Request \| None` | Optional for HTTP; `None` in background jobs/tests |
| `source` | `str` | `api`, `ui`, `job`, `cli`, etc. |
| `tenant` | `Any \| None` | Optional verified tenant context |
| `locale` | `str \| None` | Optional localization |
| `trace_id` | `str \| None` | Optional diagnostic correlation identifier |

### 10.2 Service interface

```python
class ResourceService(Generic[T]):
    async def list(
        self,
        *,
        ctx: Context,
        filters: Mapping[str, Any] | None = None,
        search: str | None = None,
        ordering: Sequence[str] | None = None,
        offset: int = 0,
        limit: int | None = None,
    ) -> Sequence[T]: ...

    async def get(self, id: Any, *, ctx: Context) -> T: ...

    async def create(
        self,
        data: Mapping[str, Any],
        *,
        ctx: Context,
    ) -> T: ...

    async def update(
        self,
        id: Any,
        changes: Mapping[str, Any],
        *,
        ctx: Context,
        version: Any | None = None,
    ) -> T: ...

    async def delete(
        self,
        id: Any,
        *,
        ctx: Context,
        version: Any | None = None,
    ) -> None: ...
```

- `list(..., limit=None)` returns all matching records, with no OBOX-imposed hard cap. Filters/sort are restricted by configured allowlists for security and correctness, not quotas.
- `get()` applies both SQL row scope and object-level access checks.
- `create()` and `update()` validate writable fields, apply server presets, run hooks, flush and perform post-write checks.
- `delete()` checks row scope and object permission before deletion.
- Service methods do **not** commit independently; the unit of work owns commit/rollback.
- All HTTP/UI/job/CLI writes using OBOX must go through this service to share rules and events.

### 10.3 Direct use outside HTTP

```python
async with app.uow(principal=actor, source="job") as ctx:
    question = await app.service(Question).create(
        {"question_text": "Example", "pub_date": published_at},
        ctx=ctx,
    )
```

Each concurrent task owns a separate `AsyncSession`. A privileged system actor must be explicitly constructed and audited; CLI/job source must not automatically bypass policy.

## 11. Request-to-commit lifecycle

```text
Request / UI / direct service call
  → resolve Context and principal
  → check operation permission
  → apply SQL row scope to lookups
  → enforce static + runtime field permissions
  → validate input schema and form/domain rules
  → apply trusted server presets
  → run before_save or before_delete hooks
  → flush within the current transaction
  → run post-write policy check
  → commit through unit of work
  → serialize permitted response fields
  → optional after_commit callback
```

- `after_commit` is best-effort; it is not a durable webhook system.
- Auditing can be attached to the service/UoW layer. The framework cannot automatically capture arbitrary raw SQL executed outside OBOX.
- Optimistic concurrency via SQLAlchemy `version_id_col`, ETag and `If-Match` is an **optional** capability; if enabled, stale updates must fail explicitly.
- Database migrations remain the host application's responsibility (typically Alembic).

## 12. Defaults and generated UI paths

| UI method | Canonical path | Behavior |
| --- | --- | --- |
| `GET` | `/admin/questions` | List |
| `GET` | `/admin/questions/new` | Create form |
| `POST` | `/admin/questions/new` | Submit create |
| `GET` | `/admin/questions/{id}` | Detail |
| `GET` | `/admin/questions/{id}/edit` | Edit form |
| `POST` | `/admin/questions/{id}/edit` | Submit update |
| `POST` | `/admin/questions/{id}/delete` | Confirmed delete |

Form submissions must use CSRF checks when authenticated with cookies. The API follows the canonical routes in §6.3. All routes are optional per registered capability and remain subject to policy enforcement.

## 13. Diagnostics and acceptance tests

**Recommended tooling** (names are proposed, not guaranteed shipped CLI commands):

```shell
obox check --strict
obox routes
obox schema --json
```

**Production-core acceptance criteria:**

1. `app.register(Question)` creates the expected Form, View and API without colliding with native FastAPI endpoints.
2. Unauthenticated users cannot access staff-only endpoints; public access is never implicit.
3. Row-scoped list, detail, update, delete and relationship lookups prevent broken object-level authorization.
4. API/Form/Service reject unauthorized field writes, including primary keys, read-only fields and server-controlled presets.
5. UI/API/Service follow the same validation, hooks, permissions and transaction behavior.
6. Foreign-key selection respects the target model's policy; async ORM serialization does not rely on unexpected lazy loads.
7. HTML routes prevent CSRF on cookie-authenticated writes and escape untrusted output; SQL is parameterized.
8. Startup catches invalid fields, unsupported relationship configurations and route/name collisions.
9. OpenAPI correctly represents the statically available schema and acknowledges conditional runtime field exposure.
10. Integration tests cover PostgreSQL, rollback, concurrent writes and application startup.

**Explicit non-requirements:** No OBOX rate limiter; no request-size cap; no maximum number of returned records; no mandatory `max_page_size`; no result-count cap on relationship queries. These are intentionally excluded, not silently deferred as required production gates.

## 14. Out of scope for the core release

Visual no-code editor, multi-step conditional forms, bulk CRUD operations, cursor pagination, automatic nested writes, offline mobile UI, collaboration, external webhook delivery, rich plugin marketplace, automatic schema/migration generation, and code/stub export. These may be built later without changing the single-builder abstraction.

## 15. Reference documentation

- FastAPI routers: https://fastapi.tiangolo.com/tutorial/bigger-applications/
- FastAPI behind a proxy: https://fastapi.tiangolo.com/advanced/behind-a-proxy/
- SQLAlchemy ORM session lifecycle: https://docs.sqlalchemy.org/en/20/orm/session_basics.html
- SQLAlchemy version counters: https://docs.sqlalchemy.org/en/20/orm/versioning.html
- SQLAlchemy query criteria: https://docs.sqlalchemy.org/en/20/orm/queryguide/api.html
- SQLAlchemy relationships/cascade: https://docs.sqlalchemy.org/en/20/orm/cascades.html
- Hasura authorization: https://hasura.io/docs/2.0/auth/authorization/permissions/
- Payload access control: https://payloadcms.com/docs/access-control/collections
- OWASP API Security Top 10: https://owasp.org/API-Security/
- RFC 9457 Problem Details: https://www.rfc-editor.org/rfc/rfc9457.html

---

**Implementation note:** These classes and signatures are **proposed contracts**. Types such as `Principal`, `DomainEvent`, `Diagnostic`, `WidgetConfig`, `FilterDefinition`, `OperationDefinition` and the exact validation exception are deliberately left as **TBD** until a working vertical slice fixes their shape. Avoid inventing a second abstraction layer merely to fill in these placeholders.
