from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, cast

import httpx
import pytest
from obox_builder.conf import SETTINGS_MODULE_ENV


@pytest.fixture
def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    import obox_builder

    monkeypatch.setenv(SETTINGS_MODULE_ENV, "tests.support.history_settings")
    obox_builder.setup()


@pytest.fixture
async def api(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    from obox_builder.contrib.admin import AdminSite, ModelAdmin
    from obox_builder.core.application import create_app
    from tests.support.apps.shop.admin import CategoryAdmin, OrderAdmin, ProductAdmin
    from tests.support.apps.shop.models import Category, Order, Product
    from tests.support.apps.warehouse.models import Item, Shelf, Tag

    site = AdminSite("api-test")
    for model, admin_class in (
        (Product, ProductAdmin),
        (Category, CategoryAdmin),
        (Order, OrderAdmin),
        (Item, ModelAdmin),
        (Shelf, ModelAdmin),
        (Tag, ModelAdmin),
    ):
        site.register(
            model,
            cast(
                type[ModelAdmin],
                type(
                    f"Api{model.__name__}",
                    (admin_class,),
                    {
                        "api_enabled": True,
                        "search_fields": ("name",) if model is Item else admin_class.search_fields,
                    },
                ),
            ),
        )
    app = create_app()
    app.include_router(site.get_router(), prefix="/admin")
    app.include_router(site.get_api_router(), prefix="/api/admin/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        http.api_site = site  # type: ignore[attr-defined]
        http.api_app = app  # type: ignore[attr-defined]
        yield http


async def sign_in(
    api: httpx.AsyncClient, *, superuser: bool = True, username: str = "api-user"
) -> dict[str, str]:
    from tests.admin.conftest import make_user

    await make_user(username, is_superuser=superuser)
    response = await api.post(
        "/api/admin/v1/auth/login", json={"username": username, "password": "s3cure-password"}
    )
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def site_for(api: httpx.AsyncClient) -> Any:
    return api.api_site  # type: ignore[attr-defined]
