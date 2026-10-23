"""ModelAdmin: declarative CRUD configuration for one model."""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

import sqlalchemy as sa
from pydantic import BaseModel

from obox_builder.contrib.admin.actions import delete_selected
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.fields import Cell, FieldSpec, introspect_model
from obox_builder.forms import build_schema
from obox_builder.utils.strings import camel_to_snake, humanize

if TYPE_CHECKING:
    from obox_builder.contrib.admin.context import AdminContext
    from obox_builder.contrib.admin.site import AdminSite

logger = logging.getLogger(__name__)

DEFAULT_PER_PAGE = 50
BASE_PATH_RE = re.compile(r"^[a-z0-9][a-z0-9/_-]*$")

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


class ModelAdmin:
    """Configuration object for one model's CRUD screens.

    Subclass and register with ``@site.register(Model)``. The declared
    attributes keep Django's names (``list_display``, ``search_fields``, ...)
    so the mental model carries over.
    """

    _model: type[Any]

    list_display: ClassVar[Sequence[str]] = ()
    list_editable: ClassVar[Sequence[str]] = ()
    list_filter: ClassVar[Sequence[str]] = ()
    #: Bulk actions on the list page; strings name ModelAdmin methods,
    #: callables are used directly. ``None`` disables actions entirely.
    actions: ClassVar[Sequence[str | Callable[..., Any]] | None] = ()
    search_fields: ClassVar[Sequence[str]] = ()
    ordering: ClassVar[Sequence[str]] = ()
    fields: ClassVar[Sequence[str] | None] = None
    readonly_fields: ClassVar[Sequence[str]] = ()
    exclude: ClassVar[Sequence[str]] = ()
    fields_config: ClassVar[Mapping[str, Any]] = {}
    verbose_name: ClassVar[str] = ""
    verbose_name_plural: ClassVar[str] = ""
    per_page: ClassVar[int] = DEFAULT_PER_PAGE
    #: URL segment(s) under ``/admin``; defaults to ``<app_label>/<model_name>``.
    base_path: ClassVar[str | None] = None
    import_enabled: ClassVar[bool] = False
    import_keys: ClassVar[Sequence[str]] = ()

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
        self._model = model
        self.site = site
        self._specs: list[FieldSpec] | None = None
        self._schema: type[BaseModel] | None = None
        self._schema_mode: str = ""
        self._inline_schema: type[BaseModel] | None = None
        self._inline_specs: list[FieldSpec] | None = None

    @property
    def model(self) -> type[Any]:
        return self._model

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
    def permission_codename(self, action: str) -> str:
        return f"{action}_{self.model_name}"

    def permission_name(self, action: str) -> str:
        return f"{self.app_label}.{self.permission_codename(action)}"

    def has_view_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return bool(ctx.user.has_perm(self.permission_name("view")))

    def has_add_permission(self, ctx: AdminContext) -> bool:
        return bool(ctx.user.has_perm(self.permission_name("add")))

    def has_change_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return bool(ctx.user.has_perm(self.permission_name("change")))

    def has_delete_permission(self, ctx: AdminContext, obj: Any = None) -> bool:
        return bool(ctx.user.has_perm(self.permission_name("delete")))

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
            if spec.type.name == "link" and relationship:
                stmt = stmt.options(selectinload(getattr(self.model, str(relationship))))
        return stmt

    def get_ordering(self, ctx: AdminContext) -> tuple[str, ...]:
        if self.ordering:
            return tuple(self.ordering)
        return (self.pk_column().key or "id",)

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

    def get_form_specs(self) -> list[FieldSpec]:
        specs = self.get_field_specs()
        by_name = {spec.name: spec for spec in specs}
        if self.fields is not None:
            return [by_name[name] for name in self.fields if name in by_name]
        return [spec for spec in specs if not spec.secret]

    def get_list_specs(self) -> list[FieldSpec]:
        specs = self.get_field_specs()
        if self.list_display:
            by_name = {spec.name: spec for spec in specs}
            return [by_name[name] for name in self.list_display if name in by_name]
        visible = [spec for spec in specs if not spec.secret]
        primary = [spec for spec in visible if spec.primary_key]
        rest = [spec for spec in visible if not spec.primary_key][:5]
        return primary + rest

    def get_inline_specs(self) -> list[FieldSpec]:
        if self._inline_specs is None:
            specs = self.spec_map()
            inline: list[FieldSpec] = []
            for name in self.list_editable:
                spec = specs.get(name)
                if spec is None or spec.primary_key or not spec.is_editable():
                    continue
                if spec.column is None or spec.type.name not in INLINE_TYPES:
                    continue
                inline.append(spec)
            self._inline_specs = inline
        return self._inline_specs

    def get_inline_schema(self) -> type[BaseModel]:
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
        )

    def _action_allowed(self, spec: ActionSpec, ctx: AdminContext) -> bool:
        if not spec.permissions:
            return True
        for perm in spec.permissions:
            checker = getattr(self, f"has_{perm}_permission", None)
            if checker is not None and checker(ctx):
                return True
        return False

    def get_search_specs(self) -> list[FieldSpec]:
        specs = self.spec_map()
        result = []
        for name in self.search_fields:
            spec = specs.get(name)
            if (
                spec is not None
                and spec.column is not None
                and "." not in name
                and spec.type.searchable
                and not spec.secret
            ):
                result.append(spec)
        return result

    def get_filter_specs(self) -> list[FieldSpec]:
        specs = self.spec_map()
        return [
            spec
            for name in self.list_filter
            if (spec := specs.get(name)) is not None and spec.column is not None
        ]

    def get_form_schema(self, mode: str = "add") -> type[BaseModel]:
        if self._schema is None or self._schema_mode != mode:
            self._schema = build_schema(
                self.get_form_specs(), name=f"{self.app_label}_{self.model_name}", mode=mode
            )
            self._schema_mode = mode
        return self._schema

    # -- cells --------------------------------------------------------------
    def build_cell(self, spec: FieldSpec, obj: Any) -> Cell:
        attr = spec.options.get("value_attr", spec.name)
        value = getattr(obj, attr, None)
        display = _display_value(value) if spec.type.name == "link" else value
        cell = spec.type.cell(display, spec)
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
        messages: list[CheckMessage] = []
        specs = self.spec_map()
        if self.base_path is not None and not BASE_PATH_RE.match(self.base_path.strip("/")):
            messages.append(
                Error(
                    f"{self.model_label}: base_path {self.base_path!r} is not a valid URL "
                    "segment; use lowercase letters, digits, '/', '-' or '_'.",
                    id="obox.admin.E106",
                )
            )
        for names, what in (
            (self.list_display, "list_display"),
            (tuple(self.fields or ()), "fields"),
            (self.readonly_fields, "readonly_fields"),
            (self.exclude, "exclude"),
        ):
            for name in names:
                if name not in specs:
                    messages.append(
                        Error(
                            f"{self.model_label}: {what} references unknown field {name!r}.",
                            id="obox.admin.E102",
                        )
                    )
        for name in self.search_fields:
            spec = specs.get(name)
            if "." in name:
                messages.append(
                    Error(
                        f"{self.model_label}: search_fields {name!r} spans a relation; "
                        "relation search arrives with M11.",
                        id="obox.admin.E103",
                    )
                )
            elif spec is None or spec.column is None:
                messages.append(
                    Error(
                        f"{self.model_label}: search_fields references unknown field {name!r}.",
                        id="obox.admin.E103",
                    )
                )
            elif not spec.type.searchable:
                messages.append(
                    Error(
                        f"{self.model_label}: search field {name!r} of type "
                        f"{spec.type.name!r} cannot be searched.",
                        id="obox.admin.E103",
                    )
                )
        for name in self.list_filter:
            spec = specs.get(name)
            if spec is None or spec.column is None:
                messages.append(
                    Error(
                        f"{self.model_label}: list_filter references unknown field {name!r}.",
                        id="obox.admin.E104",
                    )
                )
        first_list_field = self.get_list_specs()[0].name if self.get_list_specs() else None
        for name in self.list_editable:
            spec = specs.get(name)
            if spec is None:
                messages.append(
                    Error(
                        f"{self.model_label}: list_editable references unknown field {name!r}.",
                        id="obox.admin.E107",
                    )
                )
            elif name not in self.list_display:
                messages.append(
                    Error(
                        f"{self.model_label}: list_editable field {name!r} is not in list_display.",
                        id="obox.admin.E107",
                    )
                )
            elif name == first_list_field:
                messages.append(
                    Error(
                        f"{self.model_label}: list_editable field {name!r} is the first "
                        "list_display column; inline editing it would clash with the "
                        "row link.",
                        id="obox.admin.E107",
                    )
                )
            elif spec.primary_key or not spec.is_editable() or spec.column is None:
                messages.append(
                    Error(
                        f"{self.model_label}: list_editable field {name!r} cannot be "
                        "edited through the admin.",
                        id="obox.admin.E107",
                    )
                )
            elif spec.type.name not in INLINE_TYPES:
                messages.append(
                    Error(
                        f"{self.model_label}: list_editable field {name!r} of type "
                        f"{spec.type.name!r} does not support inline editing.",
                        id="obox.admin.E107",
                    )
                )
        for entry in self.actions or ():
            if not isinstance(entry, str):
                continue
            method = getattr(type(self), entry, None)
            if method is None:
                messages.append(
                    Error(
                        f"{self.model_label}: actions references unknown method {entry!r}.",
                        id="obox.admin.E108",
                    )
                )
                continue
            for perm in getattr(method, "allowed_permissions", ()):
                if not hasattr(self, f"has_{perm}_permission"):
                    messages.append(
                        Error(
                            f"{self.model_label}: action {entry!r} declares unknown "
                            f"permission {perm!r}.",
                            id="obox.admin.E108",
                        )
                    )
        for name in self.get_ordering(None):  # type: ignore[arg-type]
            bare = name[1:] if name.startswith("-") else name
            spec = specs.get(bare)
            if spec is None or spec.column is None:
                messages.append(
                    Error(
                        f"{self.model_label}: ordering references unknown field {name!r}.",
                        id="obox.admin.E105",
                    )
                )
            elif not spec.type.sortable:
                messages.append(
                    Error(
                        f"{self.model_label}: ordering field {name!r} cannot be sorted.",
                        id="obox.admin.E105",
                    )
                )
        for name in set(self.search_fields) | set(self.list_filter):
            spec = specs.get(name)
            if spec is None or spec.column is None:
                continue
            if not spec.column.index and not spec.column.unique and not spec.column.primary_key:
                messages.append(
                    Warning(
                        f"{self.model_label}: field {name!r} is searched or filtered "
                        "but has no index.",
                        id="obox.admin.W101",
                    )
                )
        for spec in self.get_field_specs():
            if spec.options.get("unmapped"):
                messages.append(
                    Warning(
                        f"{self.model_label}: field {spec.name!r} of column type "
                        f"{spec.options.get('column_type')!r} has no dedicated field type; "
                        "it is treated as text.",
                        id="obox.admin.W102",
                    )
                )
        return messages


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
