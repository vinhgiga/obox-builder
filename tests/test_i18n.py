import asyncio
import re
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from obox._auth import RBAC, Principal
from obox._config import RuntimeConfig
from obox._runtime import _Runtime
from obox.contrib.auth import Auth
from obox.contrib.resources import Resource as ResourceDefinition
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool

from tests.helpers import with_builders


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "i18n_widgets"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


class WidgetBuilder(ResourceDefinition[Widget]):
    model = Widget
    fields = ("id", "name")


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)


class AnonymousBackend:
    async def resolve(self, token: str, *, request: Any = None) -> Principal:
        return Principal()

    async def login(
        self,
        username: str,
        password: str,
        *,
        previous: str = "",
        max_age: int = 86400,
        request: Any = None,
    ) -> None:
        return None

    async def logout(self, token: str, *, request: Any = None) -> None:
        return None


async def make_app(
    *, principal: Principal | None = STAFF, auth: bool = False
) -> tuple[_Runtime, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = _Runtime(
        FastAPI(),
        settings=with_builders(
            RuntimeConfig(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                principal_dependency=lambda: principal,
                default_policy=RBAC(),
                csrf_secret="x" * 32,
            ),
            WidgetBuilder,
        ),
    )
    if auth:
        app._activate(Auth(backend=AnonymousBackend(), secure=False))
    return (app, engine)


async def get(
    path: str,
    *,
    principal: Principal | None = STAFF,
    auth: bool = False,
    headers: dict[str, str] | None = None,
    records: int = 0,
) -> httpx.Response:
    app, engine = await make_app(principal=principal, auth=auth)
    try:
        if records:
            async with async_sessionmaker(engine, expire_on_commit=False)() as session:
                session.add_all(Widget(name=f"Widget {index}") for index in range(records))
                await session.commit()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            return await client.get(path, headers=headers)
    finally:
        await engine.dispose()


async def post(
    path: str, data: dict[str, str], *, principal: Principal | None = STAFF
) -> httpx.Response:
    app, engine = await make_app(principal=principal)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            return await client.post(path, data=data)
    finally:
        await engine.dispose()


def test_language_prefix_translates_admin_pages() -> None:
    vietnamese = asyncio.run(get("/vi/admin/admin/widgets"))
    english = asyncio.run(get("/en/admin/admin/widgets"))
    assert vietnamese.status_code == english.status_code == 200
    assert 'lang="vi"' in vietnamese.text
    assert "Dạng lưới" in vietnamese.text
    assert "Thêm bản ghi" in vietnamese.text
    assert 'href="/en/admin/admin/widgets"' in vietnamese.text
    assert 'href="/vi/admin/admin/widgets"' in vietnamese.text
    assert 'lang="en"' in english.text
    assert "Grid view" in english.text


def test_accept_language_selects_unprefixed_language() -> None:
    response = asyncio.run(
        get("/admin/admin/widgets", headers={"accept-language": "vi-VN,vi;q=0.9"})
    )
    assert 'lang="vi"' in response.text
    assert "Dạng lưới" in response.text
    assert 'href="/vi/admin/admin/widgets"' in response.text


def test_language_switch_preserves_path_and_query() -> None:
    response = asyncio.run(get("/vi/admin/admin/widgets?ordering=name"))
    assert 'href="/en/admin/admin/widgets?ordering=name"' in response.text
    assert 'href="/vi/admin/admin/widgets?ordering=name"' in response.text


def test_auth_redirect_keeps_language_prefix() -> None:
    response = asyncio.run(get("/vi/admin/admin/widgets", principal=None, auth=True))
    assert response.status_code == 200
    assert response.url.path == "/vi/auth/login"
    assert 'lang="vi"' in response.text
    assert "Đăng nhập" in response.text
    assert re.search(
        '<button class="btn btn-primary"\\s+type="submit">Đăng nhập</button>', response.text
    )


def test_plural_form_uses_catalog() -> None:
    vietnamese = asyncio.run(get("/vi/admin/admin/widgets", records=2))
    english = asyncio.run(get("/en/admin/admin/widgets", records=2))
    assert "2 bản ghi trên trang này" in vietnamese.text
    assert "2 records on this page" in english.text


def test_runtime_validation_errors_are_translated() -> None:
    response = asyncio.run(post("/vi/admin/admin/widgets/new", {"csrf_token": ""}))
    assert response.status_code == 422
    assert "Trường này là bắt buộc." in response.text
