from __future__ import annotations

import asyncio
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Annotated

import httpx
import pytest
from fastapi import Depends, FastAPI, Query, Request
from obox import ModelResource, Obox
from obox.api import (
    PUBLIC,
    ApiRequest,
    ApiResponse,
    Context,
    ModelApi,
    RouterMethod,
    api_context,
    route,
)
from obox.core.exceptions import ImproperlyConfigured, NotFound
from obox.core.principal import Principal
from obox.db.engine import default_session_factory

from tests.test_forms_integration import Item, configured
from tests.test_resources import Items

ARCHIVE = "catalog.item.archive"
SYNC = "catalog.item.sync"
RUN_SYNC = "catalog.item.run_sync"
ITEMS = "/v0/v01_form_items"


def resolve(request: Request) -> Principal:
    permissions = frozenset(request.headers.get("x-test-permissions", "").split())
    return Principal(id=1, is_authenticated=bool(permissions), permissions=permissions)


class SyncInput(ApiRequest):
    count: int


class SyncResult(ApiResponse):
    total: int


def test_public_crud_scopes_data_and_replaces_default_permissions() -> None:
    class PublicItems(Items):
        group_fields = ("quantity",)

    class ItemApi(ModelApi[Item]):
        resource = PublicItems
        actions = ("list", "retrieve", "delete")
        permissions = MappingProxyType({"list": PUBLIC, "retrieve": PUBLIC})

    class ArchiveItems(Items):
        async def can(self, action: str, obj: Item | None, actor: Principal) -> bool:
            return action == "delete" and obj is not None and obj.name != "keep"

    class ArchiveApi(ModelApi[Item]):
        actions: tuple[RouterMethod, ...] = ("delete",)
        permissions = MappingProxyType({"delete": ARCHIVE})
        principal_dependency = staticmethod(resolve)

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            obox = Obox(app)
            obox.register(PublicItems, api=ItemApi, views=False)
            app.include_router(ArchiveApi().build_resource_router("/archive", ArchiveItems()))
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                async with default_session_factory().begin() as session:
                    session.add_all(
                        Item(id=i, name=name, price=Decimal("2"), quantity=i)
                        for i, name in enumerate(("public", "private", "keep"), 1)
                    )
                response = await client.get(ITEMS)
                assert response.status_code == 200
                assert {row["name"] for row in response.json()["items"]} == {"public", "keep"}
                assert (await client.head(ITEMS)).status_code == 200
                assert (await client.get(f"{ITEMS}/1")).json()["name"] == "public"
                assert (await client.get(f"{ITEMS}/2")).status_code == 404
                groups = await client.get(ITEMS + ":groups", params={"group": "quantity"})
                assert groups.status_code == 200
                assert {row["key"]["quantity"] for row in groups.json()["items"]} == {1, 3}
                assert (await client.patch(f"{ITEMS}/1", json={})).status_code == 405
                assert (await client.delete(f"{ITEMS}/1")).status_code == 401

                assert (
                    await client.delete(
                        "/archive/1", headers={"x-test-permissions": "tests.delete_item"}
                    )
                ).status_code == 403
                archive = {"x-test-permissions": ARCHIVE}
                assert (await client.delete("/archive/3", headers=archive)).status_code == 403
                assert (await client.delete("/archive/1", headers=archive)).status_code == 204
                assert ARCHIVE in obox._runtime.permissions

    asyncio.run(scenario())


def test_custom_methods_use_fastapi_validation_and_obox_policy() -> None:
    async def label() -> str:
        return "resolved"

    class BaseApi(ModelApi[Item]):
        actions: tuple[RouterMethod, ...] = ("retrieve",)
        principal_dependency = staticmethod(resolve)

        @route.get("/sync", permission=PUBLIC)
        def sync(self, ctx: Context) -> dict[str, bool]:
            return {"authenticated": ctx.principal.is_authenticated}

        @route.get("/removed", permission=PUBLIC)
        def removed(self) -> dict[str, bool]:
            return {"removed": False}

    class ItemApi(BaseApi):
        resource = Items
        error_status = MappingProxyType({NotFound: 410})
        removed = None  # type: ignore[assignment]

        @route.post("/sync", permission=(SYNC, RUN_SYNC), status_code=202, name="run-sync")
        async def enqueue(
            self,
            data: SyncInput,
            ctx: Context,
            *,
            factor: Annotated[int, Query(ge=1)],
            value: str = Depends(label),
        ) -> SyncResult:
            if data.count < 0:
                raise NotFound("Unavailable.")
            assert ctx.source == "api"
            assert value == "resolved"
            return SyncResult(total=data.count * factor)

        @route.get("/{item_id}/status", permission=PUBLIC)
        async def status(self, item_id: int) -> dict[str, int]:
            return {"id": item_id}

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            obox = Obox(app)
            obox.register(Items, api=ItemApi, views=False)
            path = ITEMS + "/sync"
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert (await client.get(path)).json() == {"authenticated": False}
                assert (await client.head(path)).content == b""
                assert (await client.get(ITEMS + "/4/status")).json() == {"id": 4}
                assert (await client.get(ITEMS + "/removed")).status_code == 401
                assert (await client.get(path, headers={"x-test-permissions": SYNC})).json() == {
                    "authenticated": True
                }
                for permissions, status in (("", 401), (SYNC, 403), (RUN_SYNC, 403)):
                    response = await client.post(
                        path,
                        json={"count": 2},
                        headers={"x-test-permissions": permissions},
                        params={"factor": 3},
                    )
                    assert response.status_code == status
                both = {"x-test-permissions": f"{SYNC} {RUN_SYNC}"}
                response = await client.post(
                    path, json={"count": 2}, headers=both, params={"factor": 3}
                )
                assert response.status_code == 202
                assert response.json() == {"total": 6}
                assert response.headers["cache-control"] == "private, no-store"
                invalid = await client.post(
                    path, json={"count": 2}, headers=both, params={"factor": 0}
                )
                assert invalid.status_code == 422
                problem = await client.post(
                    path, json={"count": -1}, headers=both, params={"factor": 1}
                )
                assert problem.status_code == 410
                assert problem.headers["content-type"] == "application/problem+json"
                client.cookies.set("session", "present")
                denied = await client.post(
                    path, json={"count": 2}, headers=both, params={"factor": 1}
                )
                assert denied.status_code == 403
                assert {SYNC, RUN_SYNC} <= obox._runtime.permissions.keys()
                assert app.url_path_for("run-sync") == path
                operation = app.openapi()["paths"][path]["post"]
                assert [parameter["name"] for parameter in operation["parameters"]] == ["factor"]
                assert operation["requestBody"]["content"]["application/json"]["schema"][
                    "$ref"
                ].endswith("/SyncInput")
                assert operation["responses"]["202"]["content"]["application/json"]["schema"][
                    "$ref"
                ].endswith("/SyncResult")

    asyncio.run(scenario())


def test_router_builds_use_their_own_resource() -> None:
    class ItemApi(ModelApi[Item]):
        resource = Items
        actions: tuple[RouterMethod, ...] = ("retrieve",)
        permissions = MappingProxyType({"retrieve": PUBLIC})

    class CompactItems(ModelResource[Item]):
        model = Item
        fields = ("name",)

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            Obox(app)
            api = ItemApi()
            app.include_router(api.build_router("/first"))
            app.include_router(api.build_resource_router("/second", CompactItems()))
            assert vars(api) == {}
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                async with default_session_factory().begin() as session:
                    session.add(Item(id=1, name="one", price=Decimal("2")))
                assert (await client.get("/first/1")).json()["id"] == 1
                assert (await client.get("/second/1")).json() == {"name": "one"}

    asyncio.run(scenario())


def test_duplicate_and_unsafe_custom_routes_are_rejected() -> None:
    with pytest.raises(ImproperlyConfigured, match=r"PUBLIC.*GET and HEAD"):
        route.post("/unsafe", permission=PUBLIC)
    with pytest.raises(ImproperlyConfigured, match="Invalid ModelApi route path"):
        route.get("/{id:no_such_converter}", permission=PUBLIC)
    with pytest.raises(ImproperlyConfigured, match="Reserved ModelApi action names"):
        type("Invalid", (ModelApi,), {"list": None})

    class ConflictingApi(ModelApi[Item]):
        resource = Items
        actions: tuple[RouterMethod, ...] = ("retrieve",)

        @route.get("/{alias}", permission=PUBLIC)
        async def lookup(self, alias: int) -> int:
            return alias

    with pytest.raises(ImproperlyConfigured, match="Duplicate API route"):
        ConflictingApi.as_router("/items")


def test_native_public_policy_audit_rejects_writes() -> None:
    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            Obox(app)

            @app.post("/unsafe", dependencies=[Depends(api_context(permission=PUBLIC))])
            async def post() -> None:
                pass

            with pytest.raises(ImproperlyConfigured, match=r"PUBLIC.*GET and HEAD"):
                async with app.router.lifespan_context(app):
                    pytest.fail("Unsafe route passed the audit.")

    asyncio.run(scenario())


@pytest.mark.parametrize("checker", ["mypy", "pyright"])
def test_custom_route_and_callback_typing(tmp_path: Path, checker: str) -> None:
    source = tmp_path / "model_api_types.py"
    example = """
from typing import assert_type
from obox import ModelResource
from obox.api import PUBLIC, Context, ModelApi, route

class Items(ModelApi[int]):
    @route.get("/status", permission=PUBLIC)
    async def status(self, ctx: Context, count: int) -> str:
        return str(count)

def notify(record_id: int, *, subject: str) -> None:
    pass

async def run(ctx: Context, resource: ModelResource[int]) -> None:
    assert_type(await Items().status(ctx, 1), str)
    ctx.on_commit(notify, 1, subject="deleted")
    resource.on_commit(notify, 1, subject="deleted")
"""
    source.write_text(example)
    command = [sys.executable, "-m", checker]
    if checker == "mypy":
        command += ["--strict", "--no-incremental"]
    result = subprocess.run([*command, str(source)], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode == 0, result.stdout + result.stderr
    source.write_text(
        example
        + "    await Items().status(ctx, 1, 2)\n"
        + '    ctx.on_commit(notify, "bad", subject="deleted")\n'
        + "    resource.on_commit(notify, 1, subject=2)\n"
    )
    result = subprocess.run([*command, str(source)], capture_output=True, text=True, check=False)  # noqa: S603
    assert result.returncode != 0
    first_invalid_line = example.count("\n") + 1
    for line in range(first_invalid_line, first_invalid_line + 3):
        assert f"{source}:{line}:" in result.stdout, result.stdout
