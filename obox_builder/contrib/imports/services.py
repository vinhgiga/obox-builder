"""Caller-owned transactions for staging, planning and applying imports."""

from __future__ import annotations

from collections import Counter
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import DataError, DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.exc import StaleDataError

from obox_builder.apps import apps
from obox_builder.conf import settings
from obox_builder.contrib.imports.models import ImportRow, ImportSession
from obox_builder.contrib.imports.parser import ImportFileError, Spreadsheet, check_file
from obox_builder.contrib.imports.resources import ImportResource, digest
from obox_builder.core.exceptions import ImproperlyConfigured, PermissionDenied
from obox_builder.fields import RecordContext
from obox_builder.forms import BoundForm

BATCH_SIZE = 250
MODES = ("insert", "upsert", "update-only", "skip-existing")


def _check_session(session: ImportSession, resource: ImportResource) -> None:
    if session.owner_id != resource.user.id or session.model_label != resource.label:
        raise PermissionDenied("This import belongs to another user or model.")
    resource.check_configuration(session.mode)
    resource.check_permissions(session.mode)


def confirm_import(session: ImportSession, resource: ImportResource) -> None:
    _check_session(session, resource)
    if session.status != "review" or resource.signature != session.resource_signature:
        raise ValueError("Validate and review the current mapping before confirming.")
    session.status, session.processed = "queued", 0
    session.revision += 1


def cancel_import(session: ImportSession, resource: ImportResource) -> None:
    if session.owner_id != resource.user.id or session.model_label != resource.label:
        raise PermissionDenied("This import belongs to another user or model.")
    if session.status in {"completed", "cancelled"}:
        raise ValueError("This import has already finished.")
    session.status, session.source = "cancelled", None
    session.revision += 1


async def create_import(
    db: AsyncSession,
    resource: ImportResource,
    filename: str,
    content: bytes,
    *,
    mode: str = "insert",
) -> ImportSession:
    if not apps.is_installed("obox_builder.contrib.imports"):
        raise ImproperlyConfigured("Install contrib.imports before importing spreadsheets.")
    resource.check_configuration(mode)
    resource.check_permissions(mode)
    check_file(filename, content)
    session = ImportSession(
        owner_id=resource.user.id,
        model_label=resource.label,
        filename=filename[:255],
        source=content,
        mode=mode,
    )
    db.add(session)
    await db.flush()
    return session


def inspect_file(session: ImportSession) -> None:
    with Spreadsheet(session.filename, session.source or b"") as spreadsheet:
        session.sheets = spreadsheet.sheets
        if not session.sheets:
            raise ImportFileError("The workbook has no visible worksheets.")
        session.sample = spreadsheet.sample(session.sheets[0])
    session.status = "setup"


async def parse_file(db: AsyncSession, session: ImportSession) -> None:
    if session.header_row < 1 or session.header_row > 100:
        raise ValueError("The header row must be between 1 and 100.")
    await db.execute(sa.delete(ImportRow).where(ImportRow.session_id == session.id))
    batch: list[dict[str, Any]] = []
    count = 0
    session.headers = []
    with Spreadsheet(session.filename, session.source or b"") as spreadsheet:
        for number, values in spreadsheet.rows(session.sheet):
            if number > session.header_row + settings.IMPORT_MAX_ROWS:
                raise ImportFileError("The worksheet exceeds the row limit, including blank rows.")
            if number < session.header_row:
                continue
            if number == session.header_row:
                if not values or any(isinstance(value, dict) for value in values):
                    raise ImportFileError("Choose a header row containing column names.")
                session.headers = [
                    str(value).strip() if value is not None else "" for value in values
                ]
                continue
            if not any(value not in (None, "") for value in values):
                continue
            if len(values) > len(session.headers):
                if any(value not in (None, "") for value in values[len(session.headers) :]):
                    raise ImportFileError("A row has more values than the header row.")
                values = values[: len(session.headers)]
            values += [None] * (len(session.headers) - len(values))
            batch.append({"session_id": session.id, "number": number, "data": values})
            count += 1
            if len(batch) == BATCH_SIZE:
                await db.execute(sa.insert(ImportRow), batch)
                batch.clear()
        if batch:
            await db.execute(sa.insert(ImportRow), batch)
    if not session.headers or not count:
        raise ImportFileError("The worksheet has no data rows after the header.")
    session.total, session.processed = count, 0
    session.source = None
    session.sample = []
    session.status = "mapping"


def suggest_mapping(headers: list[str], resource: ImportResource) -> dict[str, str]:
    def normalize(value: str) -> str:
        return "".join(c for c in value.casefold() if c.isalnum())

    candidates: dict[str, set[str]] = {}
    for spec in resource.specs:
        for label in (spec.name, spec.label):
            candidates.setdefault(normalize(label), set()).add(spec.name)
    result: dict[str, str] = {}
    for index, header in enumerate(headers):
        matches = candidates.get(normalize(header), set()) if header else set()
        if len(matches) == 1:
            target = next(iter(matches))
            if target not in result.values():
                result[str(index)] = target
    return result


async def set_mapping(
    db: AsyncSession, session: ImportSession, resource: ImportResource, mapping: dict[str, str]
) -> None:
    _check_session(session, resource)
    if session.status not in {"mapping", "review"}:
        raise ValueError("Mapping cannot be changed in this state.")
    if not mapping or len(set(mapping.values())) != len(mapping):
        raise ValueError("Map at least one column and map each field only once.")
    if any(
        index not in {str(i) for i in range(len(session.headers))} or field not in resource.columns
        for index, field in mapping.items()
    ):
        raise ValueError("The mapping contains an unavailable column or field.")
    required = {
        spec.name
        for spec in resource.specs
        if spec.required and spec.initial is None and not spec.primary_key
    }
    if session.mode != "update-only" and not required.issubset(mapping.values()):
        raise ValueError("Map all required fields before continuing.")
    if session.mode != "insert" and not set(resource.keys).issubset(mapping.values()):
        raise ValueError("Map all configured key fields before continuing.")
    if set(resource.keys).intersection(mapping.values()) and not set(resource.keys).issubset(
        mapping.values()
    ):
        raise ValueError("Map all key fields or leave them all unmapped for insert mode.")
    session.mapping = mapping
    session.resource_signature = resource.signature
    session.processed = 0
    session.revision += 1
    session.status = "validating"
    await db.execute(
        sa.update(ImportRow)
        .where(ImportRow.session_id == session.id)
        .values(action="pending", snapshot=None, errors={}, key=None, committed=False)
    )


def _payload(session: ImportSession, row: ImportRow) -> tuple[dict[str, Any], dict[str, list[str]]]:
    data: dict[str, Any] = {}
    errors: dict[str, list[str]] = {}
    for index, field in session.mapping.items():
        value = row.data[int(index)]
        if isinstance(value, dict):
            errors[field] = [value["error"]]
        else:
            data[field] = value
    return data, errors


async def _plan(
    db: AsyncSession,
    resource: ImportResource,
    session: ImportSession,
    row: ImportRow,
    *,
    lock: bool = False,
) -> tuple[str, Any, BoundForm | None, dict[str, list[str]]]:
    data, errors = _payload(session, row)
    target = None
    generate = (
        resource.generated_key is not None
        and data.get(resource.generated_key) in (None, "")
        and session.mode != "update-only"
    )
    if generate and resource.generated_key is not None:
        data.pop(resource.generated_key, None)
    if resource.keys and set(resource.keys).issubset(session.mapping.values()) and not generate:
        key_form = BoundForm.bind(
            resource.key_specs,
            resource.key_schema,
            {key: data.get(key) for key in resource.keys},
            base=RecordContext(user=resource.user),
        )
        if not key_form.is_valid:
            return "error", None, None, errors | key_form.errors
        key_values = key_form.cleaned
        key_errors = resource.value_errors(key_values)
        if key_errors:
            return "error", None, None, errors | key_errors
        row.key = digest(key_values)
        where = [getattr(resource.model, key) == key_values[key] for key in resource.keys]
        statement = resource.queryset().where(*where).order_by(None)
        if lock:
            statement = statement.with_for_update()
        target = (await db.scalars(statement)).unique().one_or_none()
        if target is None:
            # Never treat an existing, invisible record as a new record to overwrite.
            exists = await db.scalar(
                sa.select(sa.literal(True)).select_from(resource.model).where(*where).limit(1)
            )
            if exists:
                return "error", None, None, {"__all__": ["The key is unavailable."]}
            if resource.generated_key is not None:
                return (
                    "error",
                    None,
                    None,
                    {
                        "__all__": [
                            "Generated IDs only match existing records. "
                            "Leave the ID blank to create a record."
                        ]
                    },
                )
    if target is not None and session.mode == "insert":
        return "error", target, None, {"__all__": ["A record with this key already exists."]}
    if target is not None and session.mode == "skip-existing":
        return "skip", target, None, errors
    if target is None and session.mode == "update-only":
        return "error", None, None, {"__all__": ["No matching record exists."]}
    if target is not None and not resource.can_change(target):
        return "error", None, None, {"__all__": ["Updating this record is not permitted."]}
    bound = resource.bind(data, target)
    errors |= bound.errors
    # Only mapped fields and explicit initial values are assigned. DB defaults
    # own missing create columns; missing update columns keep their values.
    assigned = set(data)
    if target is None:
        assigned.update(spec.name for spec in resource.specs if spec.initial is not None)
    bound.cleaned = {key: value for key, value in bound.cleaned.items() if key in assigned}
    if errors:
        return "error", target, bound, errors
    for spec in resource.specs:
        value = bound.cleaned.get(spec.name)
        if value is None or spec.column is None:
            continue
        for foreign_key in getattr(spec.column, "foreign_keys", ()):
            related = foreign_key.column
            if not await db.scalar(
                sa.select(sa.literal(True))
                .select_from(related.table)
                .where(related == value)
                .limit(1)
            ):
                errors[spec.name] = ["The referenced record does not exist."]
    return (
        ("error" if errors else "update" if target is not None else "insert"),
        target,
        bound,
        errors,
    )


async def validate_batch(
    db: AsyncSession, session: ImportSession, resource: ImportResource
) -> bool:
    await db.refresh(session, with_for_update=True)
    _check_session(session, resource)
    if session.status != "validating":
        raise ValueError("This import is not awaiting validation.")
    if resource.signature != session.resource_signature:
        raise ValueError("The model configuration changed. Review the mapping again.")
    rows = list(
        await db.scalars(
            sa.select(ImportRow)
            .where(ImportRow.session_id == session.id, ImportRow.action == "pending")
            .order_by(ImportRow.id)
            .limit(BATCH_SIZE)
        )
    )
    for row in rows:
        action, target, _bound, errors = await _plan(db, resource, session, row)
        row.action, row.errors = action, errors
        row.snapshot = resource.snapshot(target) if target is not None else None
    session.processed += len(rows)
    if not rows:
        duplicates = (
            sa.select(ImportRow.key)
            .where(ImportRow.session_id == session.id, ImportRow.key.is_not(None))
            .group_by(ImportRow.key)
            .having(sa.func.count() > 1)
        )
        await db.execute(
            sa.update(ImportRow)
            .where(ImportRow.session_id == session.id, ImportRow.key.in_(duplicates))
            .values(action="error", errors={"__all__": ["Duplicate key within this file."]})
        )
        session.status = "review"
    return bool(rows)


async def commit_batch(db: AsyncSession, session: ImportSession, resource: ImportResource) -> bool:
    await db.refresh(session, with_for_update=True)
    _check_session(session, resource)
    if session.status not in {"queued", "committing"}:
        raise ValueError("Confirm the preview before applying an import.")
    if session.status == "queued":
        session.status, session.processed = "committing", 0
    if resource.signature != session.resource_signature:
        raise ValueError("The model configuration changed. Import stopped before the next batch.")
    rows = list(
        await db.scalars(
            sa.select(ImportRow)
            .where(
                ImportRow.session_id == session.id,
                ImportRow.action.in_(("insert", "update", "skip")),
                ImportRow.committed.is_(False),
            )
            .order_by(ImportRow.id)
            .limit(BATCH_SIZE)
        )
    )
    for row in rows:
        planned, snapshot = row.action, row.snapshot
        action, target, bound, errors = await _plan(db, resource, session, row, lock=True)
        current = resource.snapshot(target) if target is not None else None
        if action != planned or current != snapshot:
            row.action, row.errors = (
                "error",
                {"__all__": ["The record changed after preview. Import this row again."]},
            )
        elif action == "skip":
            row.committed = True
        elif errors or bound is None:
            row.action, row.errors = "error", errors
        else:
            try:
                async with db.begin_nested():
                    obj = target if target is not None else resource.model()
                    bound.apply_to(obj)
                    db.add(obj)
                    await db.flush()
            except (DBAPIError, OverflowError, StaleDataError) as exc:
                if isinstance(exc, DBAPIError) and not isinstance(exc, (DataError, IntegrityError)):
                    code = str(getattr(exc.orig, "sqlstate", "") or getattr(exc.orig, "pgcode", ""))
                    if not code.startswith(("22", "23")):
                        raise
                row.action, row.errors = (
                    "error",
                    {"__all__": ["A database constraint or concurrent change prevented this row."]},
                )
            else:
                row.committed = True
        session.processed += 1
    if not rows:
        session.status = "completed"
    return bool(rows)


async def summary(db: AsyncSession, session: ImportSession) -> dict[str, int]:
    results = await db.execute(
        sa.select(ImportRow.action, ImportRow.committed, sa.func.count())
        .where(ImportRow.session_id == session.id)
        .group_by(ImportRow.action, ImportRow.committed)
    )
    counts: Counter[str] = Counter()
    for action, committed, count in results:
        counts[action] += count
        if committed:
            counts[f"{action}_done"] += count
    return dict(counts)
