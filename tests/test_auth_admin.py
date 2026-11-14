import asyncio
import re
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import ModelBuilder, Obox, Principal
from obox_builder.auth import RBAC
from obox_builder.conf import Settings
from obox_builder.contrib.auth import Auth
from obox_builder.contrib.auth.models import Base as AuthBase
from obox_builder.contrib.auth.models import Role, Session, User, user_roles
from obox_builder.contrib.auth.passwords import hash_password, verify_password
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, selectinload
from sqlalchemy.pool import StaticPool


class Base(DeclarativeBase):
    pass


class Widget(Base):
    __tablename__ = "auth_admin_widgets"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))


class WidgetBuilder(ModelBuilder[Widget]):
    fields = ("id", "name")


class Article(Base):
    __tablename__ = "auth_admin_articles"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(sa.String(50))


class ArticleBuilder(ModelBuilder[Article]):
    fields = ("id", "title")
    policy = RBAC("blog.articles")
    permissions = ("approve", "export")


class StubBackend:
    username_field = "username"

    def __init__(self, principal: Principal) -> None:
        self.principal = principal
        self.user_model = User
        self.session_model = Session

    async def resolve(self, token: str, *, request: Any = None) -> Principal:
        return self.principal

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


SUPERUSER = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)
STAFF = Principal(
    id=2,
    is_authenticated=True,
    is_staff=True,
    permissions=frozenset(
        f"admin.widgets.{action}" for action in ("list", "retrieve", "create", "update", "delete")
    ),
)
PASSWORD = "correct horse battery staple"  # noqa: S105


async def make_app(principal: Principal = SUPERUSER, *, admin: bool = True) -> tuple[Obox, Any]:
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool)
    async with engine.begin() as connection:
        await connection.run_sync(AuthBase.metadata.create_all)
        await connection.run_sync(Base.metadata.create_all)
    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: principal,
            default_policy=RBAC(),
            csrf_secret="x" * 32,
            auth_password_validators=(
                {
                    "NAME": "obox_builder.contrib.auth.password_validation.MinimumLengthValidator",
                    "OPTIONS": {"min_length": 12},
                },
            ),
        ),
    )
    app.install(Auth(backend=StubBackend(principal), secure=False, admin=admin))
    app.register(WidgetBuilder)
    app.register(ArticleBuilder)
    return app, engine


async def seed_role(app: Obox, name: str, permissions: Sequence[str] = ()) -> int:
    async with app.uow(principal=SUPERUSER) as ctx:
        role = Role(name=name, permissions=list(permissions))
        ctx.session.add(role)
        await ctx.session.flush()
        return role.id


async def seed_user(
    app: Obox,
    username: str,
    *,
    password: str = PASSWORD,
    is_superuser: bool = False,
    is_active: bool = True,
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    department: str = "",
    role_ids: Sequence[int] = (),
) -> int:
    async with app.uow(principal=SUPERUSER) as ctx:
        roles = (
            list((await ctx.session.scalars(sa.select(Role).where(Role.id.in_(role_ids)))).unique())
            if role_ids
            else []
        )
        user = User(
            username=username,
            password_hash=await hash_password(password),
            is_active=is_active,
            is_superuser=is_superuser,
            email=email,
            first_name=first_name,
            last_name=last_name,
            department=department,
            roles=roles,
        )
        ctx.session.add(user)
        await ctx.session.flush()
        return user.id


async def fetch_user(app: Obox, id: int) -> Any:
    async with app.uow(principal=SUPERUSER) as ctx:
        statement = sa.select(User).where(User.id == id).options(selectinload(User.roles))
        return (await ctx.session.scalars(statement)).unique().one_or_none()


async def fetch_role(app: Obox, id: int) -> Any:
    async with app.uow(principal=SUPERUSER) as ctx:
        return await ctx.session.get(Role, id)


async def get(app: Obox, path: str, **kwargs: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        return await client.get(path, **kwargs)


async def post(app: Obox, path: str, data: Any) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app.app), base_url="http://test"
    ) as client:
        return await client.post(path, data=data)


def test_admin_routes_static_and_superuser_access() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client:
                users = await client.get("/auth/users")
                assert users.status_code == 200
                assert "New user" in users.text
                assert "auth-admin.js" in users.text
                roles = await client.get("/auth/roles")
                assert roles.status_code == 200
                assert "New role" in roles.text
                assert "auth-admin.js" in roles.text
                asset = await client.get("/admin/_static/auth-admin.js")
                assert asset.status_code == 200
                assert "data-tree-node" in asset.text
                assert "data-role-select" in asset.text
                assert "data-role-option" in asset.text
                assert "data-permission-preview" in asset.text
                assert "previewRoles" in asset.text
        finally:
            await engine.dispose()

        anonymous, engine = await make_app(Principal())
        try:
            response = await get(anonymous, "/auth/users")
            assert response.status_code == 303
            assert response.headers["location"] == "/auth/login?next=%2Fauth%2Fusers"
        finally:
            await engine.dispose()

        staff, engine = await make_app(STAFF)
        try:
            response = await get(staff, "/auth/users")
            assert response.status_code == 403
        finally:
            await engine.dispose()

        disabled, engine = await make_app(admin=False)
        try:
            response = await get(disabled, "/auth/users")
            assert response.status_code == 404
            builder = await get(disabled, "/admin/widgets")
            assert "/auth/users" not in builder.text
            assert "/auth/roles" not in builder.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_user_list_navigation_and_non_superuser_navigation() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            superuser_id = await seed_user(
                app,
                "ada",
                email="ada@example.com",
                first_name="Ada",
                last_name="Lovelace",
                department="Engineering",
                is_superuser=True,
            )
            await seed_user(
                app,
                "bob",
                email="bob@example.com",
                first_name="Bob",
                last_name="Stone",
                department="Operations",
                is_active=False,
            )
            page = await get(app, "/auth/users")
            assert page.status_code == 200
            headers = [
                match.strip()
                for match in re.findall(r'<th scope="col">(.*?)</th>', page.text, re.DOTALL)
            ]
            assert headers == [
                "Actions",
                "ID",
                "Username",
                "Email",
                "First name",
                "Last name",
                "Department",
                "Active",
            ]
            assert f"<td>{superuser_id}</td>" in page.text
            assert page.text.index('data-confirm="Delete this user?"') < page.text.index(
                f"<td>{superuser_id}</td>"
            )
            assert "ada@example.com" in page.text
            assert "bob@example.com" in page.text
            assert "Ada" in page.text
            assert "Lovelace" in page.text
            assert "Engineering" in page.text
            assert "Bob" in page.text
            assert "Stone" in page.text
            assert "Operations" in page.text
            assert 'class="badge bg-purple-lt text-purple"' in page.text
            assert "inactive" in page.text
            assert "Edit" in page.text
            assert "Delete" in page.text

            builder = await get(app, "/admin/widgets")
            assert builder.status_code == 200
            assert 'href="/auth/users"' in builder.text
            assert 'href="/auth/roles"' in builder.text
        finally:
            await engine.dispose()

        staff, engine = await make_app(STAFF)
        try:
            page = await get(staff, "/admin/widgets")
            assert page.status_code == 200
            assert "/auth/users" not in page.text
            assert "/auth/roles" not in page.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_create_user_with_roles_and_defaults() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            role_id = await seed_role(app, "Editors", ["admin.widgets.list"])
            form = await get(app, "/auth/users/new")
            assert form.status_code == 200
            assert 'name="username"' in form.text
            assert re.search(r'id="username".*?autocomplete="off"', form.text, re.S)
            assert 'name="email"' in form.text
            assert 'name="password"' in form.text
            assert 'name="confirm_password"' in form.text
            assert form.text.index('name="password"') < form.text.index('name="confirm_password"')
            assert "field-grid" not in form.text
            assert len(re.findall(r'<span class="text-danger"[^>]*>\*</span>', form.text)) == 3
            assert 'name="first_name"' in form.text
            assert 'name="last_name"' in form.text
            assert 'name="department"' in form.text
            assert re.search(r'name="is_active"\s+value="1"\s+checked', form.text)
            assert "data-role-select" in form.text
            assert "auth-admin.js" in form.text
            assert "Choose roles" in form.text
            assert 'class="role-select-chip"' not in form.text
            assert re.search(r"data-role-select-placeholder(?!\s+hidden)", form.text)
            option = re.search(
                rf'<input[^>]+name="role_ids"[^>]+value="{role_id}"[^>]*>', form.text
            )
            assert option is not None
            assert "checked" not in option.group(0)

            response = await post(
                app,
                "/auth/users/new",
                {
                    "csrf_token": "",
                    "username": "alice",
                    "email": "alice@example.com",
                    "password": PASSWORD,
                    "confirm_password": PASSWORD,
                    "first_name": "Alice",
                    "last_name": "Smith",
                    "department": "Research",
                    "is_active": "1",
                    "role_ids": str(role_id),
                },
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/auth/users?saved=1"

            async with app.uow(principal=SUPERUSER) as ctx:
                user = await ctx.session.scalar(
                    sa.select(User)
                    .where(User.username == "alice")
                    .options(selectinload(User.roles))
                )
                assert user is not None
                assert user.email == "alice@example.com"
                assert user.first_name == "Alice"
                assert user.last_name == "Smith"
                assert user.department == "Research"
                assert user.is_active is True
                assert user.is_superuser is False
                assert [role.name for role in user.roles] == ["Editors"]
                assert await verify_password(PASSWORD, user.password_hash)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_user_form_permission_preview_is_collapsed_and_tracks_roles() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            role_id = await seed_role(
                app, "Readers", ["admin.widgets.list", "blog.articles.retrieve"]
            )
            new_form = await get(app, "/auth/users/new")
            details = re.search(r"<details[^>]*data-permission-preview[^>]*>", new_form.text)
            assert details is not None
            assert "open" not in details.group(0)
            assert "Preview permissions" in new_form.text
            assert re.search(r"data-preview-empty(?!\s+hidden)", new_form.text)
            permission = re.search(
                r'<li[^>]*data-preview-permission="admin\.widgets\.list"[^>]*>',
                new_form.text,
            )
            assert permission is not None
            assert "hidden" in permission.group(0)
            assert f'data-preview-roles="{role_id}"' in permission.group(0)
            service = re.search(r'<section[^>]*data-preview-service="admin"[^>]*>', new_form.text)
            assert service is not None
            assert "hidden" in service.group(0)

            user_id = await seed_user(app, "reader", role_ids=[role_id])
            edit_form = await get(app, f"/auth/users/{user_id}/edit")
            details = re.search(r"<details[^>]*data-permission-preview[^>]*>", edit_form.text)
            assert details is not None
            assert "open" not in details.group(0)
            granted = re.search(
                r'<li[^>]*data-preview-permission="admin\.widgets\.list"[^>]*>',
                edit_form.text,
            )
            denied = re.search(
                r'<li[^>]*data-preview-permission="admin\.widgets\.update"[^>]*>',
                edit_form.text,
            )
            assert granted is not None
            assert "hidden" not in granted.group(0)
            assert f'data-preview-roles="{role_id}"' in granted.group(0)
            assert denied is not None
            assert "hidden" in denied.group(0)
            assert re.search(r"data-preview-empty\s+hidden", edit_form.text)
            for name in ("admin", "blog"):
                service = re.search(
                    rf'<section[^>]*data-preview-service="{name}"[^>]*>',
                    edit_form.text,
                )
                assert service is not None
                assert "hidden" not in service.group(0)
            assert 'data-preview-service="auth"' not in edit_form.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_user_validation_errors() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            await seed_user(app, "existing")
            cases = [
                (
                    {
                        "username": "new1",
                        "password": "short",
                        "confirm_password": "short",
                    },
                    "This password is too short. It must contain at least 12 characters.",
                ),
                (
                    {
                        "username": "new2",
                        "password": PASSWORD,
                        "confirm_password": "different password",
                    },
                    "Passwords do not match.",
                ),
                (
                    {
                        "username": "existing",
                        "password": PASSWORD,
                        "confirm_password": PASSWORD,
                    },
                    "This username is already taken.",
                ),
                (
                    {
                        "username": "new3",
                        "password": PASSWORD,
                        "confirm_password": PASSWORD,
                        "role_ids": "999",
                    },
                    "Choose valid roles.",
                ),
                (
                    {
                        "username": "new4",
                        "email": f"{'x' * 243}@example.com",
                        "password": PASSWORD,
                        "confirm_password": PASSWORD,
                    },
                    "Use at most 254 characters.",
                ),
            ]
            for data, message in cases:
                response = await post(app, "/auth/users/new", data)
                assert response.status_code == 422, (data, message)
                assert message in response.text

            async with app.uow(principal=SUPERUSER) as ctx:
                count = await ctx.session.scalar(sa.select(sa.func.count()).select_from(User))
                assert count == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_edit_user_preserves_password_and_updates_roles() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            first_role = await seed_role(app, "Readers")
            second_role = await seed_role(app, "Writers")
            user_id = await seed_user(
                app,
                "alice",
                email="alice@example.com",
                first_name="Alice",
                last_name="Smith",
                department="Research",
                role_ids=[first_role],
            )
            original = await fetch_user(app, user_id)

            form = await get(app, f"/auth/users/{user_id}/edit")
            assert form.status_code == 200
            assert 'value="alice"' in form.text
            assert 'value="alice@example.com"' in form.text
            assert 'name="first_name"' in form.text
            assert 'value="Alice"' in form.text
            assert 'name="last_name"' in form.text
            assert 'value="Smith"' in form.text
            assert 'value="Research"' in form.text
            first_option = re.search(
                rf'<input[^>]+name="role_ids"[^>]+value="{first_role}"[^>]*>', form.text
            )
            second_option = re.search(
                rf'<input[^>]+name="role_ids"[^>]+value="{second_role}"[^>]*>', form.text
            )
            assert first_option is not None
            assert "checked" in first_option.group(0)
            assert second_option is not None
            assert "checked" not in second_option.group(0)
            assert f'data-role-chip="{first_role}"' in form.text
            assert f'data-role-select-remove="{first_role}"' in form.text
            assert f'data-role-chip="{second_role}"' not in form.text
            assert re.search(r"data-role-select-placeholder\s+hidden", form.text)

            response = await post(
                app,
                f"/auth/users/{user_id}/edit",
                {
                    "csrf_token": "",
                    "username": "alice",
                    "email": "alice@platform.example",
                    "password": "",
                    "confirm_password": "",
                    "first_name": "Alice",
                    "last_name": "Updated",
                    "department": "Platform",
                    "is_active": "1",
                    "role_ids": str(second_role),
                },
            )
            assert response.status_code == 303

            updated = await fetch_user(app, user_id)
            assert updated.email == "alice@platform.example"
            assert updated.first_name == "Alice"
            assert updated.last_name == "Updated"
            assert updated.department == "Platform"
            assert updated.password_hash == original.password_hash
            assert [role.name for role in updated.roles] == ["Writers"]
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_self_and_last_superuser_guards() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            user_id = await seed_user(app, "root", is_superuser=True)
            form = await post(
                app,
                f"/auth/users/{user_id}/edit",
                {
                    "csrf_token": "",
                    "username": "root",
                    "password": "",
                    "confirm_password": "",
                    "first_name": "",
                    "last_name": "",
                    "department": "",
                },
            )
            assert form.status_code == 422
            assert "You cannot deactivate your own account." in form.text

            deleted = await post(app, f"/auth/users/{user_id}/delete", {"csrf_token": ""})
            assert deleted.status_code == 303
            assert deleted.headers["location"] == "/auth/users?error=self"
            assert await fetch_user(app, user_id) is not None
        finally:
            await engine.dispose()

        other, engine = await make_app(
            Principal(id=99, is_authenticated=True, is_staff=True, is_superuser=True)
        )
        try:
            user_id = await seed_user(other, "root", is_superuser=True)
            form = await post(
                other,
                f"/auth/users/{user_id}/edit",
                {
                    "csrf_token": "",
                    "username": "root",
                    "password": "",
                    "confirm_password": "",
                    "first_name": "",
                    "last_name": "",
                    "department": "",
                },
            )
            assert form.status_code == 422
            assert "At least one superuser must remain active." in form.text

            deleted = await post(other, f"/auth/users/{user_id}/delete", {"csrf_token": ""})
            assert deleted.status_code == 303
            assert deleted.headers["location"] == "/auth/users?error=last_superuser"
            assert await fetch_user(other, user_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_delete_user_removes_role_links() -> None:
    async def scenario() -> None:
        app, engine = await make_app(
            Principal(id=99, is_authenticated=True, is_staff=True, is_superuser=True)
        )
        try:
            role_id = await seed_role(app, "Editors")
            user_id = await seed_user(app, "alice", role_ids=[role_id])
            async with app.uow(principal=SUPERUSER) as ctx:
                ctx.session.add(
                    Session(
                        token_hash="token" * 10,
                        auth_hash="auth" * 16,
                        expires_at=datetime.now(UTC) + timedelta(hours=1),
                        user_id=user_id,
                    )
                )
            response = await post(app, f"/auth/users/{user_id}/delete", {"csrf_token": ""})
            assert response.status_code == 303
            assert response.headers["location"] == "/auth/users?deleted=1"
            assert await fetch_user(app, user_id) is None
            async with app.uow(principal=SUPERUSER) as ctx:
                links = await ctx.session.scalar(
                    sa.select(sa.func.count())
                    .select_from(user_roles)
                    .where(user_roles.c.user_id == user_id)
                )
                sessions = await ctx.session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Session)
                    .where(Session.user_id == user_id)
                )
                assert links == 0
                assert sessions == 0
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_roles_page_builds_permission_tree_from_builders() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            role_id = await seed_role(
                app, "Editors", ["admin.widgets.list", "legacy.permission.keep"]
            )
            page = await get(app, "/auth/roles")
            assert page.status_code == 200
            assert "Editors" in page.text
            assert 'data-tree-level="service"' in page.text
            assert 'data-tree-level="resource"' in page.text
            assert 'data-tree-level="action"' in page.text
            assert page.text.count('class="tree-arrow"') >= 2
            assert re.search(
                r'<span class="tree-arrow"[^>]*>.*?data-tree-level="service"',
                page.text,
                re.S,
            )
            for value in (
                "admin.widgets.list",
                "admin.widgets.delete",
                "admin.widgets.retrieve",
                "admin.widgets.delete",
                "blog.articles.update",
                "blog.articles.retrieve",
                "blog.articles.approve",
                "blog.articles.export",
            ):
                assert f'value="{value}"' in page.text
            assert re.search(r'value="admin\.widgets\.list"[^>]*checked', page.text)
            assert "legacy.permission.keep" not in page.text
            assert f"role-name-{role_id}" in page.text
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_role_update_preserves_unknown_permissions_and_validates() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            role_id = await seed_role(
                app, "Readers", ["legacy.permission.keep", "admin.widgets.list"]
            )
            other_id = await seed_role(app, "Maintainers")
            response = await post(
                app,
                f"/auth/roles/{role_id}",
                {
                    "csrf_token": "",
                    "name": "Editors",
                    "permissions": [
                        "admin.widgets.update",
                        "admin.widgets.delete",
                        "blog.articles.approve",
                    ],
                },
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/auth/roles?saved=1"

            role = await fetch_role(app, role_id)
            assert role.name == "Editors"
            assert set(role.permissions) == {
                "legacy.permission.keep",
                "admin.widgets.update",
                "admin.widgets.delete",
                "blog.articles.approve",
            }

            invalid = await post(
                app,
                f"/auth/roles/{role_id}",
                {
                    "csrf_token": "",
                    "name": "Editors",
                    "permissions": "unknown.permission.read",
                },
            )
            assert invalid.status_code == 422
            assert "Choose valid permissions." in invalid.text
            assert set((await fetch_role(app, role_id)).permissions) == {
                "legacy.permission.keep",
                "admin.widgets.update",
                "admin.widgets.delete",
                "blog.articles.approve",
            }

            duplicate = await post(
                app,
                f"/auth/roles/{role_id}",
                {
                    "csrf_token": "",
                    "name": "Maintainers",
                    "permissions": "admin.widgets.update",
                },
            )
            assert duplicate.status_code == 422
            assert "This role name is already taken." in duplicate.text
            assert (await fetch_role(app, role_id)).name == "Editors"
            assert await fetch_role(app, other_id) is not None
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_role_create_and_delete_removes_user_links() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            assigned = await seed_role(app, "Assigned")
            user_id = await seed_user(app, "alice", role_ids=[assigned])
            response = await post(
                app,
                "/auth/roles",
                {
                    "csrf_token": "",
                    "name": "Operators",
                    "permissions": ["admin.widgets.list", "admin.widgets.retrieve"],
                },
            )
            assert response.status_code == 303
            assert response.headers["location"] == "/auth/roles?saved=1"

            async with app.uow(principal=SUPERUSER) as ctx:
                role = await ctx.session.scalar(sa.select(Role).where(Role.name == "Operators"))
                assert role is not None
                assert set(role.permissions) == {
                    "admin.widgets.list",
                    "admin.widgets.retrieve",
                }
                created_id = role.id

            duplicate = await post(
                app,
                "/auth/roles",
                {"csrf_token": "", "name": "Operators"},
            )
            assert duplicate.status_code == 422
            assert "This role name is already taken." in duplicate.text

            deleted = await post(app, f"/auth/roles/{assigned}/delete", {"csrf_token": ""})
            assert deleted.status_code == 303
            assert deleted.headers["location"] == "/auth/roles?deleted=1"
            assert await fetch_role(app, assigned) is None
            assert await fetch_role(app, created_id) is not None
            user = await fetch_user(app, user_id)
            assert user.roles == []
        finally:
            await engine.dispose()

    asyncio.run(scenario())


def test_csrf_token_is_required_and_accepted() -> None:
    async def scenario() -> None:
        app, engine = await make_app()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app),
                base_url="http://test",
                cookies={"obox_session": "test"},
            ) as client:
                page = await client.get("/auth/users/new")
                match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
                assert match is not None
                token = match.group(1)

                rejected = await client.post(
                    "/auth/users/new",
                    data={
                        "csrf_token": "invalid",
                        "username": "rejected",
                        "password": PASSWORD,
                        "confirm_password": PASSWORD,
                        "is_active": "1",
                    },
                )
                assert rejected.status_code == 403

                accepted = await client.post(
                    "/auth/users/new",
                    data={
                        "csrf_token": token,
                        "username": "accepted",
                        "password": PASSWORD,
                        "confirm_password": PASSWORD,
                        "is_active": "1",
                    },
                )
                assert accepted.status_code == 303
                assert accepted.headers["location"] == "/auth/users?saved=1"

            async with app.uow(principal=SUPERUSER) as ctx:
                usernames = set((await ctx.session.scalars(sa.select(User.username))).all())
                assert usernames == {"accepted"}
        finally:
            await engine.dispose()

    asyncio.run(scenario())
