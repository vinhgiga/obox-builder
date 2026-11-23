from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from obox import Resource
from obox._config import RuntimeConfig
from obox._runtime import _Runtime
from obox.api import ModelApi
from obox.core import (
    SYSTEM,
    BuilderValidationError,
    NotFound,
    PermissionDenied,
    Principal,
    ResourceConflict,
    RuleViolation,
)
from obox.core.exceptions import ImproperlyConfigured
from obox.views.generic import CreateView, View
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from tests.storage import database
from tests.test_forms_integration import configured


class Base(DeclarativeBase):
    pass


class Entry(Base):
    __tablename__ = "resource_entries"
    __table_args__ = (sa.UniqueConstraint("owner_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    owner_id: Mapped[int]
    name: Mapped[str] = mapped_column(sa.String(40))
    amount: Mapped[int] = mapped_column(default=1)
    payload: Mapped[dict[str, Any]] = mapped_column(sa.JSON, default=dict)
    children: Mapped[list[Child]] = relationship(lazy="raise")


class Child(Base):
    __tablename__ = "resource_children"
    id: Mapped[int] = mapped_column(primary_key=True)
    entry_id: Mapped[int] = mapped_column(sa.ForeignKey(Entry.id))


class EntryInput(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    amount: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def check_amount(self) -> EntryInput:
        if self.name == "limited" and self.amount > 2:
            raise ValueError("Amount exceeds the limit.")
        return self


class Entries(Resource[Entry]):
    model = Entry
    schema = EntryInput
    readonly_fields = ("owner_id",)
    fields = ("id", "name", "amount", "owner_id", "payload")
    ordering = ("name",)
    load_options = (selectinload(Entry.children),)

    def get_queryset(self) -> sa.Select[tuple[Entry]]:
        return super().get_queryset().where(Entry.owner_id == self.principal.id)

    async def perform_create(self, data: dict[str, Any]) -> Entry:
        entry = Entry(**data, owner_id=self.principal.id)
        self._session().add(entry)
        await self._session().flush()
        if entry.name == "blocked":
            raise RuleViolation("quota_exceeded", "Quota exceeded.")
        if entry.name == "commit-failure":

            def fail_commit(session: Any) -> None:
                if not session.in_nested_transaction():
                    raise sa.exc.IntegrityError("commit", {}, ValueError("conflict"))

            sa.event.listen(self._session().sync_session, "before_commit", fail_commit)
        return entry


@pytest.mark.parametrize("page_size", [1, 2])
def test_resource_scope_unique_pagination_and_eager_loading(tmp_path: Path, page_size: int) -> None:
    async def scenario() -> None:
        async with database(tmp_path / "resource.sqlite3", (Base.metadata,)) as sessions:
            async with sessions.begin() as session:
                first = Entry(owner_id=1, name="a", payload={"test": True})
                second = Entry(owner_id=1, name="b")
                other = Entry(owner_id=2, name="private")
                session.add_all([first, second, other])
                await session.flush()
                session.add_all(
                    [Child(entry_id=first.id), Child(entry_id=first.id), Child(entry_id=second.id)]
                )
            async with sessions.begin() as session:
                resource = Entries(session, Principal(id=1))
                query = sa.select(Entry).join(Child).order_by(Entry.name)
                page = await resource.paginate(1, page_size=page_size, queryset=query)
                assert page.total == 2
                assert len(page) == page_size
                assert len({entry.id for entry in page}) == page_size
                window = await resource.list(
                    queryset=sa.select(Entry), limit=1, offset=1, include_total=True
                )
                assert window.total == 2
                assert [entry.name for entry in window.items] == ["b"]
                assert not window.has_more
                assert (
                    await resource.paginate(
                        1, page_size=1, queryset=sa.select(Entry).where(Entry.name == "absent")
                    )
                ).total == 0
                with pytest.raises(NotFound):
                    await resource.paginate(3, page_size=1)
                with pytest.raises(NotFound):
                    await resource.get_object(other.id)
                outside = await session.get(Entry, other.id)
                assert outside is not None
                with pytest.raises(NotFound):
                    await resource.destroy(outside)
                with pytest.raises(NotFound):
                    await resource.update(outside, {"name": "stolen"}, partial=True)
            assert len(page.items[0].children) == 2

    asyncio.run(scenario())


def test_resource_crud_partial_validation_and_savepoint(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with database(tmp_path / "resource.sqlite3", (Base.metadata,)) as sessions:
            declaration = Entries()
            async with sessions.begin() as session:
                first = declaration.bind(session, SYSTEM)
                entry = await first.create(EntryInput(name="limited", amount=2))
                with pytest.raises(BuilderValidationError):
                    await first.update(entry, {"amount": 3}, partial=True)
                assert entry.amount == 2
                with pytest.raises(BuilderValidationError):
                    await first.update(entry, {"amount": 1})
                await first.update(entry, {"amount": 1}, partial=True)
                with pytest.raises(BuilderValidationError):
                    await first.update(entry, {"amount": None}, partial=True)
                with pytest.raises(ResourceConflict):
                    async with session.begin_nested():
                        await first.create(EntryInput(name="limited"))
                assert (await first.list(include_total=True)).total == 1
                await first.destroy(entry)
                assert (await first.list(include_total=True)).total == 0
            with pytest.raises(ImproperlyConfigured, match="active session"):
                await declaration.list()

    asyncio.run(scenario())


@pytest.mark.parametrize("surface", ["html", "api"])
@pytest.mark.parametrize(("name", "status"), [("ok", 0), ("blocked", 0), ("commit-failure", 409)])
def test_adapters_share_resource_rules_and_transaction_boundary(
    tmp_path: Path, surface: str, name: str, status: int
) -> None:
    async def principal(request: Request) -> Principal:
        actor = Principal(id=1, is_authenticated=True, is_superuser=True)
        request.state.obox_bearer_principal = actor
        return actor

    class EntryCreate(CreateView):
        resource = Entries
        success_url = "/done"
        csrf_protect = False

    class EntryApi(ModelApi[Entry]):
        resource = Entries
        include_router_methods = ("list", "retrieve", "create")

    async def scenario() -> None:
        async with (
            configured(),
            database(tmp_path / "resource.sqlite3", (Base.metadata,)) as sessions,
        ):
            app = FastAPI()
            runtime = _Runtime(
                app,
                settings=RuntimeConfig(
                    session_factory=sessions, principal_dependency=principal, csrf_secret="s" * 32
                ),
            )
            router = APIRouter()
            EntryCreate.as_route(router, "/new", name="new")
            app.include_router(router)
            app.include_router(EntryApi.as_router("/api/entries"))
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                response = (
                    await client.post("/new", data={"name": name, "amount": "1"})
                    if surface == "html"
                    else await client.post("/api/entries", json={"name": name, "amount": 1})
                )
                expected = status or (
                    200
                    if name == "blocked" and surface == "html"
                    else 422
                    if name == "blocked"
                    else 303
                    if surface == "html"
                    else 201
                )
                assert response.status_code == expected, response.text
                if name == "blocked":
                    assert "Quota exceeded." in response.text
                async with sessions() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Entry)) == (
                        1 if name == "ok" else 0
                    )
                if name == "ok":
                    async with runtime.uow(principal=Principal(id=1)) as ctx:
                        with pytest.raises(ImproperlyConfigured, match="unit of work"):
                            await ctx.session.commit()

    asyncio.run(scenario())


def test_html_route_catches_dependency_entry_and_exit_errors() -> None:
    async def dependency():
        yield
        raise ResourceConflict("Commit failed.")

    class Endpoint(View):
        dependencies = (Depends(dependency, scope="function"),)
        csrf_protect = False

        async def get(self, request: Request, **kwargs: Any) -> HTMLResponse:
            return HTMLResponse("success")

    async def scenario() -> None:
        app = FastAPI()
        router = APIRouter()
        Endpoint.as_route(router, "/endpoint", name="endpoint")
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/endpoint")
            assert response.status_code == 409
            assert "Commit failed." in response.text
            assert "success" not in response.text

    asyncio.run(scenario())


def test_per_action_schemas_defaults_and_invalid_configuration(tmp_path: Path) -> None:
    class CreateInput(BaseModel):
        name: str = "default-name"

    class UpdateInput(BaseModel):
        amount: int = Field(ge=1)

    class Configured(Entries):
        schema = None
        create_schema = CreateInput
        update_schema = UpdateInput

    async def scenario() -> None:
        async with (
            database(tmp_path / "schemas.sqlite3", (Base.metadata,)) as sessions,
            sessions.begin() as session,
        ):
            resource = Configured(session, Principal(id=1))
            entry = await resource.create({})
            assert entry.name == "default-name"
            await resource.update(entry, {"amount": 4}, partial=True)
            assert entry.amount == 4
            with pytest.raises(PermissionDenied, match="Fields cannot be written"):
                await resource.update(entry, {"name": "denied"}, partial=True)
        with pytest.raises(ImproperlyConfigured):
            type("Broken", (Resource,), {"model": Entry, "ordering": "name"}).check()
        with pytest.raises(ImproperlyConfigured):
            type("Broken", (Resource,), {"model": Entry, "page_size": 0}).check()

    asyncio.run(scenario())


@pytest.mark.parametrize("action", ["update", "delete"])
def test_write_rule_violation_restores_object_before_rendering(tmp_path: Path, action: str) -> None:
    from obox.views.generic import DeleteView, UpdateView

    class Guarded(Entries):
        async def perform_update(
            self, obj: Entry, data: dict[str, Any], *, partial: bool = False
        ) -> Entry:
            await super().perform_update(obj, data, partial=partial)
            raise RuleViolation("blocked", "Cannot change this record.")

        async def perform_destroy(self, obj: Entry) -> None:
            await super().perform_destroy(obj)
            raise RuleViolation("blocked", "Cannot change this record.")

    class Edit(UpdateView if action == "update" else DeleteView):
        resource = Guarded
        csrf_protect = False
        success_url = "/done"

    async def actor() -> Principal:
        return Principal(id=1, is_authenticated=True)

    async def scenario() -> None:
        async with (
            configured(),
            database(tmp_path / "update.sqlite3", (Base.metadata,)) as sessions,
        ):
            async with sessions.begin() as session:
                session.add(Entry(id=1, owner_id=1, name="original"))
            app = FastAPI()
            _Runtime(
                app,
                settings=RuntimeConfig(
                    session_factory=sessions, principal_dependency=actor, csrf_secret="s" * 32
                ),
            )
            router = APIRouter()
            Edit.as_route(router, "/entries/{pk}", name="edit")
            app.include_router(router)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                response = await client.post(
                    "/entries/1", data={"name": "attempted", "amount": "2"}
                )
                assert response.status_code == 200, response.text
                assert "Cannot change this record." in response.text
                if action == "update":
                    assert 'value="attempted"' in response.text
            async with sessions() as session:
                assert await session.scalar(sa.select(Entry.name)) == "original"

    asyncio.run(scenario())


@pytest.mark.parametrize("native", [True, False])
def test_html_error_template_methods_and_configuration_checks(native: bool) -> None:
    from fastapi.templating import Jinja2Templates
    from jinja2 import DictLoader, Environment
    from obox.views.generic import DeleteView, FormView, ListView, TemplateView, UpdateView
    from starlette.exceptions import HTTPException

    class Missing(View):
        csrf_protect = False

        async def get(self, request: Request, **kwargs: Any) -> HTMLResponse:
            if native:
                raise HTTPException(404, "Unavailable.")
            raise NotFound("Unavailable.")

    async def scenario() -> None:
        app = FastAPI()
        app.state.templates = Jinja2Templates(
            env=Environment(
                loader=DictLoader({"404.html": "<h1>Missing page</h1>"}), autoescape=True
            )
        )
        router = APIRouter()
        Missing.as_route(router, "/missing", name="missing")
        app.include_router(router)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.get("/missing")
            assert response.status_code == 404
            assert "Missing page" in response.text
            assert response.headers["cache-control"] == "private, no-store"
            assert (await client.head("/missing")).content == b""
            unsupported = await client.post("/missing")
            assert unsupported.status_code == 405
            assert "GET" in unsupported.headers["allow"]
        with pytest.raises(ImproperlyConfigured):
            TemplateView.as_route(router, "/bad-template", name="bad-template")
        with pytest.raises(ImproperlyConfigured):
            ListView.as_route(router, "/bad-list", name="bad-list")
        with pytest.raises(ImproperlyConfigured):
            FormView.as_route(router, "/bad-form", name="bad-form")
        for view in (CreateView, UpdateView, DeleteView):
            with pytest.raises(ImproperlyConfigured, match="success_url"):
                view.as_route(
                    router, "/bad-redirect", name="bad-redirect", initkwargs={"resource": Entries}
                )

    asyncio.run(scenario())
