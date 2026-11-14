from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox_builder import Context, ModelBuilder, Obox, Principal, Settings
from obox_builder.auth import RBAC
from obox_builder.contrib.activity import Activity
from obox_builder.contrib.activity.models import Base as ActivityBase
from obox_builder.contrib.activity.models import Comment
from obox_builder.contrib.files import File, Files, attachments
from obox_builder.contrib.files.models import Base as FileBase
from obox_builder.contrib.webhooks import Webhooks
from obox_builder.contrib.webhooks.models import Base as WebhookBase
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class State(StrEnum):
    DRAFT = "draft"
    LIVE = "live"


class Record(Base):
    __tablename__ = "metadata_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(40))
    state: Mapped[State] = mapped_column(sa.Enum(State), default=State.DRAFT)
    amount: Mapped[Decimal] = mapped_column(sa.Numeric(8, 2), default=Decimal(0))
    quantity: Mapped[int] = mapped_column(default=0)
    doubled: Mapped[int] = mapped_column(sa.Computed("quantity * 2"))
    moment: Mapped[datetime | None]
    files: Mapped[list[File]] = attachments(max_size=1024, max_count=3)


class Plain(Base):
    __tablename__ = "metadata_plain"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]


class Document(Base):
    __tablename__ = "metadata_documents"
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
    name: Mapped[str]
    files: Mapped[list[File]] = attachments()


STAFF = Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True)


class Policy(RBAC[Any]):
    hide_files = False
    hide_pk = False
    deny_comments = False

    def scope(self, action: str, ctx: Context) -> Any:
        return Record.name != "hidden"

    async def allow(self, action: str, ctx: Context, *, obj: Any = None) -> bool:
        return (
            not (action == "comment" and self.deny_comments)
            and (obj is None or obj.name != "denied")
            and await super().allow(action, ctx, obj=obj)
        )

    def read_fields(self, ctx: Context, *, obj: Any = None) -> frozenset[str] | None:
        return (
            frozenset({"name"} if self.hide_pk else {"id", "name"})
            if self.hide_files or self.hide_pk
            else None
        )


@asynccontextmanager
async def application(
    path: Path, *, plugins: bool = True, max_batch: int | None = None, webhooks: bool = False
) -> AsyncIterator[tuple[Obox, httpx.AsyncClient, Policy, Any]]:
    engine = create_async_engine(
        os.getenv("OBOX_TEST_DATABASE_URL", f"sqlite+aiosqlite:///{path / 'metadata.sqlite3'}")
    )
    namespace = "metadata_" + uuid4().hex if engine.dialect.name == "postgresql" else None
    if namespace:
        async with engine.begin() as conn:
            await conn.execute(sa.schema.CreateSchema(namespace))
        engine = engine.execution_options(schema_translate_map={None: namespace})
    async with engine.begin() as conn:
        for metadata in (
            Base.metadata,
            FileBase.metadata,
            ActivityBase.metadata,
            WebhookBase.metadata,
        ):
            await conn.run_sync(metadata.create_all)
    policy = Policy()

    class Resource(ModelBuilder[Record]):
        slug = "records"
        fields = ("id", "name", "state", "amount", "quantity", "doubled", "moment", "files")

    app = Obox(
        FastAPI(),
        settings=Settings(
            session_factory=async_sessionmaker(engine, expire_on_commit=False),
            principal_dependency=lambda: STAFF,
            default_policy=policy,
            csrf_secret="x" * 32,
            api_max_batch=max_batch,
        ),
        plugins=(
            [
                Files(root=path / "media"),
                Activity(),
                *(
                    [
                        Webhooks(
                            signing_keys={"v1": b"k" * 32},
                            active_key="v1",
                            resolve_principal=lambda *_: STAFF,
                        )
                    ]
                    if webhooks
                    else []
                ),
            ]
            if plugins
            else []
        ),
    )

    class PlainResource(ModelBuilder[Plain]):
        slug = "records"
        fields = ("id", "name")

    class DocumentResource(ModelBuilder[Document]):
        slug = "documents"
        fields = ("id", "name", "files")
        policy = RBAC[Document]()

    try:
        app.register(Resource if plugins else PlainResource)
        if plugins:
            app.register(DocumentResource)
        policy = cast(Policy, app.get_builder(Record if plugins else Plain).policy)
        async with (
            app.app.router.lifespan_context(app.app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as client,
        ):
            yield app, client, policy, engine
    finally:
        if namespace:
            async with engine.begin() as conn:
                await conn.execute(sa.schema.DropSchema(namespace, cascade=True))
        await engine.dispose()


def test_describe_field_kinds_preserve_schema_and_policy(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (_, client, policy, _):
            response = await client.get("/api/records:describe")
            assert response.status_code == 200, response.text
            body = response.json()
            fields = {item["name"]: item for item in body["fields"]}
            assert fields["state"]["kind"] == "select"
            assert fields["state"]["options"]["enum"] == ["draft", "live"]
            assert fields["amount"]["kind"] == "decimal"
            assert fields["amount"]["options"]["precision"] == 8
            assert fields["amount"]["options"]["scale"] == 2
            assert fields["moment"]["kind"] == "datetime"
            assert fields["moment"]["nullable"] is True
            assert fields["doubled"]["computed"] is True
            assert fields["doubled"]["readOnly"] is True
            assert fields["id"]["computed"] is False
            assert fields["files"]["kind"] == "attachment"
            assert fields["files"]["options"]["maxSize"] == 1024
            assert fields["files"]["options"]["maxCount"] == 3
            assert body["capabilities"] == {
                "attachments": True,
                "comments": True,
                "webhooks": False,
            }
            assert "$defs" in fields["state"]["schema"]
            policy.hide_files = policy.deny_comments = True
            body = (await client.get("/api/records:describe")).json()
            assert body["capabilities"]["attachments"] is False
            assert body["capabilities"]["comments"] is False

    asyncio.run(scenario())


def test_plugins_share_routes_and_generate_concrete_metadata_schemas(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, webhooks=True) as (app, client, _, _):
            assert not any(item.level == "error" for item in app.check(strict=True))
            description = (await client.get("/api/records:describe")).json()
            assert description["capabilities"] == {
                "attachments": True,
                "comments": True,
                "webhooks": True,
            }
            result = await client.post(
                "/api/webhooks", json={"resource": "records", "url": "https://example.com/events"}
            )
            assert result.status_code == 201, result.text
            schema = (await client.get("/openapi.json")).json()
            assert schema["paths"]["/api/webhooks"]["post"]["operationId"] == "webhooks_create"
            assert (
                schema["components"]["schemas"]["ChangeNotification"]["properties"]["recordId"][
                    "type"
                ]
                == "string"
            )
            assert "MetadataResults" in schema["components"]["schemas"]

    asyncio.run(scenario())


async def seed(app: Obox, count: int = 2) -> list[str]:
    async with app.uow(principal=STAFF) as ctx:
        rows = [Record(name=f"row-{index}") for index in range(count)]
        ctx.session.add_all(rows)
        await ctx.session.flush()
        for row in rows:
            ctx.session.add(
                Comment(
                    resource_key=Record.__tablename__,
                    record_id=str(row.id),
                    author_id="1",
                    body="comment",
                )
            )
        ctx.session.add(
            File(
                key="one",
                filename="one.txt",
                size=1,
                offset=1,
                status="ready",
                content_type="text/plain",
                owner_type=Record.__tablename__,
                field="files",
                owner_id=str(rows[0].id),
                created_by="1",
            )
        )
        return [str(row.id) for row in rows]


def test_metadata_batches_are_ordered_scoped_and_do_not_change_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, policy, engine):
            ids = await seed(app, 10)
            statements: list[str] = []

            def capture(
                conn: Any, cursor: Any, statement: str, parameters: Any, context: Any, many: bool
            ) -> None:
                statements.append(statement)

            sa.event.listen(engine.sync_engine, "before_cursor_execute", capture)
            counts = []
            for keys in (ids[:1], ids[::-1]):
                statements.clear()
                response = await client.post(
                    "/api/records:queryMetadata",
                    json={"recordIds": keys, "include": ["attachments", "commentCount"]},
                )
                assert response.status_code == 200, response.text
                body = response.json()
                counts.append(len(statements))
                assert [row["id"] for row in body["items"]] == keys
                assert all(row["commentCount"] == 1 for row in body["items"])
                files = next(row for row in body["items"] if row["id"] == ids[0])["attachments"][
                    "files"
                ]
                assert files[0]["filename"] == "one.txt"
                assert files[0]["downloadUrl"] == f"/files/{files[0]['id']}"
                assert set(files[0]) == {
                    "id",
                    "filename",
                    "size",
                    "offset",
                    "contentType",
                    "status",
                    "downloadUrl",
                }
                assert response.headers["cache-control"] == "private, no-store"
            sa.event.remove(engine.sync_engine, "before_cursor_execute", capture)
            assert counts[0] == counts[1]
            record = (await client.get(f"/api/records/{ids[0]}")).json()
            assert record["files"] == [files[0]["id"]]
            assert "commentCount" not in record
            policy.hide_files = True
            response = await client.post(
                "/api/records:queryMetadata", json={"recordIds": ids, "include": ["attachments"]}
            )
            assert all(row["attachments"] == {} for row in response.json()["items"])
            policy.hide_pk = True
            assert (
                await client.post(
                    "/api/records:queryMetadata",
                    json={"recordIds": ids, "include": ["attachments"]},
                )
            ).status_code == 403

    asyncio.run(scenario())


def test_attachment_metadata_uses_database_owner_keys_for_uuid_records(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, _, _):
            async with app.uow(principal=STAFF) as ctx:
                file = File(
                    key="uuid",
                    filename="uuid.txt",
                    size=1,
                    offset=1,
                    status="ready",
                    content_type="text/plain",
                    owner_type=Document.__tablename__,
                    field="files",
                    created_by="1",
                )
                ctx.session.add(file)
                await ctx.session.flush()
                file_id = str(file.id)
            document = await client.post(
                "/api/documents", json={"name": "uuid", "files": [file_id]}
            )
            assert document.status_code == 201, document.text
            response = await client.post(
                "/api/documents:queryMetadata",
                json={"recordIds": [document.json()["id"]], "include": ["attachments"]},
            )
            assert response.status_code == 200, response.text
            assert response.json()["items"][0]["attachments"]["files"][0]["id"] == file_id

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["hidden", "denied", "comments"])
def test_metadata_rechecks_row_and_comment_permissions(tmp_path: Path, kind: str) -> None:
    async def scenario() -> None:
        async with application(tmp_path) as (app, client, policy, _):
            ids = await seed(app)
            async with app.uow(principal=STAFF) as ctx:
                if kind != "comments":
                    await ctx.session.execute(
                        sa.update(Record).where(Record.id == int(ids[1])).values(name=kind)
                    )
                else:
                    policy.deny_comments = True
            result = await client.post(
                "/api/records:queryMetadata", json={"recordIds": ids, "include": ["commentCount"]}
            )
            assert result.status_code == (404 if kind == "hidden" else 403)
            assert "items" not in result.json()

    asyncio.run(scenario())


def test_metadata_input_limits_soft_deleted_comments_and_absent_plugins(tmp_path: Path) -> None:
    async def scenario() -> None:
        async with application(tmp_path, max_batch=2) as (app, client, _, _):
            ids = await seed(app)
            for body in (
                {"recordIds": [], "include": ["attachments"]},
                {"recordIds": ids * 2, "include": ["attachments"]},
                {"recordIds": [ids[0], "0" + ids[0]], "include": ["attachments"]},
                {"recordIds": ids, "include": ["attachments", "attachments"]},
                {"recordIds": ids, "include": ["unknown"]},
                {"record_ids": ids, "include": ["attachments"]},
            ):
                assert (
                    await client.post("/api/records:queryMetadata", json=body)
                ).status_code == 422
            async with app.uow(principal=STAFF) as ctx:
                await ctx.session.execute(
                    sa.update(Comment).values(body=None, deleted_at=sa.func.now())
                )
            result = await client.post(
                "/api/records:queryMetadata", json={"recordIds": ids, "include": ["commentCount"]}
            )
            assert result.status_code == 200, result.text
            assert result.json()["items"] == [{"id": key, "commentCount": 0} for key in ids]
            assert (
                await client.post(
                    "/api/records:queryMetadata?offset=0",
                    json={"recordIds": ids, "include": ["attachments"]},
                )
            ).status_code == 422
        async with application(tmp_path, plugins=False) as (_, client, _, _):
            assert (await client.post("/api/records:queryMetadata", json={})).status_code == 404

    asyncio.run(scenario())
