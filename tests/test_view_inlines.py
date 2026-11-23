import asyncio
import re
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI
from obox import Obox, Resource, permission_required
from obox._auth import Principal
from obox._exceptions import ResourceConflict
from obox.db import _session
from obox.views.generic import CreateView, StackedInline, TabularInline, UpdateView

from tests.test_forms_integration import configured
from tests.test_formsets import Author, Book, data, seed
from tests.test_registered_views import Actors


@pytest.mark.parametrize(("layout", "direct"), [(TabularInline, False), (StackedInline, True)])
def test_view_inlines_save_parent_children_and_render_errors(layout: Any, direct: bool) -> None:
    class Books(TabularInline):
        template = layout.template
        model = Book
        fields = ("title",)
        extra = 1

    class AuthorCreate(CreateView):
        fields = ("name",)
        inlines = (Books,)

        async def save_formset(self, request: Any, form: Any, formset: Any, change: bool) -> None:
            await super().save_formset(request, form, formset, change)
            if form.instance.name == "Roll back":
                raise ResourceConflict("Cannot save this group.")

    class AuthorUpdate(UpdateView):
        fields = AuthorCreate.fields
        inlines = AuthorCreate.inlines

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_superuser=True),
            )
            obox = Obox(app)
            if direct:
                for path, view, operation in (
                    ("/authors/add", AuthorCreate, "add"),
                    ("/authors/{pk}/change", AuthorUpdate, "change"),
                ):
                    app.add_api_route(
                        path,
                        view.as_view(resource=Resource(model=Author), success_url="/authors"),
                        methods=["GET", "POST"],
                        dependencies=[
                            Depends(permission_required(f"tests.{operation}_author", html=True))
                        ],
                    )
            else:
                obox.register(Author, create_view=AuthorCreate, update_view=AuthorUpdate, api=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get("/authors/add")
                assert page.status_code == 200, page.text
                assert 'name="books-TOTAL_FORMS"' in page.text
                matched = re.search('name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert matched is not None
                token = matched[1]
                parent = {"name": "New parent", "csrfmiddlewaretoken": token}
                invalid = await client.post(
                    "/authors/add",
                    data={**parent, **data({"title": ""}, {"title": "x" * 50})},
                )
                assert invalid.status_code == 200
                assert "at most 40 characters" in invalid.text
                async with _session() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Author)) == 0
                failed = await client.post(
                    "/authors/add",
                    data={
                        **parent,
                        "name": "Roll back",
                        **data({"title": "Will roll back"}, {}),
                    },
                )
                assert failed.status_code == 200
                assert "Cannot save this group." in failed.text
                async with _session() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Author)) == 0
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Book)) == 0
                saved = await client.post(
                    "/authors/add", data={**parent, **data({"title": "First"}, {})}
                )
                assert saved.status_code == 303, saved.text
                async with _session() as session:
                    author_id = await session.scalar(sa.select(Author.id))
                    book_id = await session.scalar(sa.select(Book.id))
                changed = await client.post(
                    f"/authors/{author_id}/change",
                    data={
                        **parent,
                        **data(
                            {"id": str(book_id), "title": "Changed"},
                            {"title": "Second"},
                            initial=1,
                        ),
                    },
                )
                assert changed.status_code == 303, changed.text
                async with _session() as session:
                    assert list(await session.scalars(sa.select(Book.title).order_by(Book.id))) == [
                        "Changed",
                        "Second",
                    ]

    asyncio.run(scenario())


def test_view_rejects_inline_creation_without_child_add_permission() -> None:
    class Books(TabularInline):
        model = Book
        fields = ("title",)

    class AuthorUpdate(UpdateView):
        fields = ("name",)
        inlines = (Books,)

    async def scenario() -> None:
        async with configured():
            author = await seed()
            actor = Principal(
                id=1,
                is_authenticated=True,
                permissions=frozenset(
                    {"tests.view_author", "tests.change_author", "tests.view_book"}
                ),
            )
            app = FastAPI(openapi_url=None)
            app.add_middleware(Actors, actor=actor)
            Obox(app).register(Author, update_view=AuthorUpdate, api=False)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get(f"/authors/{author.id}/change")
                assert page.status_code == 200
                matched = re.search('name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert matched is not None
                token = matched[1]
                forged = await client.post(
                    f"/authors/{author.id}/change",
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
