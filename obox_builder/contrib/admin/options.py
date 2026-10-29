"""ModelAdmin: declarative CRUD configuration for one model."""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar, Protocol, cast

import sqlalchemy as sa
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel

from obox_builder.contrib.admin.actions import delete_selected
from obox_builder.core.checks.messages import CheckMessage
from obox_builder.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.fields import Cell, FieldSpec, introspect_model
from obox_builder.fields.introspect import FieldConfig
from obox_builder.fields.types.text import Text
from obox_builder.forms import BoundForm, build_schema
from obox_builder.utils.strings import camel_to_snake, humanize

if TYPE_CHECKING:
    from obox_builder.contrib.admin.context import AdminContext
    from obox_builder.contrib.admin.inlines import InlineModelAdmin
    from obox_builder.contrib.admin.site import AdminSite

logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 50
BASE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")


class _ListDisplayLinksDefault(Enum):
    AUTO = "auto"


_AUTO_LIST_DISPLAY_LINKS = _ListDisplayLinksDefault.AUTO

#: Field types an inline cell can render as a simple input/select.
INLINE_TYPES = frozenset(
    {"text", "email", "url", "phone", "integer", "decimal", "checkbox", "single_select"}
)


@dataclass(frozen=True)
class ActionSpec:
    """A bulk action offered on the list page."""

    name: str
    description: str
    permissions: tuple[str, ...]
    func: Callable[[AdminContext, sa.Select[Any]], Awaitable[Any]]
    api: bool = False


@dataclass(frozen=True)
class AdminRoute:
    """A protected custom route below one model's admin URL."""

    path: str
    endpoint: Callable[..., Any]
    name: str
    methods: tuple[str, ...] = ("GET",)
    permission: str | None = "view"


class _DisplayMethod(Protocol):
    __name__: str
    admin_description: str
    admin_ordering: str | None
    admin_boolean: bool

    def __call__(self, *args: Any, **kwargs: Any) -> Any: ...


class SimpleListFilter:
    """A request-aware filter that can narrow the admin's already-scoped query."""

    title: ClassVar[str] = ""
    parameter_name: ClassVar[str] = ""

    def __init__(self, ctx: AdminContext) -> None:
        self.ctx = ctx

    def lookups(self) -> Sequence[tuple[str, str]]:
        return ()

    def queryset(self, queryset: sa.Select[Any], value: str) -> sa.Select[Any]:
        return queryset


def display(
    function: Callable[..., Any] | None = None,
    *,
    description: str | None = None,
    ordering: str | None = None,
    boolean: bool = False,
) -> Any:
    """Mark a model or admin method for use as a read-only display column."""

    def decorate(method: Callable[..., Any]) -> Callable[..., Any]:
        display_method = cast(_DisplayMethod, method)
        display_method.admin_description = description or humanize(method.__name__)
        display_method.admin_ordering = ordering
        display_method.admin_boolean = boolean
        return method

    return decorate(function) if function is not None else decorate


class ModelAdmin:
    """Configuration object for one model's CRUD screens.

    Subclass and register with ``@site.register(Model)``. The declared
    attributes keep Django's names (``list_display``, ``search_fields``, ...)
    so the mental model carries over.
    """

    api_enabled: bool = False
    model: type[Any]

    list_display: ClassVar[Sequence[str | Callable[..., Any]]] = ()
    list_display_links: ClassVar[Sequence[str] | None | _ListDisplayLinksDefault] = (
        _AUTO_LIST_DISPLAY_LINKS
    )
    list_editable: ClassVar[Sequence[str]] = ()
    list_filter: ClassVar[Sequence[str | type[SimpleListFilter]]] = ()
    #: Bulk actions on the list page; strings name ModelAdmin methods,
    #: callables are used directly. ``None`` disables actions entirely.
    actions: ClassVar[Sequence[str | Callable[..., Any]] | None] = ()
    search_fields: ClassVar[Sequence[str]] = ()
    ordering: ClassVar[Sequence[str]] = ()
    fields: ClassVar[Sequence[str] | None] = None
    fieldsets: ClassVar[Sequence[tuple[str | None, Mapping[str, Any]]] | None] = None
    readonly_fields: ClassVar[Sequence[str | Callable[..., Any]]] = ()
    save_as: ClassVar[bool] = False
    save_as_continue: ClassVar[bool] = True
    exclude: ClassVar[Sequence[str]] = ()
    fields_config: ClassVar[Mapping[str, FieldConfig]] = {}
    verbose_name: ClassVar[str] = ""
    verbose_name_plural: ClassVar[str] = ""
    per_page: ClassVar[int] = DEFAULT_PER_PAGE
    show_full_result_count: ClassVar[bool] = True
    extra_form_fields: ClassVar[Sequence[FieldSpec]] = ()
    #: URL segment(s) under ``/admin``; defaults to ``<app_label>/<model_name>``.
    base_path: ClassVar[str | None] = None
    import_enabled: ClassVar[bool] = False
    import_keys: ClassVar[Sequence[str]] = ()
    autocomplete_fields: ClassVar[Sequence[str]] = ()
    inlines: ClassVar[Sequence[type[InlineModelAdmin]]] = ()
    prepopulated_fields: ClassVar[Mapping[str, Sequence[str]]] = {}
    extra_css: ClassVar[Sequence[str]] = ()
    extra_js: ClassVar[Sequence[str]] = ()

    def imports_available(self) -> bool:
        from obox_builder.apps import apps
        from obox_builder.conf import settings

        return (
            self.import_enabled
            and settings.JOBS_ENABLED
            and all(
                apps.is_installed(name)
                for name in ("obox_builder.contrib.imports", "obox_builder.contrib.jobs")
            )
        )

    def import_path(self) -> str:
        return f"{self.list_path()}/import"

    def __init__(self, model: type[Any], site: AdminSite | None = None) -> None:
        self.model = model
        self.site = site
        self._specs: list[FieldSpec] | None = None
        self._schema: type[BaseModel] | None = None
        self._schema_mode: str = ""
        self._inline_schema: type[BaseModel] | None = None
        self._inline_specs: list[FieldSpec] | None = None

    # -- identity -----------------------------------------------------------
    @property
    def app_label(self) -> str:
        return self.model._model_label  # type: ignore[attr-defined]

    @property
    def model_name(self) -> str:
        return camel_to_snake(self.model.__name__)

    @property
    def model_label(self) -> str:
        return f"{self.app_label}.{self.model_name}"

    @property
    def name(self) -> str:
        return self.verbose_name or humanize(self.model.__name__)

    @property
    def plural_name(self) -> str:
        if self.verbose_name_plural:
            return self.verbose_name_plural
        name = self.name
        if name.endswith(("s", "x", "z", "ch", "sh")):
            return f"{name}es"
        if name.endswith("y") and len(name) > 1 and name[-2] not in "aeiou":
            return f"{name[:-1]}ies"
        return f"{name}s"

    @property
    def route_prefix(self) -> str:
        return f"admin:{self.app_label}_{self.model_name}"

    @property
    def path_prefix(self) -> str:
        """Path segment(s) under ``/admin`` used by the CRUD routes."""
        if self.base_path:
            return self.base_path.strip("/")
        return f"{self.app_label}/{self.model_name}"

    def list_path(self) -> str:
        return f"/admin/{self.path_prefix}"

    def add_path(self) -> str:
        return f"{self.list_path()}/add"

    def change_path(self, pk: Any) -> str:
        return f"{self.list_path()}/{pk}"

    def delete_path(self, pk: Any) -> str:
        return f"{self.list_path()}/{pk}/delete"

    def history_path(self, pk: Any) -> str:
        return f"{self.list_path()}/{pk}/history"

    def comments_path(self, pk: Any) -> str:
        return f"{self.list_path()}/{pk}/comments"

    def comment_action_path(self, pk: Any, comment_id: int, action: str) -> str:
        return f"{self.comments_path(pk)}/{comment_id}/{action}"

    # -- permissions --------------------------------------------------------
    def permission_name(self, action: str) -> str:
        from obox_builder.contrib.auth.authz import model_permission

        return model_permission(self.model, action)

    def has_view_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return bool(
            ctx.user.has_perm(self.permission_name("view"))
            or ctx.user.has_perm(self.permission_name("change"))
        )

    def has_add_permission(self, ctx: AdminContext) -> bool:
        return self._has_write_permission(ctx, "add")

    def has_change_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return self._has_write_permission(ctx, "change")

    def has_delete_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return self._has_write_permission(ctx, "delete")

    def _has_write_permission(self, ctx: AdminContext, action: str) -> bool:
        # Account writes must use the dedicated administration policy and password helpers.
        return (
            not getattr(self.model, "__auth_model__", False)
            and self.model_label not in {"auth.role", "auth.permission"}
            and bool(ctx.user.has_perm(self.permission_name(action)))
        )

    def check_permission(self, ctx: AdminContext, action: str, obj: Any = None) -> None:
        checker = getattr(self, f"has_{action}_permission")
        allowed = checker(ctx) if action == "add" else checker(ctx, obj)
        if not allowed:
            raise PermissionDenied(
                f"You do not have permission to {action} this {self.name.lower()}."
            )

    def has_history_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        """Reading the audit trail requires the row-history permission *and* view."""
        from obox_builder.contrib.history import HISTORY_VIEW_PERMISSION

        return self.has_view_permission(ctx, obj) and bool(
            ctx.user.has_perm(HISTORY_VIEW_PERMISSION)
        )

    def comments_available(self) -> bool:
        """True when the comments app is installed and this model opts in."""
        from obox_builder.apps import apps
        from obox_builder.contrib.comments import APP_NAME
        from obox_builder.contrib.comments.registry import is_commentable

        return apps.is_installed(APP_NAME) and is_commentable(self.model)

    def has_comment_view_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        from obox_builder.contrib.comments import COMMENTS_VIEW_PERMISSION

        return self.has_view_permission(ctx, obj) and bool(
            ctx.user.has_perm(COMMENTS_VIEW_PERMISSION)
        )

    def has_comment_add_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        from obox_builder.contrib.comments import COMMENTS_ADD_PERMISSION

        return self.has_view_permission(ctx, obj) and bool(
            ctx.user.has_perm(COMMENTS_ADD_PERMISSION)
        )

    def has_comment_moderate_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        from obox_builder.contrib.comments import COMMENTS_MODERATE_PERMISSION

        return self.has_view_permission(ctx, obj) and bool(
            ctx.user.has_perm(COMMENTS_MODERATE_PERMISSION)
        )

    # -- queryset -----------------------------------------------------------
    def get_queryset(self, ctx: AdminContext) -> sa.Select[Any]:
        from sqlalchemy.orm import selectinload

        stmt: sa.Select[Any] = sa.select(self.model)
        for spec in self.get_field_specs():
            relationship = spec.options.get("relationship")
            if (
                spec.type.name in {"link", "multi_link"}
                and relationship
                and (spec.type.name == "multi_link" or not (ctx and ctx.extra.get("api")))
                and not (ctx and ctx.extra.get("api_labels"))
            ):
                stmt = stmt.options(selectinload(getattr(self.model, str(relationship))))
        return stmt

    def get_ordering(self, ctx: AdminContext) -> tuple[str, ...]:
        return tuple(self.ordering or ())

    def get_list_display(
        self, ctx: AdminContext | None = None
    ) -> Sequence[str | Callable[..., Any]]:
        return self.list_display

    def get_list_display_links(self, ctx: AdminContext | None = None) -> tuple[str, ...]:
        """Return linked columns; the first is linked when the option is omitted."""
        if self.list_display_links is _AUTO_LIST_DISPLAY_LINKS:
            specs = self.get_list_specs(ctx)
            return (specs[0].name,) if specs else ()
        if self.list_display_links is None:
            return ()
        if isinstance(self.list_display_links, str):
            return (self.list_display_links,)
        return tuple(self.list_display_links)

    def get_list_filter(
        self, ctx: AdminContext | None = None
    ) -> Sequence[str | type[SimpleListFilter]]:
        return self.list_filter

    def get_search_fields(self, ctx: AdminContext | None = None) -> Sequence[str]:
        return self.search_fields

    def get_fields(self, ctx: AdminContext | None = None) -> Sequence[str] | None:
        return self.fields

    def get_exclude(self, ctx: AdminContext | None = None) -> Sequence[str]:
        return self.exclude

    def get_fieldsets(
        self, ctx: AdminContext | None = None
    ) -> Sequence[tuple[str | None, Mapping[str, Any]]] | None:
        return self.fieldsets

    def get_readonly_fields(
        self, ctx: AdminContext | None = None
    ) -> Sequence[str | Callable[..., Any]]:
        return self.readonly_fields

    def get_list_editable(self, ctx: AdminContext | None = None) -> Sequence[str]:
        return self.list_editable

    def get_autocomplete_fields(self, ctx: AdminContext | None = None) -> Sequence[str]:
        return self.autocomplete_fields

    def get_prepopulated_fields(
        self, ctx: AdminContext | None = None
    ) -> Mapping[str, Sequence[str]]:
        return self.prepopulated_fields

    def get_list_per_page(self, ctx: AdminContext | None = None) -> int:
        return self.per_page

    def get_extra_form_fields(self, ctx: AdminContext) -> Sequence[FieldSpec]:
        """Return validated form-only fields; override for request-specific fields."""
        return self.extra_form_fields

    def get_api_urls(self) -> Sequence[AdminRoute]:
        """Additional JSON routes, relative to this model's API namespace."""
        return ()

    def get_urls(self) -> Sequence[AdminRoute]:
        """Return extra FastAPI routes under this model's admin URL.

        A route endpoint may accept ``ctx: AdminContext`` plus normal FastAPI
        parameters. AdminContext injection, authentication, permission checks,
        and CSRF validation are supplied by the admin site.
        """
        return ()

    def get_paginator(self, ctx: AdminContext, queryset: sa.Select[Any]) -> Any:
        """Return a paginator for this request; override for custom count/page behavior."""
        from obox_builder.contrib.admin.pagination import AdminPaginator

        return AdminPaginator(
            ctx,
            queryset,
            show_full_result_count=self.show_full_result_count,
        )

    async def get_object(self, ctx: AdminContext, pk: str, *, for_update: bool = False) -> Any:
        """Resolve a row within this request's queryset, including eager loads."""
        value = self.parse_pk(pk)
        stmt = self.get_queryset(ctx).where(self.pk_column() == value)
        if for_update:
            stmt = stmt.with_for_update(of=self.model).execution_options(populate_existing=True)
        result = await ctx.db.scalars(stmt)
        obj = result.unique().one_or_none()
        if obj is None:
            raise LookupError(f"{self.name} with id {pk!r} does not exist.")
        return obj

    # -- field specs --------------------------------------------------------
    def get_field_specs(self) -> list[FieldSpec]:
        if self._specs is None:
            specs = introspect_model(self.model, overrides=self.fields_config)
            excluded = set(self.exclude)
            readonly = set(self.readonly_fields)
            prepared: list[FieldSpec] = []
            for spec in specs:
                if spec.name in excluded:
                    continue
                if spec.name in readonly and not spec.readonly:
                    from dataclasses import replace

                    spec = replace(spec, readonly=True, editable=False)
                prepared.append(spec)
            self._specs = prepared
        return self._specs

    def get_spec(self, name: str) -> FieldSpec | None:
        for spec in self.get_field_specs():
            if spec.name == name:
                return spec
        return None

    def spec_map(self) -> dict[str, FieldSpec]:
        return {spec.name: spec for spec in self.get_field_specs()}

    def get_form_specs(self, ctx: AdminContext | None = None) -> list[FieldSpec]:
        specs = self.get_field_specs()
        by_name = {spec.name: spec for spec in specs}
        fields = self.fields if ctx is None else self.get_fields(ctx)
        excluded = set(self.exclude if ctx is None else self.get_exclude(ctx))
        if fields is not None:
            return [
                by_name[name]
                for name in fields
                if name in by_name
                and name not in excluded
                and not by_name[name].options.get("through_payload")
            ]
        return [
            spec
            for spec in specs
            if spec.name not in excluded
            and not spec.secret
            and not spec.options.get("through_payload")
        ]

    def get_form_layout(
        self, specs: Sequence[FieldSpec], ctx: AdminContext | None = None
    ) -> list[dict[str, Any]]:
        """Resolve fieldsets to safe, ordered rows of declared fields."""
        by_name = {spec.name: spec for spec in specs}
        configured = self.fieldsets if ctx is None else self.get_fieldsets(ctx)
        fields = self.fields if ctx is None else self.get_fields(ctx)
        fieldsets = configured or [(None, {"fields": fields or tuple(by_name)})]
        layout = []
        for title, options in fieldsets:
            rows = []
            for entry in options.get("fields", ()):
                if isinstance(entry, str):
                    names = (entry,)
                elif callable(entry):
                    names = (entry.__name__,)
                else:
                    names = tuple(entry)
                row = [by_name[name] for name in names if name in by_name]
                if row:
                    rows.append(row)
            layout.append(
                {
                    "title": title,
                    "description": options.get("description", ""),
                    "classes": tuple(options.get("classes", ())),
                    "rows": rows,
                }
            )
        return layout

    def get_list_specs(self, ctx: AdminContext | None = None) -> list[FieldSpec]:
        specs = self.get_field_specs()
        display = self.list_display if ctx is None else self.get_list_display(ctx)
        if display:
            by_name = {spec.name: spec for spec in specs}
            columns = []
            for entry in display:
                if isinstance(entry, str) and entry in by_name:
                    columns.append(by_name[entry])
                    continue
                if isinstance(entry, str):
                    related = self._query_field(entry, ctx)
                    path = related.options.get("relationship_path") if related else None
                    if (
                        related is not None
                        and path
                        and not any(relationship.property.uselist for relationship in path)
                    ):
                        target_name = entry.rsplit("__", 1)[-1]
                        columns.append(
                            replace(
                                related,
                                label=humanize(entry.replace("__", "_")),
                                readonly=True,
                                editable=False,
                                options={
                                    **related.options,
                                    "display_relationship": True,
                                    "display_attribute": target_name,
                                },
                            )
                        )
                        continue
                method = (
                    entry
                    if callable(entry)
                    else getattr(type(self), entry, None) or getattr(self.model, entry, None)
                )
                if not callable(method):
                    continue
                name = method.__name__
                ordering = getattr(method, "admin_ordering", None)
                ordered = by_name.get(ordering) if ordering else None
                columns.append(
                    FieldSpec(
                        name=name,
                        label=getattr(method, "admin_description", humanize(name)),
                        type=Text(),
                        column=ordered.column if ordered else None,
                        readonly=True,
                        editable=False,
                        options={
                            "computed": True,
                            "display_callable": method,
                            "display_admin_method": callable(entry)
                            and hasattr(type(self), name)
                            or isinstance(entry, str)
                            and hasattr(type(self), name),
                            "display_boolean": bool(getattr(method, "admin_boolean", False)),
                        },
                    )
                )
            return columns
        visible = [spec for spec in specs if not spec.secret]
        primary = [spec for spec in visible if spec.primary_key]
        rest = [spec for spec in visible if not spec.primary_key][:5]
        return primary + rest

    def get_inline_specs(self, ctx: AdminContext | None = None) -> list[FieldSpec]:
        if ctx is None and self._inline_specs is not None:
            return self._inline_specs
        if ctx is not None or self._inline_specs is None:
            specs = self.spec_map()
            inline: list[FieldSpec] = []
            editable = self.list_editable if ctx is None else self.get_list_editable(ctx)
            for name in editable:
                spec = specs.get(name)
                if spec is None or spec.primary_key or not spec.is_editable():
                    continue
                if spec.column is None or spec.type.name not in INLINE_TYPES:
                    continue
                inline.append(spec)
            if ctx is None:
                self._inline_specs = inline
            return inline
        return self._inline_specs

    def _query_field(self, name: str, ctx: AdminContext | None = None) -> FieldSpec | None:
        direct = self.spec_map().get(name)
        if direct is not None:
            return direct
        parts = name.split("__")
        if len(parts) < 2 or len(parts) > 4:
            return None
        from dataclasses import replace

        model = self.model
        path = []
        scopes = []
        for relationship_name in parts[:-1]:
            relationship = sa.inspect(model).relationships.get(relationship_name)
            if relationship is None:
                return None
            path.append(getattr(model, relationship_name))
            model = relationship.mapper.class_
            related_admin = self.site.get_model_admin_for(model) if self.site else None
            related_admin = related_admin or ModelAdmin(model, self.site)
            if ctx is not None:
                related_ctx = replace(ctx, admin=related_admin, obj=None)
                if (
                    not related_admin.has_view_permission(related_ctx)
                    or type(related_admin).has_view_permission is not ModelAdmin.has_view_permission
                ):
                    return None
                related_query = related_admin.get_queryset(related_ctx)
            else:
                related_query = sa.select(model)
            primary_key = sa.inspect(model).primary_key[0]
            scopes.append(related_query.with_only_columns(primary_key).order_by(None))
        target_spec = next(
            (spec for spec in introspect_model(model) if spec.name == parts[-1]), None
        )
        if target_spec is None or target_spec.column is None or target_spec.secret:
            return None
        return replace(
            target_spec,
            name=name,
            options={
                **target_spec.options,
                "relationship_path": tuple(path),
                "relationship_scopes": tuple(scopes),
            },
        )

    def get_query_specs(self, ctx: AdminContext | None = None) -> dict[str, FieldSpec]:
        specs = self.spec_map()
        specs.update(
            {spec.name: spec for spec in self.get_list_specs(ctx) if spec.options.get("computed")}
        )
        filters = self.list_filter if ctx is None else self.get_list_filter(ctx)
        search_fields = self.search_fields if ctx is None else self.get_search_fields(ctx)
        names = [name for name in filters if isinstance(name, str)]
        names.extend(
            entry[1:] if entry[:1] in {"^", "=", "@"} else entry for entry in search_fields
        )
        for name in names:
            spec = self._query_field(name, ctx)
            if spec is not None:
                specs[name] = spec
        return specs

    def get_inline_schema(self, ctx: AdminContext | None = None) -> type[BaseModel]:
        if ctx is not None:
            return build_schema(
                self.get_inline_specs(ctx),
                name=f"{self.app_label}_{self.model_name}",
                mode="inline",
            )
        if self._inline_schema is None:
            self._inline_schema = build_schema(
                self.get_inline_specs(),
                name=f"{self.app_label}_{self.model_name}",
                mode="inline",
            )
        return self._inline_schema

    # -- bulk actions -------------------------------------------------------
    def get_actions(self, ctx: AdminContext) -> list[ActionSpec]:
        """Actions available to ``ctx.user``: the built-in delete plus ``actions``."""
        if self.actions is None:
            return []
        specs: list[ActionSpec] = []
        seen: set[str] = set()
        for entry in (delete_selected, *self.actions):
            spec = self._resolve_action(entry)
            if spec is None or spec.name in seen:
                continue
            seen.add(spec.name)
            if self._action_allowed(spec, ctx):
                specs.append(spec)
        return specs

    def _resolve_action(self, entry: str | Callable[..., Any]) -> ActionSpec | None:
        if isinstance(entry, str):
            method = getattr(type(self), entry, None)
            if method is None:
                return None
            name = entry
            func: Callable[..., Awaitable[Any]] = getattr(self, entry)
        else:
            method = entry
            name = entry.__name__
            func = functools.partial(entry, self)
        description = getattr(method, "description", "") or name.replace("_", " ").capitalize()
        if "%(" in description:
            description = description % {
                "verbose_name": self.name.lower(),
                "verbose_name_plural": self.plural_name.lower(),
            }
        permissions = tuple(getattr(method, "allowed_permissions", ()))
        return ActionSpec(
            name=name,
            description=description,
            permissions=permissions,
            func=func,
            api=bool(getattr(method, "api_enabled", False)),
        )

    def _action_allowed(self, spec: ActionSpec, ctx: AdminContext, obj: Any = None) -> bool:
        if not spec.permissions:
            return True
        for perm in spec.permissions:
            checker = getattr(self, f"has_{perm}_permission", None)
            if checker is not None and (checker(ctx) if perm == "add" else checker(ctx, obj)):
                return True
        return False

    def get_search_specs(self, ctx: AdminContext | None = None) -> list[FieldSpec]:
        specs = self.spec_map()
        result = []
        search_fields = self.search_fields if ctx is None else self.get_search_fields(ctx)
        for entry in search_fields:
            mode = "contains"
            name = entry
            if name[:1] in {"^", "=", "@"}:
                mode = "startswith" if name.startswith("^") else "exact"
                if name.startswith("@"):
                    mode = "contains"
                name = name[1:]
            spec = self._query_field(name, ctx) or specs.get(name)
            if (
                spec is not None
                and spec.column is not None
                and spec.type.searchable
                and not spec.secret
            ):
                from dataclasses import replace

                result.append(replace(spec, options={**spec.options, "search_mode": mode}))
        return result

    def get_filter_specs(self, ctx: AdminContext | None = None) -> list[FieldSpec]:
        filters = self.list_filter if ctx is None else self.get_list_filter(ctx)
        return [
            spec
            for name in filters
            if isinstance(name, str)
            and (spec := self._query_field(name, ctx)) is not None
            and spec.column is not None
        ]

    def get_form_schema(self, mode: str = "add") -> type[BaseModel]:
        if self._schema is None or self._schema_mode != mode:
            self._schema = build_schema(
                self.get_form_specs(), name=f"{self.app_label}_{self.model_name}", mode=mode
            )
            self._schema_mode = mode
        return self._schema

    def get_form_fields(self, ctx: AdminContext) -> list[FieldSpec]:
        """Request-specific fields; ``ctx.obj`` is the existing row on change."""
        specs = self.get_form_specs(ctx)
        extra_fields = list(self.get_extra_form_fields(ctx))
        names = set(sa.inspect(self.model).attrs.keys())
        names.update(spec.name for spec in specs)
        for spec in extra_fields:
            if not isinstance(spec, FieldSpec):
                raise ImproperlyConfigured("Extra form fields must be FieldSpec instances.")
            if spec.name in names:
                raise ImproperlyConfigured(
                    f"Extra form field {spec.name!r} duplicates a field on {self.model_label}."
                )
            names.add(spec.name)
        specs.extend(extra_fields)
        fields = self.get_fields(ctx)
        if fields is not None:
            by_name = {spec.name: spec for spec in specs}
            specs = [by_name[name] for name in fields if name in by_name]
        readonly_fields = self.get_readonly_fields(ctx)
        readonly_names = {
            entry if isinstance(entry, str) else entry.__name__ for entry in readonly_fields
        }
        specs = [
            replace(spec, readonly=True, editable=False)
            if spec.name in readonly_names and not spec.readonly
            else spec
            for spec in specs
        ]
        names = {spec.name for spec in specs}
        for entry in readonly_fields:
            method = (
                entry
                if callable(entry)
                else getattr(type(self), entry, None) or getattr(self.model, entry, None)
            )
            if not callable(method):
                continue
            name = method.__name__
            if name not in names:
                specs.append(
                    FieldSpec(
                        name=name,
                        label=getattr(method, "admin_description", humanize(name)),
                        type=Text(),
                        readonly=True,
                        editable=False,
                        options={
                            "computed": True,
                            "display_callable": method,
                            "display_admin_method": hasattr(type(self), name),
                            "display_boolean": bool(getattr(method, "admin_boolean", False)),
                        },
                    )
                )
                names.add(name)
        return specs

    def get_form(self, ctx: AdminContext, *, change: bool = False) -> type[BaseModel]:
        """Override to provide custom validation without sharing request state."""
        return build_schema(
            self.get_form_fields(ctx),
            name=f"{self.app_label}_{self.model_name}",
            mode="change" if change else "add",
        )

    async def save_model(
        self, ctx: AdminContext, obj: Any, form: BoundForm, *, change: bool
    ) -> None:
        """Save the validated row inside the caller's transaction; do not commit."""
        ctx.db.add(obj)
        await ctx.db.flush()

    async def save_related(
        self, ctx: AdminContext, obj: Any, form: BoundForm, *, change: bool
    ) -> None:
        """Save related data after ``save_model``, in the same transaction."""
        from obox_builder.contrib.admin.relations import save_relations

        await save_relations(ctx, obj, form)

    def get_inline_instances(self, ctx: AdminContext) -> list[InlineModelAdmin]:
        return [inline(self.model, self.site) for inline in self.inlines]

    async def delete_model(self, ctx: AdminContext, obj: Any) -> None:
        await ctx.db.delete(obj)

    async def delete_queryset(self, ctx: AdminContext, queryset: sa.Select[Any]) -> None:
        objects = (
            (await ctx.db.scalars(queryset.order_by(None).order_by(self.pk_column())))
            .unique()
            .all()
        )
        for obj in objects:
            ctx.obj = obj
            await self.delete_model(ctx, obj)

    async def response_add(self, ctx: AdminContext, obj: Any) -> Response:
        return await self._response_save(ctx, obj)

    async def response_change(self, ctx: AdminContext, obj: Any) -> Response:
        return await self._response_save(ctx, obj)

    async def response_delete(self, ctx: AdminContext, object_id: Any) -> Response:
        return RedirectResponse(self.list_path(), status_code=303)

    async def _response_save(self, ctx: AdminContext, obj: Any) -> Response:
        data = await ctx.request.form()
        action = data.get("_action")
        destination = self.list_path()
        if action == "add_another" and self.has_add_permission(ctx):
            destination = self.add_path()
        elif (
            action == "continue"
            and self.has_view_permission(ctx, obj)
            or (
                action == "save_as_new"
                and self.save_as_continue
                and self.has_change_permission(ctx, obj)
            )
        ):
            destination = self.change_path(self.get_pk(obj))
        return RedirectResponse(destination, status_code=303)

    # -- cells --------------------------------------------------------------
    def build_cell(self, spec: FieldSpec, obj: Any) -> Cell:
        if spec.options.get("computed"):
            method = spec.options["display_callable"]
            if spec.options.get("display_admin_method"):
                value = getattr(self, method.__name__)(obj)
            else:
                value = getattr(obj, method.__name__)
                if callable(value):
                    value = value()
            if spec.options.get("display_boolean"):
                return Cell(text="Yes" if value else "No", value=bool(value), kind="boolean")
            return Cell(text="" if value is None else str(value), value=value)
        attr = spec.options.get("value_attr", spec.name)
        value = getattr(obj, attr, None)
        display = _display_value(value) if spec.type.name == "link" else value
        cell = spec.type.cell(display, spec)
        if spec.type.name == "file" and value:
            from dataclasses import replace

            cell = replace(cell, url=f"{self.change_path(self.get_pk(obj))}/files/{spec.name}")
        if spec.type.name == "link" and value is not None:
            url = self._link_url(spec, pk_value(value))
            if url:
                from dataclasses import replace

                cell = replace(cell, url=url, value=pk_value(value))
        return cell

    def _link_url(self, spec: FieldSpec, pk: Any) -> str | None:
        target = spec.options.get("target")
        if target is None or self.site is None or pk is None:
            return None
        target_admin = self.site.get_model_admin_for(target)
        if target_admin is None:
            return None
        return target_admin.change_path(pk)

    # -- primary keys -------------------------------------------------------
    def pk_column(self) -> sa.Column[Any]:
        return sa.inspect(self.model).primary_key[0]  # type: ignore[union-attr]

    def get_pk(self, obj: Any) -> Any:
        return getattr(obj, self.pk_column().key, None)

    def parse_pk(self, raw: str) -> Any:
        python_type = self.pk_column().type.python_type
        try:
            return python_type(raw)
        except (TypeError, ValueError):
            raise LookupError(f"Invalid {self.model_name} id {raw!r}.") from None

    # -- system checks ------------------------------------------------------
    def validate(self) -> list[CheckMessage]:
        from obox_builder.contrib.admin.validation import validate_admin

        return validate_admin(self)


def pk_value(value: Any) -> Any:
    """Return the primary key of an instance, or the scalar itself."""
    if value is None or isinstance(value, (int, str)):
        return value
    mapper = sa.inspect(type(value))
    if mapper is None:  # pragma: no cover - defensive
        return None
    primary_key = mapper.primary_key
    if not primary_key:  # pragma: no cover - defensive
        return None
    return getattr(value, primary_key[0].key, None)


def _display_value(value: Any) -> Any:
    """Human label for a related instance: ``__str__``, else ``#pk``."""
    text = str(value)
    if not text.startswith("<"):
        return text
    mapper = sa.inspect(type(value))
    if mapper is None or not mapper.primary_key:  # pragma: no cover - defensive
        return text
    pk = getattr(value, mapper.primary_key[0].key, None)
    return f"#{pk}" if pk is not None else text
