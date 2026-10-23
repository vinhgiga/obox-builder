"""Search configured fields across the models visible to the current user."""

from __future__ import annotations

import unicodedata
from typing import Any
from urllib.parse import urlencode

from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from obox_builder.contrib.admin.context import AdminContext
from obox_builder.contrib.admin.rendering import render
from obox_builder.contrib.admin.views.common import current_site, page_context
from obox_builder.contrib.auth.dependencies import login_required
from obox_builder.db.search import fold_text
from obox_builder.db.session import get_db
from obox_builder.fields import QuerySpec, QuerySpecError

PER_MODEL = 20


def _highlight(text: str, term: str) -> list[tuple[str, bool]]:
    # Preserve the original spelling and let the template escape every segment.
    folded = ""
    positions: list[int] = []
    for index, char in enumerate(text):
        part = fold_text(char) or ""
        folded += part
        positions.extend([index] * len(part))
    needle = fold_text(term) or ""
    start = folded.find(needle) if needle else -1
    if start < 0:
        return [(text[:160], False)]
    left, right = positions[start], positions[start + len(needle) - 1] + 1
    while right < len(text) and unicodedata.combining(text[right]):
        right += 1
    return [
        (text[max(0, left - 60) : left], False),
        (text[left:right], True),
        (text[right : right + 100], False),
    ]


async def global_search(
    request: Request,
    user: Any = Depends(login_required),
    db: AsyncSession = Depends(get_db),
) -> Response:
    site = current_site(request)
    try:
        query = QuerySpec.parse(request.query_params, {})
    except QuerySpecError as exc:
        raise HTTPException(400, str(exc)) from exc
    matches = []
    if query.q:
        for admin in sorted(site.get_registry().values(), key=lambda item: item.model_label):
            ctx = AdminContext(request=request, user=user, db=db, site=site, admin=admin)
            search_specs = [
                spec
                for spec in admin.get_search_specs()
                if not spec.secret and spec.type.searchable and spec.column is not None
            ]
            if (
                not user.has_perm(admin.permission_name("view"))
                or not admin.has_view_permission(ctx)
                or not search_specs
            ):
                continue
            specs = admin.spec_map()
            stmt = QuerySpec(q=query.q).apply(
                admin.get_queryset(ctx).order_by(None), specs, search_specs=search_specs
            )
            objects = (await db.scalars(stmt.limit(PER_MODEL + 1))).unique().all()
            results = []
            for obj in objects[:PER_MODEL]:
                if not admin.has_view_permission(ctx, obj):
                    continue
                pk = admin.get_pk(obj)
                texts = [
                    spec.type.to_input(getattr(obj, spec.column.key or spec.name), spec)
                    for spec in search_specs
                    if spec.column is not None
                ]
                label = texts[0] or f"#{pk}"
                excerpt = next(
                    (
                        text
                        for text in texts
                        if (fold_text(query.q) or "") in (fold_text(text) or "")
                    ),
                    label,
                )
                row_filter = urlencode({"f": f"{admin.pk_column().key}:eq:{pk}"})
                url = (
                    admin.change_path(pk)
                    if user.has_perm(admin.permission_name("change"))
                    and admin.has_change_permission(ctx, obj)
                    else f"{admin.list_path()}?{row_filter}"
                )
                results.append(
                    {"label": label[:160], "segments": _highlight(excerpt, query.q), "url": url}
                )
            if results:
                matches.append(
                    {
                        "name": admin.plural_name,
                        "results": results,
                        "url": f"{admin.list_path()}?{urlencode({'q': query.q})}",
                    }
                )
    context = await page_context(request, q=query.q, matches=matches)
    return render(request, "admin/search.html", context)
