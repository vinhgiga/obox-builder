from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, cast

import httpx
import pytest
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from jinja2 import ChoiceLoader, FileSystemLoader
from obox import ModelResource
from obox.api import ModelApi
from obox.core import (
    BuilderValidationError,
    NotFound,
    PermissionDenied,
    Principal,
    ResourceConflict,
    RuleViolation,
)
from obox.core.exceptions import ImproperlyConfigured
from obox.runtime.config import RuntimeConfig
from obox.runtime.runtime import _Runtime
from obox.services import BaseService
from obox.views.generic import CreateView, DetailView, ListView, View
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import (
    DeclarativeBase,
    InstrumentedAttribute,
    Mapped,
    mapped_column,
    relationship,
    selectinload,
)
from sqlalchemy.sql.elements import ColumnElement

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


class EntryService(BaseService):
    async def create(self, data: dict[str, Any]) -> Entry:
        entry = Entry(**data, owner_id=self.principal.id)
        self.session.add(entry)
        await self.session.flush()
        if entry.name == "blocked":
            raise RuleViolation("quota_exceeded", "Quota exceeded.")
        if entry.name == "commit-failure":

            def fail_commit(session: Any) -> None:
                if not session.in_nested_transaction():
                    raise sa.exc.IntegrityError("commit", {}, ValueError("conflict"))

            sa.event.listen(self.session.sync_session, "before_commit", fail_commit)
        return entry


class Entries(ModelResource[Entry]):
    model = Entry
    schema: type[BaseModel] | None = EntryInput
    readonly_fields = ("owner_id",)
    fields = ("id", "name", "amount", "owner_id", "payload")
    ordering: Sequence[str | ColumnElement[Any] | InstrumentedAttribute[Any]] = ("name",)
    load_options = (selectinload(Entry.children),)
    service_class = EntryService

    def get_queryset(self) -> sa.Select[tuple[Entry]]:
        return super().get_queryset().where(Entry.owner_id == self.principal.id)

    async def perform_create(self, data: dict[str, Any]) -> Entry:
        return await cast(EntryService, self.service).create(data)


def test_html_and_api_share_page_order_and_scoped_lookup(tmp_path: Path) -> None:
    class Directory(Entries):
        lookup_field = "name"
        page_size = 1
        ordering = (Entry.name.desc(),)

        async def check_permission(self, action: str, obj: Entry | None = None) -> None:
            if action == "retrieve" and obj is not None and obj.name == "restricted":
                raise PermissionDenied("Restricted entry.")
            await super().check_permission(action, obj)

    class Index(ListView):
        resource = Directory
        template_prefix = "listing/"

    class Detail(DetailView):
        resource = Directory
        slug_field = "name"

        def get_queryset(self) -> sa.Select[tuple[Entry]]:
            return sa.select(Entry)

    class Output(BaseModel):
        model_config = ConfigDict(from_attributes=True)
        name: str

    async def principal(request: Request) -> Principal:
        return Principal(id=1, is_authenticated=True, is_superuser=True)

    class DirectoryAPI(ModelApi[Entry]):
        resource = Directory
        read_schema = Output
        pagination = "page"
        lookup_url_kwarg = "slug"
        page_size_param = "per_page"
        max_page_size = 2
        principal_dependency = staticmethod(principal)
        error_status = MappingProxyType({PermissionDenied: 404})

    async def scenario() -> None:
        template = tmp_path / "listing" / "resource_entries_list.html"
        template.parent.mkdir()
        template.write_text("{{ page_obj.number }}:{{ object_list[0].name }}", encoding="utf-8")
        async with configured(), database(tmp_path / "pages.sqlite3", (Base.metadata,)) as sessions:
            async with sessions.begin() as session:
                session.add_all(
                    Entry(owner_id=owner, name=name)
                    for owner, name in [
                        (1, "alpha"),
                        (1, "restricted"),
                        (1, "zeta"),
                        (2, "private"),
                    ]
                )
            app = FastAPI()
            runtime = _Runtime(
                app,
                settings=RuntimeConfig(
                    session_factory=sessions, principal_dependency=principal, csrf_secret="s" * 32
                ),
            )
            loader = runtime.templates.env.loader
            assert loader is not None
            runtime.templates.env.loader = ChoiceLoader([FileSystemLoader(tmp_path), loader])
            router = APIRouter()
            Index.as_route(router, "/entries", name="entry-list")
            Detail.as_route(router, "/entries/{slug}", name="entry-detail")
            app.include_router(router)
            app.include_router(
                DirectoryAPI.as_router("/api/entries", tags=["entries"], name_prefix="entries")
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert (await client.get("/entries?page=2")).text == "2:restricted"
                assert (await client.get("/api/entries?page=2")).json() == {
                    "items": [{"name": "restricted"}],
                    "page": 2,
                    "page_size": 1,
                    "total": 3,
                    "pages": 3,
                }
                assert len((await client.get("/api/entries?per_page=2")).json()["items"]) == 2
                capped = await client.get("/api/entries?per_page=3")
                assert capped.status_code == 200
                assert capped.json()["page_size"] == 2
                for prefix in ("/entries", "/api/entries"):
                    assert (await client.get(f"{prefix}?page={10**40}")).status_code == 404
                    assert (await client.get(f"{prefix}/private")).status_code == 404
                    assert (await client.get(f"{prefix}/alpha")).status_code == 200
                assert (await client.get("/entries/restricted")).status_code == 403
                denied = await client.get("/api/entries/restricted")
                assert denied.status_code == 404
                assert denied.json()["code"] == "permission_denied"
                assert (await client.post("/api/entries", json={"name": "new"})).status_code == 405
                assert (await client.delete("/api/entries/alpha")).status_code == 405
                assert (
                    str(app.url_path_for("entries-retrieve", slug="alpha")) == "/api/entries/alpha"
                )
                paths = app.openapi()["paths"]
                assert set(paths["/api/entries"]) == {"get"}
                listing = paths["/api/entries"]["get"]
                assert listing["tags"] == ["entries"]
                assert {param["name"] for param in listing["parameters"]} == {
                    "page",
                    "per_page",
                    "filter",
                    "search",
                    "order",
                }
                lookup = paths["/api/entries/{slug}"]["get"]["parameters"][0]
                assert lookup["name"] == "slug"
                assert lookup["schema"]["type"] == "string"

    asyncio.run(scenario())


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
                assert isinstance(resource, ModelResource)
                assert resource.service is resource.service
                assert resource.bind(session, Principal(id=2)).service.principal.id == 2
                query = sa.select(Entry).join(Child).order_by(Entry.name)
                page = await resource.paginate(1, page_size=page_size, queryset=query)
                assert page.total == 2
                assert len(page) == page_size
                assert (page.start_index, page.end_index) == (1, page_size)
                assert page.next_number == (2 if page_size == 1 else None)
                assert page.previous_number is None
                assert page.is_paginated == (page_size == 1)
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
                first = declaration.bind(session, Principal(id=1))
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

    class ChildOutput(BaseModel):
        model_config = ConfigDict(from_attributes=True)
        id: int

    class EntryOutput(BaseModel):
        model_config = ConfigDict(from_attributes=True)
        name: str
        amount: int
        children: list[ChildOutput]

    class EntryApi(ModelApi[Entry]):
        resource = Entries
        read_schema = EntryOutput
        actions = ("list", "retrieve", "create", "update", "partial_update", "destroy")

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
                    if surface == "api":
                        assert response.json() == {"name": "ok", "amount": 1, "children": []}
                        detail = response.headers["location"]
                        assert (await client.put(detail, json={"amount": 3})).status_code == 422
                        changed = await client.put(detail, json={"name": "limited", "amount": 2})
                        assert changed.status_code == 200
                        assert changed.json() == {"name": "limited", "amount": 2, "children": []}
                        assert (await client.patch(detail, json={"amount": 3})).status_code == 422
                        reset = await client.put(detail, json={"name": "limited"})
                        assert reset.status_code == 200
                        assert reset.json()["amount"] == 1
                        removed = await client.delete(detail)
                        assert removed.status_code == 204
                        assert removed.content == b""
                        assert (await client.get(detail)).status_code == 404

    asyncio.run(scenario())


def test_html_route_catches_dependency_entry_and_exit_errors() -> None:
    async def dependency() -> AsyncIterator[None]:
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
        for options in ({"ordering": "name"}, {"page_size": 0}, {"service_class": object}):
            broken = cast(
                type[ModelResource[Entry]],
                type("Broken", (ModelResource,), {"model": Entry, **options}),
            )
            with pytest.raises(ImproperlyConfigured):
                broken.check()

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

    edit = cast(
        type[View],
        type(
            "Edit",
            (UpdateView if action == "update" else DeleteView,),
            {"resource": Guarded, "csrf_protect": False, "success_url": "/done"},
        ),
    )

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
            edit.as_route(router, "/entries/{pk}", name="edit")
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
