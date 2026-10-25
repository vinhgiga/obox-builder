"""Full-page and fragment responses for the admin UI."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from starlette.responses import HTMLResponse

from obox_builder.conf import settings
from obox_builder.templating import render as render_page


def render(
    request: Request,
    name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
) -> HTMLResponse:
    from obox_builder.contrib.admin.views.common import current_site

    headers = request.headers
    admin_site = current_site(request)
    fragment = (
        settings.ADMIN_SMOOTH_NAVIGATION
        and headers.get("hx-request") == "true"
        and headers.get("hx-target") == "main#admin-main"
        and headers.get("hx-request-type") != "full"
        and headers.get("hx-history-restore-request") != "true"
    )
    response = render_page(
        request,
        name,
        {
            **(context or {}),
            "site_name": admin_site.site_header,
            "site_header": admin_site.site_header,
            "site_title": admin_site.site_title,
            "index_title": admin_site.index_title,
            "admin_fragment": fragment,
            "smooth_navigation": settings.ADMIN_SMOOTH_NAVIGATION,
        },
        status_code=status_code,
    )
    response.headers["Cache-Control"] = "private, no-cache"
    response.headers["Vary"] = (
        "HX-Request, HX-Target, HX-Request-Type, HX-History-Restore-Request, HX-Preloaded"
    )
    if fragment and request.method != "GET":
        response.headers["HX-Push-Url"] = "false"
    if fragment and name == "admin/auth/login.html":
        response.headers["HX-Redirect"] = str(request.url)
    return response
