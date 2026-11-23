"""Discover literal component declarations in route templates."""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from jinja2 import Environment, meta, nodes
from jinja2.exceptions import TemplateNotFound, TemplateSyntaxError

from obox.core.exceptions import ImproperlyConfigured


@dataclass(frozen=True)
class ComponentUse:
    ref: str
    key: str | None
    props: dict[str, Any]
    signature: str
    template: str
    line: int


@dataclass(frozen=True)
class TemplateScan:
    uses: tuple[ComponentUse, ...]
    templates: tuple[str, ...]
    diagnostics: tuple[TemplateDiagnostic, ...]


@dataclass(frozen=True)
class TemplateDiagnostic:
    code: str
    message: str


def component_signature(ref: str, props: dict[str, Any]) -> str:
    return json.dumps([ref, props], sort_keys=True, separators=(",", ":"), allow_nan=False)


def _literal(node: nodes.Expr) -> Any:
    if isinstance(node, nodes.Const):
        return node.value
    if isinstance(node, (nodes.Tuple, nodes.List)):
        values = [_literal(item) for item in node.items]
        return tuple(values) if isinstance(node, nodes.Tuple) else values
    if isinstance(node, nodes.Dict):
        return {_literal(item.key): _literal(item.value) for item in node.items}
    if isinstance(node, nodes.Neg):
        return -_literal(node.node)
    if isinstance(node, nodes.Pos):
        return +_literal(node.node)
    raise ValueError("value is not a literal")


def scan_template(env: Environment, name: str, *, base_template: str) -> TemplateScan:
    pending = deque([name])
    scanned: set[str] = set()
    uses: dict[str, ComponentUse] = {}
    diagnostics: list[TemplateDiagnostic] = []

    while pending:
        current = pending.popleft()
        if current in scanned:
            continue
        scanned.add(current)
        try:
            source, _, _ = env.loader.get_source(env, current)  # type: ignore[union-attr]
            tree = env.parse(source, name=current)
        except TemplateNotFound:
            diagnostics.append(
                TemplateDiagnostic("missing_template", f"Missing template: {current}")
            )
            continue
        except TemplateSyntaxError as exc:
            raise ImproperlyConfigured(f"Invalid template {current!r}: {exc}") from exc

        references = tree.find_all((nodes.Extends, nodes.Include, nodes.Import, nodes.FromImport))
        for reference in references:
            reference = cast(Any, reference)
            expression = reference.template
            if (
                isinstance(reference, nodes.Extends)
                and isinstance(expression, nodes.Name)
                and expression.name == "base_template"
            ):
                reference.template = nodes.Const(base_template)

        for referenced in meta.find_referenced_templates(tree):
            if referenced is None:
                diagnostics.append(
                    TemplateDiagnostic(
                        "dynamic_template_reference",
                        f"Dynamic template reference in {current}; it was not scanned.",
                    )
                )
            else:
                pending.append(referenced)

        for call in tree.find_all(nodes.Call):
            if not isinstance(call.node, nodes.Name) or call.node.name != "component":
                continue
            try:
                if len(call.args) != 1 or call.dyn_args is not None or call.dyn_kwargs is not None:
                    raise ValueError("component() requires one literal reference")
                ref = _literal(call.args[0])
                props: dict[str, Any] = {}
                key = None
                options: set[str] = set()
                for item in call.kwargs:
                    if item.key in options:
                        raise ValueError(f"option {item.key!r} is repeated")
                    options.add(item.key)
                    value = _literal(item.value)
                    if item.key == "key":
                        key = value
                    elif item.key in {"title", "filters"}:
                        props[item.key] = value
                    else:
                        raise ValueError(f"unsupported option {item.key!r}")
                if not isinstance(ref, str) or not ref:
                    raise ValueError("component reference must be a nonempty import path")
                if key is not None and not isinstance(key, str):
                    raise ValueError("component key must be a string")
                if (
                    "title" in props
                    and props["title"] is not None
                    and not isinstance(props["title"], str)
                ):
                    raise ValueError("component title must be a string")
                if "filters" in props and not isinstance(props["filters"], dict):
                    raise ValueError("component filters must be a literal mapping")
                signature = component_signature(ref, props)
                use = ComponentUse(ref, key, props, signature, current, call.lineno)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ImproperlyConfigured(
                    f"Invalid component() call in {current}:{call.lineno}: {exc}."
                ) from exc
            previous = uses.get(signature)
            if previous is not None and previous.key != use.key:
                raise ImproperlyConfigured(
                    f"Component {ref!r} uses conflicting keys in {current}:{call.lineno}."
                )
            uses.setdefault(signature, use)

    return TemplateScan(tuple(uses.values()), tuple(scanned), tuple(diagnostics))
