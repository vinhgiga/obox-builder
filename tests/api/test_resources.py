from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import obox_builder
import pytest
from fastapi import FastAPI
from obox_builder.conf import settings
from obox_builder.contrib.api import Api
from obox_builder.db import dispose, get_engine, get_sessionmaker
from obox_builder.db.base import ModelBase
from obox_builder.fields import File
from obox_builder.resources import OperationContext, ResourceRegistry, operation
from pydantic import BaseModel, Field
from tests.resources.test_operations import Base, Record, Records
from tests.support.auth_settings import Settings as AuthSettings


class Settings(AuthSettings):
    INSTALLED_APPS: list[str] = ["obox_builder.contrib.auth", "obox_builder.contrib.api"]


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=80)


class Renamed(BaseModel):
    affected: int


class ApiRecords(Records):
    @operation(scope="selection", permission="update")
    async def rename_selection(
        self, ctx: OperationContext, objects: list[Record], data: Rename
    ) -> Renamed:
        for obj in objects:
            obj.name = data.name
        await ctx.session.flush()
        return Renamed(affected=len(objects))


class FileRecords(Records):
    fields_config = {"payload": File(accept=("image/png",), max_bytes=1024)}


@pytest.fixture
async def client(request: pytest.FixtureRequest) -> AsyncIterator[httpx.AsyncClient]:
    settings.configure(Settings)
    obox_builder.setup()
    from obox_builder.contrib.auth.models import Permission, Role
    from obox_builder.contrib.auth.services import create_user

    assert not obox_builder.apps.is_installed("obox_builder.contrib.admin")
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(ModelBase.metadata.create_all)
        await conn.run_sync(Base.metadata.create_all)
    async with get_sessionmaker()() as db:
        read = Permission(service="example", resource="record", action="read")
        update = Permission(service="example", resource="record", action="update")
        role = Role(name="Operator", permissions=[read, update])
        user = await create_user(db, "operator", "test-password")
        await db.refresh(user, ["roles"])
        user.roles.append(role)
        db.add_all(
            [
                Record(id=1, owner_id=user.id, name="visible"),
                Record(id=2, owner_id=user.id + 1, name="hidden"),
            ]
        )
        await db.commit()
    resource = getattr(request, "param", ApiRecords)
    api = Api(registry=ResourceRegistry([resource]))
    api.register(resource, path="/records")
    app = FastAPI()
    app.include_router(api.get_router(), prefix="/v1")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as http:
        yield http
    await dispose()


async def login(client: httpx.AsyncClient) -> dict[str, Any]:
    response = await client.post(
        "/v1/auth/login", json={"username": "operator", "password": "test-password"}
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_api_runs_without_admin_and_exposes_typed_openapi(client):
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    assert "example_recordRead" in schemas
    assert set(schemas["example_recordRead"]["properties"]) == {"id", "name", "payload"}
    assert "fields" not in schemas["example_recordRead"]["properties"]
    assert "/v1/records" in document["paths"]
    assert (await client.get("/v1/records")).status_code == 401
    await login(client)
    response = await client.get("/v1/records")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == [{"id": "1", "name": "visible"}]
    assert (await client.get("/v1/records/2")).status_code == 404


async def test_rest_mutations_require_csrf_etag_and_reject_unpublished_fields(client):
    identity = await login(client)
    current = await client.get("/v1/records/1")
    assert current.status_code == 200, current.text
    headers = {"X-CSRF-Token": identity["csrf_token"], "If-Match": current.headers["etag"]}
    assert (await client.patch("/v1/records/1", json={"name": "changed"})).status_code == 403
    invalid = await client.patch("/v1/records/1", headers=headers, json={"internal_note": "leak"})
    assert invalid.status_code == 422, invalid.text
    changed = await client.patch("/v1/records/1", headers=headers, json={"name": "changed"})
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"id": "1", "name": "changed"}
    assert changed.headers["etag"] != headers["If-Match"]
    stale = await client.patch("/v1/records/1", headers=headers, json={"name": "stale"})
    assert stale.status_code == 412


async def test_bearer_scope_cannot_supply_or_expand_grants(client):
    from obox_builder.contrib.auth.tokens import create_token

    await login(client)
    async with get_sessionmaker()() as db:
        token, _ = await create_token(db, 1, "Read only", ["example.record:read"])
        await db.commit()
    client.cookies.clear()
    headers = {"Authorization": f"Bearer {token}"}
    assert (await client.get("/v1/records", headers=headers)).status_code == 200
    denied = await client.patch("/v1/records/1", headers=headers, json={"name": "changed"})
    assert denied.status_code == 403, denied.text


async def test_selection_is_typed_and_authorized_before_any_write(client):
    identity = await login(client)
    headers = {"X-CSRF-Token": identity["csrf_token"]}
    path = "/v1/records/actions/renameSelection"
    rejected = await client.post(
        path, headers=headers, json={"ids": ["1", "2"], "data": {"name": "changed"}}
    )
    assert rejected.status_code == 404, rejected.text
    assert (await client.get("/v1/records/1")).json()["name"] == "visible"
    invalid = await client.post(path, headers=headers, json={"ids": ["1"], "data": {"name": ""}})
    assert invalid.status_code == 422
    changed = await client.post(
        path, headers=headers, json={"ids": ["1"], "data": {"name": "changed"}}
    )
    assert changed.status_code == 200, changed.text
    assert changed.json() == {"affected": 1}
    schema = (await client.get("/openapi.json")).json()["paths"][path]["post"]
    assert "requestBody" in schema


async def test_idempotent_patch_replays_with_original_precondition(client):
    identity = await login(client)
    initial = await client.get("/v1/records/1")
    headers = {
        "X-CSRF-Token": identity["csrf_token"],
        "If-Match": initial.headers["etag"],
        "Idempotency-Key": "rename",
    }
    first = await client.patch("/v1/records/1", headers=headers, json={"name": "changed"})
    assert first.status_code == 200, first.text
    replay = await client.patch("/v1/records/1", headers=headers, json={"name": "changed"})
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    assert replay.headers["etag"] == first.headers["etag"]
    conflict = await client.patch("/v1/records/1", headers=headers, json={"name": "other"})
    assert conflict.status_code == 422


async def test_json_boundaries_and_hidden_fields_cannot_be_queried(client, monkeypatch):
    import json

    identity = await login(client)
    headers = {"X-CSRF-Token": identity["csrf_token"]}
    wrong_type = await client.patch("/v1/records/1", headers=headers, content="{}")
    assert wrong_type.status_code == 415
    hidden = await client.get(
        "/v1/records",
        params={"filter": json.dumps({"field": "payload", "op": "eq", "value": "private"})},
    )
    assert hidden.status_code == 400, hidden.text
    monkeypatch.setattr(settings.load(), "API_MAX_BODY_BYTES", 32)
    oversized = await client.patch("/v1/records/1", headers=headers, json={"name": "x" * 40})
    assert oversized.status_code == 413


@pytest.mark.parametrize("client", [FileRecords], indirect=True)
async def test_private_upload_claims_validate_credentials_and_rollback_files(
    client, monkeypatch, tmp_path
):
    from obox_builder.contrib.auth.tokens import create_token

    monkeypatch.setattr(settings.load(), "MEDIA_ROOT", tmp_path)
    identity = await login(client)
    headers = {"X-CSRF-Token": identity["csrf_token"]}
    image = b"\x89PNG\r\n\x1a\nprivate file"
    uploaded = await client.post(
        "/v1/uploads",
        headers=headers,
        data={"resource": "example.record", "field": "payload", "id": "1"},
        files={"file": ("image.png", image, "image/png")},
    )
    assert uploaded.status_code == 201, uploaded.text
    claim = uploaded.json()
    current = await client.get("/v1/records/1")
    write_headers = {**headers, "If-Match": current.headers["etag"]}
    raw = await client.patch(
        "/v1/records/1", headers=write_headers, json={"payload": "admin/uploads/arbitrary.png"}
    )
    assert raw.status_code == 422
    async with get_sessionmaker()() as db:
        token, _ = await create_token(db, 1, "Other credential", ["example.record:*"])
        await db.commit()
    foreign = await client.patch(
        "/v1/records/1",
        headers={"Authorization": f"Bearer {token}", "If-Match": current.headers["etag"]},
        json={"payload": {"upload_token": claim["upload_token"]}},
    )
    assert foreign.status_code == 422, foreign.text
    invalid = await client.patch(
        "/v1/records/1",
        headers=write_headers,
        json={"payload": {"upload_token": claim["upload_token"]}, "name": "hidden"},
    )
    assert invalid.status_code == 422, invalid.text
    assert not list((tmp_path / "admin" / "uploads").glob("*"))
    changed = await client.patch(
        "/v1/records/1",
        headers=write_headers,
        json={"payload": {"upload_token": claim["upload_token"]}},
    )
    assert changed.status_code == 200, changed.text
    downloaded = await client.get("/v1/records/1/files/payload")
    assert downloaded.content == image
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    client.cookies.clear()
    assert (await client.get("/v1/records/1/files/payload")).status_code == 401
