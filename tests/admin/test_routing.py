from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from obox_builder.conf import settings
from obox_builder.contrib.admin.routing import AdminPageRoute
from obox_builder.contrib.admin.site import AdminSite
from obox_builder.core.application import create_app
from obox_builder.exceptions import PermissionDenied


async def test_permission_errors_keep_admin_branding_scoped_to_admin_routes(
    admin_env: None,
) -> None:
    settings.load().ROOT_URLCONF = ""
    app = create_app()
    site = AdminSite(site_header="Operations", site_title="Control panel")

    def bind_site(request: Request) -> None:
        request.state.admin_site = site

    router = APIRouter(route_class=AdminPageRoute, dependencies=[Depends(bind_site)])

    @router.get("/denied")
    @app.get("/plain")
    async def denied() -> None:
        raise PermissionDenied("Access was refused.")

    app.include_router(router, prefix="/operations")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        html = await client.get("/operations/denied", headers={"Accept": "text/html"})
        plain = await client.get("/plain", headers={"Accept": "text/html"})
        for accept in ("application/json", "text/html, application/json", "*/*"):
            response = await client.get("/operations/denied", headers={"Accept": accept})
            assert response.status_code == 403
            assert response.json() == {"detail": "Access was refused."}

    assert html.status_code == plain.status_code == 403
    assert "Control panel" in html.text
    assert "Access was refused." in html.text
    assert "admin/vendor/tabler" in html.text
    assert "Control panel" not in plain.text
    assert "admin/" not in plain.text


async def test_custom_application_permission_handler_takes_precedence(admin_env: None) -> None:
    app = create_app()
    router = APIRouter(route_class=AdminPageRoute)

    @router.get("/denied")
    async def denied() -> None:
        raise PermissionDenied("Access was refused.")

    async def custom(request: Request, exc: Exception) -> Response:
        return JSONResponse({"custom": str(exc)}, status_code=403)

    app.add_exception_handler(PermissionDenied, custom)
    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/denied", headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert response.json() == {"custom": "Access was refused."}


async def test_missing_admin_error_template_uses_generic_fallback(
    admin_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jinja2 import TemplateNotFound
    from obox_builder.contrib.admin import routing

    def missing(*args: object, **kwargs: object) -> Response:
        raise TemplateNotFound("admin/errors/403.html")

    monkeypatch.setattr(routing, "render", missing)
    app = create_app()
    router = APIRouter(route_class=AdminPageRoute)

    @router.get("/denied")
    async def denied() -> None:
        raise PermissionDenied("Access was refused.")

    app.include_router(router)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/denied", headers={"Accept": "text/html"})
    assert response.status_code == 403
    assert "403 Forbidden" in response.text
