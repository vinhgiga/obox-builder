from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox._auth import RBAC
from obox._config import RuntimeConfig
from obox._runtime import _Runtime
from obox.contrib.resources import Resource
from obox.contrib.resources.apis import ApiOptions
from obox.contrib.resources.views import ViewOptions
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tests.helpers import with_builders
from tests.test_api_contract import STAFF, request

DATABASE_URL = os.getenv("OBOX_CHINOOK_TEST_DATABASE_URL")
POSTGREST_URL = os.getenv("OBOX_POSTGREST_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not POSTGREST_URL,
    reason="Set OBOX_CHINOOK_TEST_DATABASE_URL and OBOX_POSTGREST_URL for PostgREST parity.",
)


class Base(DeclarativeBase):
    pass


class Artist(Base):
    __tablename__ = "artist"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    artist_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]
    albums: Mapped[list[Album]] = relationship(back_populates="artist")


class Album(Base):
    __tablename__ = "album"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    album_id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str]
    artist_id: Mapped[int] = mapped_column(sa.ForeignKey("public.artist.artist_id"))
    artist: Mapped[Artist] = relationship(back_populates="albums")


class Genre(Base):
    __tablename__ = "genre"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    genre_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]


class MediaType(Base):
    __tablename__ = "media_type"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    media_type_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]


class Track(Base):
    __tablename__ = "track"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    track_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    album_id: Mapped[int | None] = mapped_column(sa.ForeignKey("public.album.album_id"))
    media_type_id: Mapped[int] = mapped_column(sa.ForeignKey("public.media_type.media_type_id"))
    genre_id: Mapped[int | None] = mapped_column(sa.ForeignKey("public.genre.genre_id"))
    composer: Mapped[str | None]
    milliseconds: Mapped[int]
    bytes: Mapped[int | None]
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    album: Mapped[Album | None] = relationship()
    genre: Mapped[Genre | None] = relationship()


class Customer(Base):
    __tablename__ = "customer"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    customer_id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str]
    last_name: Mapped[str]
    company: Mapped[str | None]
    phone: Mapped[str | None]
    support_rep_id: Mapped[int | None] = mapped_column(
        sa.ForeignKey("public.employee.employee_id")
    )


class Employee(Base):
    __tablename__ = "employee"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    employee_id: Mapped[int] = mapped_column(primary_key=True)
    first_name: Mapped[str]
    last_name: Mapped[str]
    reports_to: Mapped[int | None] = mapped_column(
        sa.ForeignKey("public.employee.employee_id")
    )
    email: Mapped[str | None]


class Invoice(Base):
    __tablename__ = "invoice"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    invoice_id: Mapped[int] = mapped_column(primary_key=True)
    customer_id: Mapped[int] = mapped_column(sa.ForeignKey("public.customer.customer_id"))
    invoice_date: Mapped[datetime]
    total: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    billing_state: Mapped[str | None]


class InvoiceLine(Base):
    __tablename__ = "invoice_line"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    invoice_line_id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(sa.ForeignKey("public.invoice.invoice_id"))
    track_id: Mapped[int] = mapped_column(sa.ForeignKey("public.track.track_id"))
    unit_price: Mapped[Decimal] = mapped_column(sa.Numeric(10, 2))
    quantity: Mapped[int]


class Playlist(Base):
    __tablename__ = "playlist"
    __table_args__: ClassVar[dict[str, str]] = {"schema": "public"}

    playlist_id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None]


class ReadApi(ApiOptions):
    operations = ("list", "retrieve")


class ArtistView(ViewOptions):
    related = ("albums",)


class ArtistResource(Resource[Artist]):
    model = Artist
    fields = ("artist_id", "name")
    filter_fields = ("artist_id", "name")
    sort_fields = ("artist_id", "name")
    search_fields = ("name",)
    label_field = "name"
    slug = "artists"
    view_options = ArtistView
    api_options = ReadApi


class AlbumResource(Resource[Album]):
    model = Album
    fields = ("album_id", "title", "artist_id")
    filter_fields = fields
    sort_fields = fields
    search_fields = ("title",)
    label_field = "title"
    slug = "albums"
    api_options = ReadApi


class GenreResource(Resource[Genre]):
    model = Genre
    fields = ("genre_id", "name")
    filter_fields = fields
    sort_fields = fields
    search_fields = ("name",)
    label_field = "name"
    slug = "genres"
    api_options = ReadApi


class MediaTypeResource(Resource[MediaType]):
    model = MediaType
    fields = ("media_type_id", "name")
    filter_fields = fields
    sort_fields = fields
    search_fields = ("name",)
    label_field = "name"
    slug = "mediaTypes"
    api_options = ReadApi


class TrackResource(Resource[Track]):
    model = Track
    fields = (
        "track_id",
        "name",
        "album_id",
        "media_type_id",
        "genre_id",
        "composer",
        "milliseconds",
        "bytes",
        "unit_price",
    )
    filter_fields = fields
    sort_fields = fields
    search_fields = ("name", "composer")
    label_field = "name"
    slug = "tracks"
    api_options = ReadApi


class CustomerResource(Resource[Customer]):
    model = Customer
    fields = ("customer_id", "first_name", "last_name", "company", "phone", "support_rep_id")
    filter_fields = fields
    sort_fields = fields
    search_fields = ("first_name", "last_name", "company")
    label_field = "first_name"
    slug = "customers"
    api_options = ReadApi


class EmployeeResource(Resource[Employee]):
    model = Employee
    fields = ("employee_id", "first_name", "last_name", "reports_to", "email")
    filter_fields = fields
    sort_fields = fields
    search_fields = ("first_name", "last_name", "email")
    label_field = "first_name"
    slug = "employees"
    api_options = ReadApi


class InvoiceResource(Resource[Invoice]):
    model = Invoice
    fields = ("invoice_id", "customer_id", "invoice_date", "total", "billing_state")
    filter_fields = fields
    sort_fields = fields
    label_field = "invoice_id"
    slug = "invoices"
    api_options = ReadApi


class InvoiceLineResource(Resource[InvoiceLine]):
    model = InvoiceLine
    fields = ("invoice_line_id", "invoice_id", "track_id", "unit_price", "quantity")
    filter_fields = fields
    sort_fields = fields
    label_field = "invoice_line_id"
    slug = "invoiceLines"
    api_options = ReadApi


class PlaylistResource(Resource[Playlist]):
    model = Playlist
    fields = ("playlist_id", "name")
    filter_fields = fields
    sort_fields = fields
    search_fields = ("name",)
    label_field = "name"
    slug = "playlists"
    api_options = ReadApi


@asynccontextmanager
async def chinook_application() -> AsyncIterator[_Runtime]:
    if DATABASE_URL is None:
        pytest.skip("OBOX_CHINOOK_TEST_DATABASE_URL is not set.")
    engine = create_async_engine(DATABASE_URL)
    runtime = _Runtime(
        FastAPI(),
        settings=with_builders(
            RuntimeConfig(
                session_factory=async_sessionmaker(engine, expire_on_commit=False),
                principal_dependency=lambda: STAFF,
                default_policy=RBAC(),
                csrf_secret="x" * 32,
            ),
            ArtistResource,
            AlbumResource,
            GenreResource,
            MediaTypeResource,
            TrackResource,
            CustomerResource,
            EmployeeResource,
            InvoiceResource,
            InvoiceLineResource,
            PlaylistResource,
        ),
    )
    try:
        async with runtime.app.router.lifespan_context(runtime.app):
            yield runtime
    finally:
        await engine.dispose()


def oracle_record(row: dict[str, Any], primary_key: str) -> dict[str, Any]:
    result = {}
    for name, value in row.items():
        public_name = "id" if name == primary_key else to_camel(name)
        if value is not None and name.endswith("_id"):
            value = str(value)
        elif value is not None and name == "unit_price":
            value = str(Decimal(str(value)).quantize(Decimal("0.01")))
        result[public_name] = value
    return result


async def oracle_get(
    client: httpx.AsyncClient,
    resource: str,
    params: Sequence[tuple[str, str]],
    *,
    exact_count: bool = False,
) -> httpx.Response:
    headers = {"Prefer": "count=exact"} if exact_count else None
    response = await client.get(f"/{resource}", params=params, headers=headers)
    assert response.status_code in {200, 206}, response.text
    return response


def test_read_filters_match_postgrest_on_chinook() -> None:
    cases = (
        ("null", [("composer", "is.null")], [("composer", "is.null")]),
        ("not null", [("composer", "not.is.null")], [("composer", "not.is.null")]),
        ("empty string", [("composer", "eq.")], [("composer", "eq.")]),
        ("empty isdistinct", [("composer", "isdistinct.")], [("composer", "isdistinct.")]),
        ("empty IN", [("track_id", "in.()")], [("id", "in.()")]),
        ("negated empty IN", [("track_id", "not.in.()")], [("id", "not.in.()")]),
        ("numeric equality", [("unit_price", "eq.0.99")], [("unitPrice", "eq.0.99")]),
        ("numeric greater than", [("unit_price", "gt.0.99")], [("unitPrice", "gt.0.99")]),
        ("numeric range", [("unit_price", "gte.0.99")], [("unitPrice", "gte.0.99")]),
        ("numeric less than", [("unit_price", "lt.1.00")], [("unitPrice", "lt.1.00")]),
        ("numeric upper bound", [("unit_price", "lte.0.99")], [("unitPrice", "lte.0.99")]),
        ("string equality", [("name", "eq.Desafinado")], [("name", "eq.Desafinado")]),
        ("string inequality", [("name", "neq.Desafinado")], [("name", "neq.Desafinado")]),
        ("negated equality", [("name", "not.eq.Desafinado")], [("name", "not.eq.Desafinado")]),
        ("LIKE pattern", [("name", "like.*Rock*")], [("name", "like.*Rock*")]),
        ("string pattern", [("name", "ilike.*rock*")], [("name", "ilike.*rock*")]),
        (
            "distinct from value",
            [("name", "isdistinct.Desafinado")],
            [("name", "isdistinct.Desafinado")],
        ),
        (
            "IN values",
            [("track_id", "in.(1,2,3)")],
            [("id", "in.(1,2,3)")],
        ),
        (
            "OR group",
            [("or", "(track_id.eq.1,track_id.eq.2)")],
            [("or", "(id.eq.1,id.eq.2)")],
        ),
        (
            "AND group",
            [("and", "(unit_price.gte.0.99,unit_price.lt.1.00)")],
            [("and", "(unitPrice.gte.0.99,unitPrice.lt.1.00)")],
        ),
        (
            "negated OR group",
            [("not.or", "(track_id.eq.1,track_id.eq.2)")],
            [("not.or", "(id.eq.1,id.eq.2)")],
        ),
        (
            "search",
            [("or", "(name.ilike.*rock*,composer.ilike.*rock*)")],
            [("search", "rock")],
        ),
    )

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            for name, pg_filter, api_filter in cases:
                pg_response = await oracle_get(
                    oracle,
                    "track",
                    [("select", "track_id"), ("order", "track_id.asc"), ("limit", "5"), *pg_filter],
                )
                api_response = await request(
                    app,
                    "GET",
                    "/api/admin/tracks",
                    params=[("select", "id"), ("order", "id.asc"), ("limit", "5"), *api_filter],
                )
                assert api_response.status_code == 200, f"{name}: {api_response.text}"
                expected = [oracle_record(row, "track_id") for row in pg_response.json()]
                assert api_response.json()["items"] == expected, name

    asyncio.run(scenario())


def test_nullable_customer_fields_match_postgrest() -> None:
    cases = (
        ("company", "is.null"),
        ("company", "not.is.null"),
        ("company", "eq."),
        ("phone", "is.null"),
        ("support_rep_id", "not.is.null"),
    )

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            for field, expression in cases:
                pg_response = await oracle_get(
                    oracle,
                    "customer",
                    [
                        ("select", "customer_id"),
                        ("order", "customer_id.asc"),
                        ("limit", "10"),
                        (field, expression),
                    ],
                )
                api_field = to_camel(field)
                api_response = await request(
                    app,
                    "GET",
                    "/api/admin/customers",
                    params=[
                        ("select", "id"),
                        ("order", "id.asc"),
                        ("limit", "10"),
                        (api_field, expression),
                    ],
                )
                assert api_response.status_code == 200, api_response.text
                assert api_response.json()["items"] == [
                    oracle_record(row, "customer_id") for row in pg_response.json()
                ], (field, expression)

    asyncio.run(scenario())


def test_datetime_filters_match_postgrest() -> None:

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            expression = "gte.2010-01-01T00:00:00Z"
            pg_response = await oracle_get(
                oracle,
                "invoice",
                [
                    ("select", "invoice_id"),
                    ("invoice_date", expression),
                    ("order", "invoice_id.asc"),
                    ("limit", "10"),
                ],
            )
            api_response = await request(
                app,
                "GET",
                "/api/admin/invoices",
                params=[
                    ("select", "id"),
                    ("invoiceDate", expression),
                    ("order", "id.asc"),
                    ("limit", "10"),
                ],
            )
            assert api_response.status_code == 200, api_response.text
            assert api_response.json()["items"] == [
                oracle_record(row, "invoice_id") for row in pg_response.json()
            ]

    asyncio.run(scenario())


def test_collection_projection_and_detail_match_postgrest() -> None:

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            pg_list = await oracle_get(
                oracle,
                "track",
                [
                    ("select", "track_id,name,composer"),
                    ("composer", "is.null"),
                    ("order", "track_id.asc"),
                    ("limit", "3"),
                ],
            )
            api_list = await request(
                app,
                "GET",
                "/api/admin/tracks",
                params=[("select", "name,composer"), ("composer", "is.null"), ("limit", "3")],
            )
            assert api_list.status_code == 200, api_list.text
            expected_list = [oracle_record(row, "track_id") for row in pg_list.json()]
            assert api_list.json()["items"] == expected_list

            pg_detail = await oracle_get(
                oracle,
                "track",
                [
                    (
                        "select",
                        "track_id,name,album_id,media_type_id,genre_id,composer,milliseconds,bytes,unit_price",
                    ),
                    ("track_id", "eq.63"),
                ],
            )
            api_detail = await request(app, "GET", "/api/admin/tracks/63")
            assert api_detail.status_code == 200, api_detail.text
            assert api_detail.json() == oracle_record(pg_detail.json()[0], "track_id")

    asyncio.run(scenario())


def test_each_single_key_chinook_table_has_list_and_detail_reads() -> None:
    resources = (
        ("artist", "artists", "artist_id"),
        ("album", "albums", "album_id"),
        ("genre", "genres", "genre_id"),
        ("media_type", "mediaTypes", "media_type_id"),
        ("track", "tracks", "track_id"),
        ("customer", "customers", "customer_id"),
        ("employee", "employees", "employee_id"),
        ("invoice", "invoices", "invoice_id"),
        ("invoice_line", "invoiceLines", "invoice_line_id"),
        ("playlist", "playlists", "playlist_id"),
    )

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            for table, slug, primary_key in resources:
                pg_response = await oracle_get(
                    oracle,
                    table,
                    [("select", primary_key), ("order", f"{primary_key}.asc"), ("limit", "2")],
                )
                api_response = await request(
                    app,
                    "GET",
                    f"/api/admin/{slug}",
                    params=[("select", "id"), ("order", "id.asc"), ("limit", "2")],
                )
                assert api_response.status_code == 200, f"{table}: {api_response.text}"
                expected = [oracle_record(row, primary_key) for row in pg_response.json()]
                assert api_response.json()["items"] == expected, table
                if expected:
                    detail = await request(
                        app, "GET", f"/api/admin/{slug}/{expected[0]['id']}"
                    )
                    assert detail.status_code == 200, f"{table}: {detail.text}"
                    assert detail.json()["id"] == expected[0]["id"], table

    asyncio.run(scenario())


def test_query_post_pagination_count_and_null_order_match_postgrest() -> None:

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        pairs = [
            ("select", "name,composer,genreId"),
            ("composer", "is.null"),
            ("order", "composer.asc.nullsfirst,id.asc"),
            ("limit", "3"),
            ("offset", "2"),
            ("count", "exact"),
        ]
        pg_pairs = [
            ("select", "track_id,name,composer,genre_id"),
            ("composer", "is.null"),
            ("order", "composer.asc.nullsfirst,track_id.asc"),
            ("limit", "3"),
            ("offset", "2"),
        ]
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            pg_response = await oracle_get(oracle, "track", pg_pairs, exact_count=True)
            expected = [oracle_record(row, "track_id") for row in pg_response.json()]
            total = int(pg_response.headers["content-range"].rsplit("/", 1)[1])

            get_response = await request(app, "GET", "/api/admin/tracks", params=pairs)
            post_response = await request(
                app, "POST", "/api/admin/tracks:query", json={"query": pairs}
            )
            for response in (get_response, post_response):
                assert response.status_code == 200, response.text
                assert response.json()["items"] == expected
                page = response.json()["page"]
                assert {key: value for key, value in page.items() if key != "nextCursor"} == {
                    "limit": 3,
                    "offset": 2,
                    "hasMore": True,
                    "total": total,
                }
                assert page["nextCursor"]

            pg_next = await oracle_get(
                oracle,
                "track",
                [
                    ("select", "track_id,name,composer,genre_id"),
                    ("composer", "is.null"),
                    ("order", "composer.asc.nullsfirst,track_id.asc"),
                    ("limit", "3"),
                    ("offset", "5"),
                ],
            )
            cursor_response = await request(
                app,
                "GET",
                "/api/admin/tracks",
                params=[
                    ("select", "name,composer,genreId"),
                    ("composer", "is.null"),
                    ("order", "composer.asc.nullsfirst,id.asc"),
                    ("limit", "3"),
                    ("cursor", get_response.json()["page"]["nextCursor"]),
                ],
            )
            assert cursor_response.status_code == 200, cursor_response.text
            assert cursor_response.json()["items"] == [
                oracle_record(row, "track_id") for row in pg_next.json()
            ]

    asyncio.run(scenario())


def test_all_null_order_modifiers_match_postgrest() -> None:
    orders = (
        ("composer.asc.nullslast,track_id.asc", "composer.asc.nullslast,id.asc"),
        ("composer.desc.nullsfirst,track_id.asc", "composer.desc.nullsfirst,id.asc"),
        ("composer.desc.nullslast,track_id.asc", "composer.desc.nullslast,id.asc"),
    )

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            for pg_order, api_order in orders:
                pg_response = await oracle_get(
                    oracle,
                    "track",
                    [
                        ("select", "track_id"),
                        ("order", pg_order),
                        ("limit", "10"),
                    ],
                )
                api_response = await request(
                    app,
                    "GET",
                    "/api/admin/tracks",
                    params=[("select", "id"), ("order", api_order), ("limit", "10")],
                )
                assert api_response.status_code == 200, api_response.text
                assert api_response.json()["items"] == [
                    oracle_record(row, "track_id") for row in pg_response.json()
                ], api_order

    asyncio.run(scenario())


def test_query_options_and_resource_descriptions_cover_read_surfaces() -> None:

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            pg_response = await oracle_get(
                oracle,
                "album",
                [
                    ("select", "album_id,title"),
                    ("artist_id", "eq.1"),
                    ("order", "album_id.asc"),
                    ("limit", "3"),
                ],
                exact_count=True,
            )
            options = await request(
                app,
                "GET",
                "/api/admin/albums:queryOptions",
                params=[
                    ("artistId", "eq.1"),
                    ("order", "id.asc"),
                    ("limit", "3"),
                    ("count", "exact"),
                ],
            )
            assert options.status_code == 200, options.text
            assert options.json()["items"] == [
                {"id": str(row["album_id"]), "label": row["title"]}
                for row in pg_response.json()
            ]
            assert options.json()["page"]["total"] == int(
                pg_response.headers["content-range"].rsplit("/", 1)[1]
            )

            detail = await request(app, "GET", "/api/admin/tracks:describe")
            assert detail.status_code == 200, detail.text
            assert detail.json()["slug"] == "admin/tracks"
            assert {field["name"] for field in detail.json()["fields"]} >= {
                "id",
                "composer",
                "genreId",
            }
            listing = await request(app, "GET", "/api:describe")
            assert listing.status_code == 200, listing.text
            assert {item["slug"] for item in listing.json()["resources"]} == {
                "admin/artists",
                "admin/albums",
                "admin/genres",
                "admin/mediaTypes",
                "admin/tracks",
                "admin/customers",
                "admin/employees",
                "admin/invoices",
                "admin/invoiceLines",
                "admin/playlists",
            }

    asyncio.run(scenario())


def test_embedded_and_related_reads_match_postgrest_relationships() -> None:

    async def scenario() -> None:
        if POSTGREST_URL is None:
            pytest.skip("OBOX_POSTGREST_URL is not set.")
        async with chinook_application() as app, httpx.AsyncClient(
            base_url=POSTGREST_URL
        ) as oracle:
            pg_track = await oracle_get(
                oracle,
                "track",
                [
                    ("select", "track_id,album_id,album:album(album_id,title)"),
                    ("track_id", "eq.1"),
                ],
            )
            expanded = await request(
                app,
                "GET",
                "/api/admin/tracks",
                params=[("id", "eq.1"), ("select", "albumId"), ("expand", "album")],
            )
            assert expanded.status_code == 200, expanded.text
            pg_row = pg_track.json()[0]
            api_row = expanded.json()["items"][0]
            assert api_row["id"] == str(pg_row["track_id"])
            assert api_row["albumId"] == str(pg_row["album_id"])
            assert api_row["expanded"]["album"] == {
                "id": str(pg_row["album"]["album_id"]),
                "label": pg_row["album"]["title"],
            }

            pg_albums = await oracle_get(
                oracle,
                "album",
                [
                    ("select", "album_id,title,artist_id"),
                    ("order", "artist_id.asc,album_id.asc"),
                ],
            )
            albums_by_artist: dict[int, list[dict[str, Any]]] = {}
            for row in pg_albums.json():
                albums_by_artist.setdefault(row["artist_id"], []).append(row)
            artist_id, expected_albums = next(
                (key, rows) for key, rows in albums_by_artist.items() if len(rows) > 1
            )
            first_page = await request(
                app,
                "GET",
                f"/api/admin/artists/{artist_id}/relationships/albums",
                params={"limit": "1"},
            )
            assert first_page.status_code == 200, first_page.text
            assert [(item["id"], item["label"]) for item in first_page.json()["items"]] == [
                (str(expected_albums[0]["album_id"]), expected_albums[0]["title"])
            ]
            assert first_page.json()["page"]["hasMore"] is True
            cursor = first_page.json()["page"]["nextCursor"]
            assert cursor
            next_page = await request(
                app,
                "GET",
                f"/api/admin/artists/{artist_id}/relationships/albums",
                params={"limit": "1", "cursor": cursor},
            )
            assert next_page.status_code == 200, next_page.text
            assert [(item["id"], item["label"]) for item in next_page.json()["items"]] == [
                (str(expected_albums[1]["album_id"]), expected_albums[1]["title"])
            ]

    asyncio.run(scenario())
