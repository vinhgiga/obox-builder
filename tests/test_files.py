from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from obox_builder import ModelBuilder, Obox, Principal, Settings
from obox_builder.auth import RBAC
from obox_builder.contrib.files import IMAGES, File, Files, attachments
from obox_builder.contrib.files.models import Base as FileBase
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "file_documents"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(50))
    attachments: Mapped[list[File]] = attachments(types=IMAGES)


class DocumentBuilder(ModelBuilder[Document]):
    fields = ("id", "name", "attachments")


PNG = b"\x89PNG\r\n\x1a\ncontent"
FILE_FIELDS = {"id", "filename", "size", "contentType", "offset", "status"}


@asynccontextmanager
async def application(tmp_path: Path) -> AsyncIterator[tuple[Obox, httpx.AsyncClient]]:
    def principal(request: Request) -> Principal:
        return Principal(
            id=request.headers.get("actor", "1"),
            is_authenticated=True,
            is_staff=True,
            is_superuser=True,
        )

    app = Obox(
        FastAPI(),
        settings=Settings(
            database_url=f"sqlite+aiosqlite:///{tmp_path / 'files.sqlite3'}",
            principal_dependency=principal,
            default_policy=RBAC(),
            csrf_secret="x" * 32,
        ),
        plugins=[Files(root=tmp_path / "media")],
    )
    app.register(DocumentBuilder)
    async with app.app.router.lifespan_context(app.app):
        assert app.database.engine is not None
        async with app.database.engine.begin() as connection:
            await connection.run_sync(FileBase.metadata.create_all)
            await connection.run_sync(Base.metadata.create_all)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app.app),
            base_url="http://test",
            follow_redirects=True,
        ) as client:
            yield app, client


def test_file_json_contract_across_upload_resume_claim_and_widget(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client):
            created = await client.post(
                "/files",
                json={
                    "filename": "sample.png",
                    "size": len(PNG),
                    "field": "file_documents.attachments",
                    "contentType": "image/png",
                    "ownerId": None,
                },
            )
            assert created.status_code == 201, created.text
            upload = created.json()
            assert set(upload) == FILE_FIELDS | {"chunkSize"}
            assert upload["chunkSize"] == 8 * 1024**2
            assert upload["contentType"] == "image/png"
            path = f"/files/{upload['id']}"
            headers = {"content-type": "application/offset+octet-stream", "upload-offset": "0"}
            partial = await client.patch(path, headers=headers, content=PNG[:8])
            assert partial.status_code == 200
            assert set(partial.json()) == FILE_FIELDS
            assert partial.json()["offset"] == 8
            assert partial.json()["status"] == "pending"

            conflict = await client.patch(path, headers=headers, content=PNG[:8])
            assert conflict.status_code == 409
            assert conflict.json()["code"] == "resource_conflict"
            assert conflict.headers["content-type"] == "application/problem+json"
            state = await client.get(path + "/state")
            assert state.status_code == 200
            assert state.json() == partial.json()
            assert state.headers["cache-control"] == "private, no-store"
            assert (await client.get(path + "/state", headers={"actor": "2"})).status_code == 404

            ready = await client.patch(
                path, headers={**headers, "upload-offset": "8"}, content=PNG[8:]
            )
            assert ready.status_code == 200
            assert set(ready.json()) == FILE_FIELDS
            assert ready.json()["status"] == "ready"
            assert ready.json()["offset"] == len(PNG)
            renamed = await client.put(path, json={"filename": "renamed.png"})
            assert renamed.status_code == 200
            assert set(renamed.json()) == FILE_FIELDS
            assert renamed.json()["filename"] == "renamed.png"

            record = await client.post(
                "/api/documents", json={"name": "Example", "attachments": [upload["id"]]}
            )
            assert record.status_code == 201, record.text
            record_id = record.json()["id"]
            reordered = await client.put(
                f"/files/{record_id}/order",
                json={"field": "file_documents.attachments", "ids": [upload["id"]]},
            )
            assert reordered.status_code == 200
            assert reordered.json() == {"ids": [upload["id"]]}
            download = await client.get(path)
            assert download.status_code == 200
            assert download.content == PNG
            assert download.headers["content-type"] == "image/png"

            page = await client.get(f"/admin/documents/{record_id}")
            assert page.status_code == 200
            match = re.search(r'data-files-meta="([^"]+)"', page.text)
            assert match is not None
            assert json.loads(html.unescape(match[1])) == [renamed.json()]
            owned_upload = await client.post(
                "/files",
                json={
                    "filename": "next.png",
                    "size": len(PNG),
                    "field": "file_documents.attachments",
                    "ownerId": record_id,
                },
            )
            assert owned_upload.status_code == 201
            missing_owner = await client.post(
                "/files",
                json={
                    "filename": "next.png",
                    "size": len(PNG),
                    "field": "file_documents.attachments",
                    "ownerId": "99999",
                },
            )
            assert missing_owner.status_code == 404

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["content_type", "owner_id"])
def test_file_requests_reject_python_field_names(tmp_path: Path, field: str) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client):
            response = await client.post(
                "/files",
                json={
                    "filename": "sample.png",
                    "size": len(PNG),
                    "field": "file_documents.attachments",
                    field: None if field == "owner_id" else "image/png",
                },
            )
            assert response.status_code == 422

    asyncio.run(scenario())


def test_file_openapi_matches_json_properties(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, _):
            schema = app.app.openapi()
            models = schema["components"]["schemas"]
            assert set(models["NewFile"]["properties"]) == {
                "filename",
                "size",
                "field",
                "contentType",
                "ownerId",
            }
            assert set(models["FileMetadata"]["properties"]) == FILE_FIELDS
            assert set(models["UploadCreated"]["properties"]) == FILE_FIELDS | {"chunkSize"}
            for path, method, status, model in (
                ("/files", "post", "201", "UploadCreated"),
                ("/files/{fid}/state", "get", "200", "FileMetadata"),
                ("/files/{fid}", "patch", "200", "FileMetadata"),
                ("/files/{fid}", "put", "200", "FileMetadata"),
            ):
                response = schema["paths"][path][method]["responses"][status]
                assert response["content"]["application/json"]["schema"] == {
                    "$ref": f"#/components/schemas/{model}"
                }

    asyncio.run(scenario())


def test_failed_upload_returns_problem_and_preserves_state(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client):
            created = await client.post(
                "/files",
                json={"filename": "invalid.png", "size": 3, "field": "file_documents.attachments"},
            )
            assert created.status_code == 201
            path = f"/files/{created.json()['id']}"
            failed = await client.patch(
                path,
                headers={"content-type": "application/offset+octet-stream", "upload-offset": "0"},
                content=b"bad",
            )
            assert failed.status_code == 409
            assert failed.json()["code"] == "resource_conflict"
            state = (await client.get(path + "/state")).json()
            assert set(state) == FILE_FIELDS
            assert state["contentType"] == "image/png"
            assert state["status"] == "failed"

    asyncio.run(scenario())


def test_file_protocol_headers_and_problems(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client):
            created = await client.post(
                "/files",
                json={
                    "filename": "test.png",
                    "size": len(PNG),
                    "field": "file_documents.attachments",
                },
            )
            path = f"/files/{created.json()['id']}"
            ready = await client.patch(
                path,
                headers={"content-type": "application/offset+octet-stream", "upload-offset": "0"},
                content=PNG,
            )
            assert ready.status_code == 200
            for url in (path, path + "/state"):
                get = await client.get(url)
                head = await client.head(url)
                assert get.status_code == head.status_code == 200
                assert head.content == b""
                for name in ("content-type", "content-length", "cache-control", "etag"):
                    assert head.headers.get(name) == get.headers.get(name)
                denied = await client.head(url, headers={"actor": "2"})
                assert denied.status_code == 404
                assert denied.content == b""
            options = await client.options(path)
            assert options.status_code == 204
            assert set(options.headers["allow"].split(", ")) == {
                "GET",
                "HEAD",
                "PATCH",
                "PUT",
                "OPTIONS",
            }
            invalid = await client.post("/files", json={"filename": "bad"})
            assert invalid.status_code == 422
            problem = invalid.json()
            assert problem["traceId"] == invalid.headers["x-request-id"]
            assert invalid.headers["content-type"] == "application/problem+json"
            assert invalid.headers["cache-control"] == "private, no-store"
            assert problem["errors"][0]["pointer"].startswith("#/")
            schema = app.app.openapi()
            assert (
                "application/problem+json"
                in schema["paths"]["/files"]["post"]["responses"]["422"]["content"]
            )

    asyncio.run(scenario())
