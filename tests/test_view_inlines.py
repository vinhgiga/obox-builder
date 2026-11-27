import asyncio
from importlib import import_module
from typing import Any, ClassVar

import httpx
import pytest
import sqlalchemy as sa
from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from obox import ModelResource, Obox, permission_required
from obox.apps import AppConfig
from obox.conf import settings
from obox.core.exceptions import ResourceConflict
from obox.core.principal import Principal
from obox.db.session import session_scope
from obox.middleware.csrf import get_token
from obox.views.generic import CreateView, ModelViewSet, StackedInline, TabularInline, UpdateView

from tests.runtime import resource_for
from tests.test_forms_integration import configured
from tests.test_formsets import Author, Book, data, seed
from tests.test_registered_views import Actors


class InlineViewTestMixin:
    request: Request

    def render_to_response(self, context: dict[str, Any], **kwargs: Any) -> HTMLResponse:
        token = get_token(self.request)
        errors = [context["form"].errors]
        errors.extend(group["formset"].errors for group in context.get("inline_formsets", []))
        return HTMLResponse(
            f'<input name="csrfmiddlewaretoken" value="{token}">{errors}',
            status_code=kwargs.get("status_code", 200),
        )


@pytest.mark.parametrize(("layout", "direct"), [(TabularInline, False), (StackedInline, True)])
def test_view_inlines_save_parent_children_and_error_statuses(layout: Any, direct: bool) -> None:
    class Books(TabularInline):
        template = layout.template
        resource = resource_for(Book, fields=("title",))
        fields = ("title",)
        extra = 1

    class AuthorCreate(InlineViewTestMixin, CreateView):
        fields = ("name",)
        inlines = (Books,)

        async def save_formset(self, request: Any, form: Any, formset: Any, change: bool) -> None:
            await super().save_formset(request, form, formset, change)
            if form.instance.name == "Roll back":
                raise ResourceConflict("Cannot save this group.")

    class AuthorUpdate(InlineViewTestMixin, UpdateView):
        fields = AuthorCreate.fields
        inlines = AuthorCreate.inlines

    class Authors(ModelResource[Author]):
        model = Author

    class AuthorPages(ModelViewSet[Author]):
        resource = Authors
        create_view = AuthorCreate
        update_view = AuthorUpdate

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors,
                actor=Principal(id=1, is_authenticated=True, is_superuser=True),
            )
            if direct:
                Obox(app)
                for path, view, operation in (
                    ("/authors/add", AuthorCreate, "add"),
                    ("/authors/{pk}/change", AuthorUpdate, "change"),
                ):
                    app.add_api_route(
                        path,
                        view.as_view(resource=ModelResource(model=Author), success_url="/authors"),
                        methods=["GET", "POST"],
                        dependencies=[
                            Depends(permission_required(f"tests.{operation}_author", html=True))
                        ],
                    )
            else:

                class TestConfig(AppConfig):
                    views: ClassVar[list[Any]] = [AuthorPages]

                settings.INSTALLED_APPS = [TestConfig("tests", import_module("tests"))]
                Obox(app)
            base_path = "/authors" if direct else "/tests/authors"
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get(f"{base_path}/add")
                assert page.status_code == 200
                token = page.text.split('value="', 1)[1].split('"', 1)[0]
                parent = {"name": "New parent", "csrfmiddlewaretoken": token}
                invalid = await client.post(
                    f"{base_path}/add",
                    data={**parent, **data({"title": ""}, {"title": "x" * 50})},
                )
                assert invalid.status_code == 422, invalid.text
                assert "at most 40 characters" in invalid.text
                async with session_scope() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Author)) == 0
                failed = await client.post(
                    f"{base_path}/add",
                    data={
                        **parent,
                        "name": "Roll back",
                        **data({"title": "Will roll back"}, {}),
                    },
                )
                assert failed.status_code == 422
                assert "Cannot save this group." in failed.text
                async with session_scope() as session:
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Author)) == 0
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Book)) == 0
                saved = await client.post(
                    f"{base_path}/add", data={**parent, **data({"title": "First"}, {})}
                )
                assert saved.status_code == 303, saved.text
                async with session_scope() as session:
                    author_id = await session.scalar(sa.select(Author.id))
                    book_id = await session.scalar(sa.select(Book.id))
                changed = await client.post(
                    f"{base_path}/{author_id}/change",
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
                async with session_scope() as session:
                    assert list(await session.scalars(sa.select(Book.title).order_by(Book.id))) == [
                        "Changed",
                        "Second",
                    ]

    asyncio.run(scenario())


def test_view_rejects_inline_creation_without_child_add_permission() -> None:
    class Books(TabularInline):
        resource = resource_for(Book, fields=("title",))
        fields = ("title",)

    class AuthorUpdate(InlineViewTestMixin, UpdateView):
        fields = ("name",)
        inlines = (Books,)

    class Authors(ModelResource[Author]):
        model = Author

    class AuthorPages(ModelViewSet[Author]):
        resource = Authors
        update_view = AuthorUpdate

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

            class TestConfig(AppConfig):
                views: ClassVar[list[Any]] = [AuthorPages]

            settings.INSTALLED_APPS = [TestConfig("tests", import_module("tests"))]
            Obox(app)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                page = await client.get(f"/tests/authors/{author.id}/change")
                assert page.status_code == 200
                token = page.text.split('value="', 1)[1].split('"', 1)[0]
                forged = await client.post(
                    f"/tests/authors/{author.id}/change",
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
                async with session_scope() as session:
                    assert await session.scalar(sa.select(Author.name)) == "One"
                    assert await session.scalar(sa.select(sa.func.count()).select_from(Book)) == 2

    asyncio.run(scenario())
