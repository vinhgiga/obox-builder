import asyncio
from typing import Any

import httpx
import sqlalchemy as sa
from obox_builder import Obox, Principal
from obox_builder.auth import StaffOnly
from obox_builder.conf import Settings
from obox_builder.contrib.auth import Auth
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "i18n_widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


STAFF = Principal(id=1, is_authenticated=True, is_staff=True)


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


async def make_app(*, principal: Principal | None = STAFF, auth: bool = False) -> tuple[Obox, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = Obox(
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: principal,
            default_policy=StaffOnly(),
            csrf_secret="x" * 32,
        ),
    )
    if auth:
        Auth(backend=AnonymousBackend(), secure=False).install(app)
    app.register(Widget)
    return app, engine


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
            transport=httpx.ASGITransport(app=app),
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
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            return await client.post(path, data=data)
    finally:
        await engine.dispose()


def test_language_prefix_translates_admin_pages() -> None:
    vietnamese = asyncio.run(get("/vi/admin/widgets"))
    english = asyncio.run(get("/en/admin/widgets"))
    assert vietnamese.status_code == english.status_code == 200
    assert 'lang="vi"' in vietnamese.text
    assert "Quản trị" in vietnamese.text
    assert 'href="/en/admin/widgets"' in vietnamese.text
    assert 'href="/vi/admin/widgets"' in vietnamese.text
    assert 'lang="en"' in english.text
    assert "Administration" in english.text


def test_accept_language_selects_unprefixed_language() -> None:
    response = asyncio.run(get("/admin/widgets", headers={"accept-language": "vi-VN,vi;q=0.9"}))
    assert 'lang="vi"' in response.text
    assert "Quản trị" in response.text
    assert 'href="/vi/admin/widgets"' in response.text


def test_language_switch_preserves_path_and_query() -> None:
    response = asyncio.run(get("/vi/admin/widgets?ordering=name"))
    assert 'href="/en/admin/widgets?ordering=name"' in response.text
    assert 'href="/vi/admin/widgets?ordering=name"' in response.text


def test_auth_redirect_keeps_language_prefix() -> None:
    response = asyncio.run(get("/vi/admin/widgets", principal=None, auth=True))
    assert response.status_code == 200
    assert response.url.path == "/vi/auth/login"
    assert 'lang="vi"' in response.text
    assert "Đăng nhập" in response.text
    assert '<button type="submit">Đăng nhập</button>' in response.text


def test_plural_form_uses_catalog() -> None:
    vietnamese = asyncio.run(get("/vi/admin/widgets", records=2))
    english = asyncio.run(get("/en/admin/widgets", records=2))
    assert "2 bản ghi trên trang này" in vietnamese.text
    assert "2 records on this page" in english.text


def test_runtime_validation_errors_are_translated() -> None:
    response = asyncio.run(post("/vi/admin/widgets/new", {"csrf_token": ""}))
    assert response.status_code == 422
    assert "Trường này là bắt buộc." in response.text
