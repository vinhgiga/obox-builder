import asyncio
import re
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox import Obox
from obox._auth import Principal
from obox.builder import AdminSite, ModelBuilder, StackedInline, TabularInline
from obox.db import _session
from obox.urls import _mount, path

from tests.test_builder import Actors
from tests.test_forms_integration import configured
from tests.test_formsets import Author, Book, data, seed


@pytest.mark.parametrize("layout", [TabularInline, StackedInline])
def test_builder_inlines_save_parent_children_and_render_errors(layout: Any) -> None:
    class Books(TabularInline):
        template = layout.template
        model = Book
        fields = ("title",)
        extra = 1

    class Authors(ModelBuilder):
        fields = ("name",)
        inlines = (Books,)

    site = AdminSite()
    site.register(Author, Authors)

    async def scenario() -> None:
        async with configured():
            app = FastAPI()
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_staff=True, is_superuser=True),
            )
            Obox(app)
            _mount(app, [path("manage/", site.urls)])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/manage/tests/author/add/")
                assert page.status_code == 200, page.text
                assert 'name="books-TOTAL_FORMS"' in page.text
                matched = re.search('name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert matched is not None
                token = matched[1]
                parent = {"name": "New parent", "csrfmiddlewaretoken": token}
                invalid = await client.post(
                    "/manage/tests/author/add/",
                    data={**parent, **data({"title": ""}, {"title": "x" * 50})},
                )
                assert invalid.status_code == 200
                assert "at most 40 characters" in invalid.text
                async with _session() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Author)) == 0
                saved = await client.post(
                    "/manage/tests/author/add/", data={**parent, **data({"title": "First"}, {})}
                )
                assert saved.status_code == 302, saved.text
                changed = await client.post(
                    "/manage/tests/author/1/change/",
                    data={
                        **parent,
                        **data({"id": "1", "title": "Changed"}, {"title": "Second"}, initial=1),
                    },
                )
                assert changed.status_code == 302, changed.text
                async with _session() as session:
                    assert list(await session.scalars(sa.select(Book.title).order_by(Book.id))) == [
                        "Changed",
                        "Second",
                    ]

    asyncio.run(scenario())


def test_builder_rejects_inline_creation_without_child_add_permission() -> None:
    class Books(TabularInline):
        model = Book
        fields = ("title",)

    class Authors(ModelBuilder):
        fields = ("name",)
        inlines = (Books,)

    site = AdminSite()
    site.register(Author, Authors)

    async def scenario() -> None:
        async with configured():
            author = await seed()
            actor = Principal(
                id=1,
                is_authenticated=True,
                is_staff=True,
                permissions=frozenset(
                    {"tests.view_author", "tests.change_author", "tests.view_book"}
                ),
            )
            app = FastAPI()
            app.add_middleware(Actors, actor=actor)
            Obox(app)
            _mount(app, [path("manage/", site.urls)])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get(f"/manage/tests/author/{author.id}/change/")
                assert page.status_code == 200
                matched = re.search('name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert matched is not None
                token = matched[1]
                forged = await client.post(
                    f"/manage/tests/author/{author.id}/change/",
                    data={
                        "name": "Forged parent",
                        "csrfmiddlewaretoken": token,
                        **data(
                            {"id": "1", "title": "First"},
                            {"id": "2", "title": "Second"},
                            {"title": "Forged child"},
                            initial=2,
                        ),
                    },
                )
                assert forged.status_code == 403
                async with _session() as session:
                    assert await session.scalar(sa.select(Author.name)) == "One"
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Book)) == 2

    asyncio.run(scenario())
