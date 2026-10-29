"""System checks for ModelAdmin declarations."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import sqlalchemy as sa

from obox_builder.contrib.admin.options import (
    _AUTO_LIST_DISPLAY_LINKS,
    BASE_PATH_RE,
    INLINE_TYPES,
    ModelAdmin,
    SimpleListFilter,
)
from obox_builder.core.checks.messages import CheckMessage, Error, Warning
from obox_builder.exceptions import ImproperlyConfigured
from obox_builder.fields.introspect import ADMIN_WIDGETS


def validate_admin(admin: ModelAdmin) -> list[CheckMessage]:
    messages: list[CheckMessage] = []
    specs = admin.spec_map()
    for entry in admin.list_display:
        name = entry if isinstance(entry, str) else entry.__name__
        method = (
            getattr(type(admin), name, None) or getattr(admin.model, name, None)
            if isinstance(entry, str)
            else entry
        )
        display_column = next(
            (column for column in admin.get_list_specs() if column.name == name), None
        )
        if (
            isinstance(entry, str)
            and name not in specs
            and not callable(method)
            and display_column is None
        ):
            messages.append(
                Error(
                    f"{admin.model_label}: list_display field {name!r} is unknown.",
                    id="obox.admin.E111",
                )
            )
    for column in admin.get_list_specs():
        if column.column is None and not column.options.get("computed"):
            messages.append(
                Error(
                    f"{admin.model_label}: list_display field {column.name!r} is unknown.",
                    id="obox.admin.E111",
                )
            )
    for spec in specs.values():
        if spec.type.name == "file" and (
            spec.column is None or not isinstance(spec.column.type, (sa.String, sa.Unicode))
        ):
            messages.append(
                Error(
                    f"{admin.model_label}: file field {spec.name!r} must use a text column.",
                    id="obox.admin.E114",
                )
            )
        widget = spec.options.get("widget")
        if widget is not None and (not isinstance(widget, str) or widget not in ADMIN_WIDGETS):
            messages.append(
                Error(
                    f"{admin.model_label}: field {spec.name!r} uses unknown widget {widget!r}.",
                    id="obox.admin.E115",
                )
            )
        elif isinstance(widget, str):
            compatible_types = {
                "checkbox": {"checkbox"},
                "date": {"date"},
                "datetime": {"datetime"},
                "email": {"email", "text"},
                "file": {"file"},
                "json": {"json"},
                "multiselect": {"multi_link"},
                "number": {"integer", "decimal"},
                "select": {"link", "single_select"},
                "textarea": {"json", "long_text", "text"},
                "time": {"time"},
                "url": {"text", "url"},
            }
            expected_types = compatible_types.get(widget)
            if expected_types is not None and spec.type.name not in expected_types:
                messages.append(
                    Error(
                        f"{admin.model_label}: widget {widget!r} is incompatible with "
                        f"field {spec.name!r} of type {spec.type.name!r}.",
                        id="obox.admin.E115",
                    )
                )
        rows = spec.options.get("rows")
        if rows is not None and (
            not isinstance(rows, int) or isinstance(rows, bool) or not 1 <= rows <= 40
        ):
            messages.append(
                Error(
                    f"{admin.model_label}: field {spec.name!r} widget rows must be between "
                    "1 and 40.",
                    id="obox.admin.E115",
                )
            )
    if admin.fieldsets is not None:
        for _title, options in admin.fieldsets:
            if not isinstance(options, Mapping) or not options.get("fields"):
                messages.append(
                    Error(
                        f"{admin.model_label}: each fieldset must define a non-empty fields list.",
                        id="obox.admin.E112",
                    )
                )
                continue
            for entry in options.get("fields", ()):
                names = (
                    (getattr(entry, "__name__", entry),)
                    if isinstance(entry, str) or callable(entry)
                    else entry
                )
                for name in names:
                    if name not in specs and name not in {
                        getattr(item, "__name__", item) for item in admin.readonly_fields
                    }:
                        messages.append(
                            Error(
                                f"{admin.model_label}: fieldset references unknown field {name!r}.",
                                id="obox.admin.E112",
                            )
                        )
    for name in admin.autocomplete_fields:
        field_spec = specs.get(name)
        target = field_spec.options.get("target") if field_spec is not None else None
        target_admin = (
            admin.site.get_model_admin_for(target)
            if admin.site is not None and target is not None
            else None
        )
        if (
            field_spec is None
            or field_spec.type.name not in {"link", "multi_link"}
            or not field_spec.is_editable()
            or target_admin is None
            or not target_admin.get_search_specs()
        ):
            messages.append(
                Error(
                    f"{admin.model_label}: autocomplete field {name!r} requires "
                    "an editable relation "
                    "and a registered target admin with search_fields.",
                    id="obox.admin.E109",
                )
            )
    from obox_builder.contrib.admin.inlines import InlineFormSet, InlineModelAdmin

    for inline_class in admin.inlines:
        try:
            if not isinstance(inline_class, type) or not issubclass(inline_class, InlineModelAdmin):
                raise ImproperlyConfigured("inlines must contain InlineModelAdmin subclasses.")
            inline = inline_class(admin.model, admin.site)
            messages.extend(inline.validate())
            formset_class = inline.formset_class
            if formset_class is not None and (
                not isinstance(formset_class, type) or not issubclass(formset_class, InlineFormSet)
            ):
                messages.append(
                    Error(
                        f"{admin.model_label}: inline formset_class must subclass InlineFormSet.",
                        id="obox.admin.E117",
                    )
                )
            if not isinstance(inline.show_change_link, bool):
                messages.append(
                    Error(
                        f"{admin.model_label}: inline show_change_link must be a boolean.",
                        id="obox.admin.E117",
                    )
                )
        except ImproperlyConfigured as exc:
            messages.append(Error(f"{admin.model_label}: {exc}", id="obox.admin.E110"))
    if admin.base_path is not None and not BASE_PATH_RE.match(admin.base_path.strip("/")):
        messages.append(
            Error(
                f"{admin.model_label}: base_path {admin.base_path!r} is not a valid URL "
                "segment; use lowercase letters, digits, '/', '-' or '_'.",
                id="obox.admin.E106",
            )
        )
    related_display_names = {
        spec.name for spec in admin.get_list_specs() if spec.options.get("display_relationship")
    }
    for entry in admin.list_display:
        field_name = entry if isinstance(entry, str) else entry.__name__
        method = getattr(type(admin), field_name, None) or getattr(admin.model, field_name, None)
        if (
            field_name not in specs
            and field_name not in related_display_names
            and not callable(method)
        ):
            messages.append(
                Error(
                    f"{admin.model_label}: list_display references unknown field {field_name!r}.",
                    id="obox.admin.E102",
                )
            )
    for field_name in admin.fields or ():
        if field_name not in specs:
            messages.append(
                Error(
                    f"{admin.model_label}: fields references unknown field {field_name!r}.",
                    id="obox.admin.E102",
                )
            )
    for entry in admin.readonly_fields:
        field_name = entry if isinstance(entry, str) else entry.__name__
        method = getattr(type(admin), field_name, None) or getattr(admin.model, field_name, None)
        if field_name not in specs and not callable(method):
            messages.append(
                Error(
                    f"{admin.model_label}: readonly_fields references unknown field "
                    f"{field_name!r}.",
                    id="obox.admin.E102",
                )
            )
    for field_name in admin.exclude:
        if field_name not in specs:
            messages.append(
                Error(
                    f"{admin.model_label}: exclude references unknown field {field_name!r}.",
                    id="obox.admin.E102",
                )
            )
    for name in admin.search_fields:
        field_name = name[1:] if name[:1] in {"^", "=", "@"} else name
        field_spec = admin._query_field(field_name)
        if "." in field_name:
            messages.append(
                Error(
                    f"{admin.model_label}: search_fields {name!r} must use '__' "
                    "between relationship names.",
                    id="obox.admin.E103",
                )
            )
        elif field_spec is None or field_spec.column is None:
            messages.append(
                Error(
                    f"{admin.model_label}: search_fields references unknown field {name!r}.",
                    id="obox.admin.E103",
                )
            )
        elif not field_spec.type.searchable:
            messages.append(
                Error(
                    f"{admin.model_label}: search field {name!r} of type "
                    f"{field_spec.type.name!r} cannot be searched.",
                    id="obox.admin.E103",
                )
            )
    for filter_item in admin.list_filter:
        if isinstance(filter_item, type) and issubclass(filter_item, SimpleListFilter):
            if not filter_item.title or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_]*", filter_item.parameter_name
            ):
                messages.append(
                    Error(
                        f"{admin.model_label}: custom list filters need a title and a valid "
                        "parameter_name.",
                        id="obox.admin.E104",
                    )
                )
            continue
        if not isinstance(filter_item, str):
            messages.append(
                Error(
                    f"{admin.model_label}: list_filter entries must be field names or "
                    "SimpleListFilter subclasses.",
                    id="obox.admin.E104",
                )
            )
            continue
        field_spec = admin._query_field(filter_item)
        if field_spec is None or field_spec.column is None:
            messages.append(
                Error(
                    f"{admin.model_label}: list_filter references unknown field {filter_item!r}.",
                    id="obox.admin.E104",
                )
            )
    for target, sources in admin.prepopulated_fields.items():
        if target not in specs or not sources or any(source not in specs for source in sources):
            messages.append(
                Error(
                    f"{admin.model_label}: prepopulated_fields must reference known fields "
                    "and at least one source field.",
                    id="obox.admin.E113",
                )
            )
    list_specs = admin.get_list_specs()
    display_names = {spec.name for spec in list_specs}
    link_config = admin.list_display_links
    if isinstance(link_config, str):
        messages.append(
            Error(
                f"{admin.model_label}: list_display_links must be a sequence of field names, "
                "None, or omitted.",
                id="obox.admin.E116",
            )
        )
        link_names: tuple[str, ...] = ()
    elif link_config is None or link_config is _AUTO_LIST_DISPLAY_LINKS:
        link_names = admin.get_list_display_links()
    elif isinstance(link_config, Sequence):
        link_names = tuple(link_config)
    else:
        messages.append(
            Error(
                f"{admin.model_label}: list_display_links must be a sequence of field names, "
                "None, or omitted.",
                id="obox.admin.E116",
            )
        )
        link_names = ()
    if any(not isinstance(name, str) for name in link_names):
        messages.append(
            Error(
                f"{admin.model_label}: list_display_links values must be field names.",
                id="obox.admin.E116",
            )
        )
        link_names = tuple(name for name in link_names if isinstance(name, str))
    if len(link_names) != len(set(link_names)):
        messages.append(
            Error(
                f"{admin.model_label}: list_display_links contains duplicate fields.",
                id="obox.admin.E116",
            )
        )
    for name in link_names:
        if name not in display_names:
            messages.append(
                Error(
                    f"{admin.model_label}: list_display_links field {name!r} is not in "
                    "list_display.",
                    id="obox.admin.E116",
                )
            )
    linked_names = set(link_names)
    for name in admin.list_editable:
        field_spec = specs.get(name)
        if field_spec is None:
            messages.append(
                Error(
                    f"{admin.model_label}: list_editable references unknown field {name!r}.",
                    id="obox.admin.E107",
                )
            )
        elif name not in admin.list_display:
            messages.append(
                Error(
                    f"{admin.model_label}: list_editable field {name!r} is not in list_display.",
                    id="obox.admin.E107",
                )
            )
        elif name in linked_names:
            messages.append(
                Error(
                    f"{admin.model_label}: list_editable field {name!r} cannot also be a "
                    "list_display_links field.",
                    id="obox.admin.E107",
                )
            )
        elif field_spec.primary_key or not field_spec.is_editable() or field_spec.column is None:
            messages.append(
                Error(
                    f"{admin.model_label}: list_editable field {name!r} cannot be "
                    "edited through the admin.",
                    id="obox.admin.E107",
                )
            )
        elif field_spec.type.name not in INLINE_TYPES:
            messages.append(
                Error(
                    f"{admin.model_label}: list_editable field {name!r} of type "
                    f"{field_spec.type.name!r} does not support inline editing.",
                    id="obox.admin.E107",
                )
            )
    for entry in admin.actions or ():
        if not isinstance(entry, str):
            continue
        method = getattr(type(admin), entry, None)
        if method is None:
            messages.append(
                Error(
                    f"{admin.model_label}: actions references unknown method {entry!r}.",
                    id="obox.admin.E108",
                )
            )
            continue
        for perm in getattr(method, "allowed_permissions", ()):
            if not hasattr(admin, f"has_{perm}_permission"):
                messages.append(
                    Error(
                        f"{admin.model_label}: action {entry!r} declares unknown "
                        f"permission {perm!r}.",
                        id="obox.admin.E108",
                    )
                )
    for name in admin.get_ordering(None):  # type: ignore[arg-type]
        bare = name[1:] if name.startswith("-") else name
        field_spec = specs.get(bare)
        if field_spec is None or field_spec.column is None:
            messages.append(
                Error(
                    f"{admin.model_label}: ordering references unknown field {name!r}.",
                    id="obox.admin.E105",
                )
            )
        elif not field_spec.type.sortable:
            messages.append(
                Error(
                    f"{admin.model_label}: ordering field {name!r} cannot be sorted.",
                    id="obox.admin.E105",
                )
            )
    query_names = set(admin.search_fields)
    query_names.update(name for name in admin.list_filter if isinstance(name, str))
    for name in query_names:
        field_name = name[1:] if name[:1] in {"^", "=", "@"} else name
        field_spec = admin._query_field(field_name)
        if field_spec is None or field_spec.column is None:
            continue
        if (
            not field_spec.column.index
            and not field_spec.column.unique
            and not field_spec.column.primary_key
        ):
            messages.append(
                Warning(
                    f"{admin.model_label}: field {name!r} is searched or filtered "
                    "but has no index.",
                    id="obox.admin.W101",
                )
            )
    for spec in admin.get_field_specs():
        if spec.options.get("unmapped"):
            messages.append(
                Warning(
                    f"{admin.model_label}: field {spec.name!r} of column type "
                    f"{spec.options.get('column_type')!r} has no dedicated field type; "
                    "it is treated as text.",
                    id="obox.admin.W102",
                )
            )
    return messages
