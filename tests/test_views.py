import asyncio
import re
from typing import Any, cast

import httpx
import sqlalchemy as sa
from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from obox import Obox, forms, permission_required
from obox.core.principal import Principal
from obox.views.generic import (
    CreateView,
    DeleteView,
    FormView,
    ListView,
    UpdateView,
    View,
)
from pydantic import BaseModel, Field, field_validator

from tests.test_forms_integration import Item, ItemForm, configured
from tests.test_registered_views import Actors


def test_typed_form_view_renders_errors_and_reads_validated_attributes() -> None:
    class ContactInput(BaseModel):
        name: str
        rate: int = Field(ge=1, le=5)

        @field_validator("name")
        @classmethod
        def uppercase(cls, value: str) -> str:
            return value.upper()

    class Contact(forms.Form[ContactInput]):
        schema = ContactInput

    class ContactView(FormView):
        form_class = Contact
        template_name = "obox/generic/form.html"

        async def form_valid(self, form: forms.Form) -> HTMLResponse:
            contact = cast(Contact, form)
            assert contact.cleaned_data is not None
            return HTMLResponse(f"{contact.cleaned_data.name}:{contact.cleaned_data.rate}")

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors, actor=Principal(id=1, is_authenticated=True, is_superuser=True)
            )
            Obox(app)
            guard = [Depends(permission_required("tests.use_views", html=True))]
            app.add_api_route(
                "/contact/", ContactView.as_view(), methods=["GET", "POST"], dependencies=guard
            )
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                page = await client.get("/contact/")
                assert page.status_code == 200
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                invalid = await client.post(
                    "/contact/",
                    data={"csrfmiddlewaretoken": token[1], "name": "<script>", "rate": "9"},
                )
                assert invalid.status_code == 200
                assert "&lt;script&gt;" in invalid.text
                assert "less than or equal to 5" in invalid.text
                valid = await client.post(
                    "/contact/",
                    data={"csrfmiddlewaretoken": token[1], "name": "Alice", "rate": "4"},
                )
                assert valid.status_code == 200
                assert valid.text == "ALICE:4"

    asyncio.run(scenario())


def test_generic_views_create_update_delete_and_queryset_contract() -> None:
    class Create(CreateView):
        model = Item
        form_class = ItemForm
        template_name = "obox/generic/form.html"
        success_url = "/items/"

    class Update(UpdateView):
        model = Item
        form_class = ItemForm
        template_name = "obox/generic/form.html"
        success_url = "/items/"

    class Delete(DeleteView):
        model = Item
        template_name = "obox/generic/confirm_delete.html"
        success_url = "/items/"

    class List(ListView):
        model = Item
        template_name = "obox/generic/list.html"

        def get_context_data(self, **kwargs: Any) -> dict[str, Any]:
            context = super().get_context_data(**kwargs)
            return {
                **context,
                "rows": [[obj.name] for obj in self.object_list],
                "list_display": ["name"],
            }

    async def scenario() -> None:
        async with configured():
            app = FastAPI(openapi_url=None)
            app.add_middleware(
                Actors, actor=Principal(id=1, is_authenticated=True, is_superuser=True)
            )
            Obox(app)
            guard = [Depends(permission_required("tests.use_views", html=True))]
            for route, view, methods in (
                ("/items/", List, ["GET"]),
                ("/new/", Create, ["GET", "POST"]),
                ("/items/{pk:int}/", Update, ["GET", "POST"]),
                ("/items/{pk:int}/delete/", Delete, ["GET", "POST"]),
            ):
                app.add_api_route(route, view.as_view(), methods=methods, dependencies=guard)
            assert isinstance(List().get_queryset(), sa.Select)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                page = await client.get("/new/")
                assert page.status_code == 200
                token = re.search(r'name="csrfmiddlewaretoken" value="([^"]+)"', page.text)
                assert token is not None
                denied = await client.post("/new/", data={"name": "a", "price": "1"})
                assert denied.status_code == 403
                invalid = await client.post("/new/", data={"csrfmiddlewaretoken": token[1]})
                assert invalid.status_code == 200
                assert "This field is required." in invalid.text
                created = await client.post(
                    "/new/", data={"csrfmiddlewaretoken": token[1], "name": "Alpha", "price": "1"}
                )
                assert created.status_code == 303
                assert created.headers["location"] == "/items/"
                listing = await client.get("/items/")
                assert "alpha" in listing.text
                updated = await client.post(
                    "/items/1/",
                    data={"csrfmiddlewaretoken": token[1], "name": "Beta", "price": "2"},
                )
                assert updated.status_code == 303
                assert (await client.get("/items/999/")).status_code == 404
                unsupported = await client.put(
                    "/items/1/delete/", headers={"X-CSRFToken": token[1]}
                )
                assert unsupported.status_code == 405
                assert "PUT" not in unsupported.headers["allow"]
                deleted = await client.post(
                    "/items/1/delete/", data={"csrfmiddlewaretoken": token[1]}
                )
                assert deleted.status_code == 303
                assert "beta" not in (await client.get("/items/")).text

    asyncio.run(scenario())


def test_as_view_uses_new_instances_and_preserves_path_parameters() -> None:
    class Counter(View):
        count = 0

        def get(self, request: Request, **kwargs: Any) -> HTMLResponse:
            self.count += 1
            return HTMLResponse(f"{self.count}:{kwargs['pk']}")

    async def scenario() -> None:
        async with configured():
            app = FastAPI()
            router = APIRouter()
            router.add_api_route("/{pk:int}/", Counter.as_view(), methods=["GET", "OPTIONS"])
            app.include_router(router, prefix="/nested")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get("/nested/2/")).text == "1:2"
                assert (await client.get("/nested/3/")).text == "1:3"
                assert (await client.options("/nested/2/")).status_code == 200
                assert (await client.get("/nested/not-int/")).status_code == 404

    asyncio.run(scenario())


def test_native_route_names_and_csrf_origin_checks() -> None:
    class Echo(View):
        def get(self, request: Request, **kwargs: Any) -> HTMLResponse:
            from obox.middleware.csrf import get_token

            get_token(request)
            return HTMLResponse(str(kwargs))

        post = get

    async def scenario() -> None:
        async with configured():
            app = FastAPI()
            router = APIRouter(prefix="/group")
            router.add_api_route(
                "/{key}/", Echo.as_view(), name="test:echo", methods=["GET", "POST"]
            )
            app.include_router(router)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.get("/group/good-slug/")
                assert "good-slug" in response.text
                assert str(app.url_path_for("test:echo", key="a")) == "/group/a/"
                token = client.cookies["csrftoken"]
                assert (
                    await client.post(
                        "/group/a/", headers={"X-CSRFToken": token, "Origin": "http://hostile"}
                    )
                ).status_code == 403
                assert (
                    await client.post(
                        "/group/a/", headers={"X-CSRFToken": token, "Origin": "http://test"}
                    )
                ).status_code == 200

    asyncio.run(scenario())


def test_jinja_backend_options_errors_and_standalone_template_view(tmp_path: Any) -> None:
    import pytest
    from obox.conf import settings
    from obox.template import TemplateDoesNotExist, TemplateSyntaxError
    from obox.template.backends.jinja2 import Jinja2
    from obox.views.generic import TemplateView

    (tmp_path / "hello.html").write_text("{{ value }}:{{ marker }}")
    config = {"DIRS": [tmp_path], "OPTIONS": {"autoescape": True}}
    backend = Jinja2(config)
    assert (
        backend.get_template("hello.html").render({"value": "<ok>", "marker": "yes"})
        == "&lt;ok&gt;:yes"
    )
    with pytest.raises(TemplateDoesNotExist, match="absent"):
        backend.get_template("absent.html")
    with pytest.raises(TemplateSyntaxError):
        backend.from_string("{% broken %}")

    async def scenario() -> None:
        async with configured():
            settings.TEMPLATES = [{**config, "BACKEND": "obox.template.backends.jinja2.Jinja2"}]
            app = FastAPI()
            app.add_api_route(
                "/",
                TemplateView.as_view(
                    template_name="hello.html", extra_context={"value": "<ok>", "marker": "yes"}
                ),
                methods=["GET"],
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get("/")).text == "&lt;ok&gt;:yes"

    asyncio.run(scenario())


def test_native_uuid_lookup_works_in_generic_views() -> None:
    from uuid import UUID, uuid4

    from obox.forms import modelform_factory
    from obox.views.generic import DetailView
    from sqlalchemy.orm import Mapped, mapped_column

    from tests.test_forms_integration import Base

    class Record(Base):
        __tablename__ = "v01_uuid_records"
        record_key: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)
        name: Mapped[str] = mapped_column(sa.String(20))

    class Detail(DetailView):
        model = Record

        def render_to_response(self, context: Any, **kwargs: Any) -> HTMLResponse:
            return HTMLResponse(str(context["object"].record_key))

    async def scenario() -> None:
        async with configured():
            form = modelform_factory(Record, fields=("name",))({"name": "first"})
            assert await form.is_valid()
            record = await form.save()
            app = FastAPI()
            app.add_api_route("/records/{pk:uuid}/", Detail.as_view(), methods=["GET"])
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.get(f"/records/{record.record_key}/")).text == str(
                    record.record_key
                )

    asyncio.run(scenario())
