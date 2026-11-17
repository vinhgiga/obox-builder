"""Validated predicates for analytical SQL selections."""

from typing import Any

import sqlalchemy as sa

from obox._exceptions import BuilderValidationError
from obox._filters import FilterGroup, FilterTerm
from obox._query import condition
from obox.db._fields import FILTER_EMPTY_OPERATORS, FILTER_OPERATORS, schema, validate


def _filter_condition(
    builder: Any, readable: set[str], field: str, op: str, value: Any, dialect: str = ""
) -> Any:
    if op.startswith("pg."):
        return condition(builder, readable, FilterTerm(field, op, value), dialect)
    if field not in builder.filter_fields or field not in readable:
        raise BuilderValidationError({field: ["Filtering this field is not permitted."]})
    column = getattr(builder.model, field)
    spec = builder.specs[field]
    if op not in FILTER_OPERATORS:
        raise BuilderValidationError({f"filter_op.{field}": ["Unknown filter operator."]})
    if "obox_files" in spec.column.info:
        if op in FILTER_EMPTY_OPERATORS:
            return ~column.any() if op == "is_empty" else column.any()
        raise BuilderValidationError({field: ["Attachments support empty/not-empty filters."]})
    if op in FILTER_EMPTY_OPERATORS:
        empty = column.is_(None)
        if isinstance(spec.column.type, sa.String):
            empty = sa.or_(empty, column == "")
        return empty if op == "is_empty" else sa.not_(empty)
    parsed = validate(schema("Filter", builder.specs, (field,), partial=True), {field: value})
    if op in {"contains", "does_not_contain"}:
        if not isinstance(spec.column.type, sa.String):
            raise BuilderValidationError(
                {f"filter_op.{field}": ["This operator requires a text field."]}
            )
        match = column.icontains(parsed[field], autoescape=True)
        if op == "does_not_contain":
            match = sa.not_(match)
            if spec.column.nullable:
                match = sa.or_(column.is_(None), match)
    else:
        match = column == parsed[field]
        if op == "is_not":
            match = column != parsed[field]
            if spec.column.nullable:
                match = sa.or_(column.is_(None), match)
    return match


def _filter_tree(
    builder: Any, readable: set[str], node: FilterGroup | FilterTerm, dialect: str
) -> Any | None:
    if isinstance(node, FilterTerm):
        return _filter_condition(builder, readable, node.field, node.op, node.value, dialect)
    children = [
        condition
        for child in node.children
        if (condition := _filter_tree(builder, readable, child, dialect)) is not None
    ]
    if not children:
        return None
    result = (sa.and_ if node.conjunction.removeprefix("not.") == "and" else sa.or_)(*children)
    return sa.not_(result) if node.conjunction.startswith("not.") else result
