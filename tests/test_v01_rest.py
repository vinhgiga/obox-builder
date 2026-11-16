import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from obox import Obox
from obox.rest_framework import serializers
from obox.rest_framework.decorators import action
from obox.rest_framework.filters import OrderingFilter, SearchFilter
from obox.rest_framework.pagination import PageNumberPagination
from obox.rest_framework.permissions import IsAuthenticated
from obox.rest_framework.response import Response
from obox.rest_framework.routers import SimpleRouter
from obox.rest_framework.viewsets import ModelViewSet
from obox.urls import _mount, include, path

from tests.test_v01_forms import Item, configured


class ItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = Item
        fields = ("id", "name", "price", "quantity")

    def validate_name(self, value: str) -> str:
        if value == "reject":
            raise serializers.ValidationError("Rejected")
        return value


class Items(ModelViewSet):
    queryset = sa.select(Item).order_by(Item.id)
    serializer_class = ItemSerializer
    filter_backends = (SearchFilter, OrderingFilter)
    search_fields = ("name",)
    ordering_fields = ("name", "price")

    @action(detail=False)
    def count(self, request: Any) -> Response:
        return Response({"action": self.action})


def router_app(cls: Any = Items) -> FastAPI:
    app = FastAPI()
    Obox(app)
    router = SimpleRouter()
    router.register("items", cls)
    _mount(app, [path("api/", include(router.urls))])
    return app


def test_serializer_validation_is_independent_and_does_not_save() -> None:
    async def scenario() -> None:
        async with configured():
            serializer = ItemSerializer(
                data={"name": " alpha ", "price": "2.50", "id": 100, "ignored": True}
            )
            assert await serializer.is_valid()
            assert serializer.validated_data == {"name": "alpha", "price": Decimal("2.50")}
            assert serializer.instance is None
            record = await serializer.save()
            assert record.id == 1
            assert serializer.data == {"id": 1, "name": "alpha", "price": "2.50", "quantity": 1}
            invalid = ItemSerializer(data={"name": "reject", "price": "1"})
            assert not await invalid.is_valid()
            assert invalid.errors == {"name": ["Rejected"]}
            with pytest.raises(serializers.ValidationError):
                await invalid.is_valid(raise_exception=True)
            partial = ItemSerializer(record, data={"quantity": 4}, partial=True)
            assert await partial.is_valid()
            await partial.save()
            assert record.quantity == 4
            assert not await ItemSerializer(record, data={"quantity": 4}).is_valid()

    asyncio.run(scenario())


def test_router_crud_put_patch_errors_types_and_extra_actions() -> None:
    async def scenario() -> None:
        async with configured():
            app = router_app()
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                assert (await client.get("/api/items/")).json() == []
                unsupported = await client.put("/api/items/", json={})
                assert unsupported.status_code == 405
                assert unsupported.json() == {"detail": 'Method "PUT" not allowed.'}
                schema = app.openapi()
                assert "put" not in schema["paths"]["/api/items/"]
                assert "201" in schema["paths"]["/api/items/"]["post"]["responses"]
                assert "204" in schema["paths"]["/api/items/{pk}/"]["delete"]["responses"]
                invalid = await client.post("/api/items/", json={"quantity": 2})
                assert invalid.status_code == 400
                assert invalid.json() == {
                    "name": ["This field is required."],
                    "price": ["This field is required."],
                }
                created = await client.post("/api/items/", json={"name": "alpha", "price": "2.50"})
                assert created.status_code == 201
                assert created.json()["id"] == 1
                assert (await client.put("/api/items/1/", json={"quantity": 2})).status_code == 400
                patch = await client.patch("/api/items/1/", json={"quantity": 3})
                assert patch.status_code == 200
                assert patch.json()["name"] == "alpha"
                assert patch.json()["quantity"] == 3
                assert (await client.get("/api/items/1/")).json() == patch.json()
                assert (await client.get("/api/items/count/")).json() == {"action": "count"}
                assert (await client.get("/api/items/999/")).status_code == 404
                assert (await client.get("/api/items/?search=absent")).json() == []
                duplicate = await client.post("/api/items/", json={"name": "alpha", "price": "1"})
                assert duplicate.status_code == 400
                assert duplicate.json() == {"name": ["This field must be unique."]}
                deleted = await client.delete("/api/items/1/")
                assert deleted.status_code == 204
                assert deleted.content == b""
                assert (await client.get("/api/items/")).json() == []
                assert (
                    await client.post(
                        "/api/items/", content="{", headers={"Content-Type": "application/json"}
                    )
                ).status_code == 400

    asyncio.run(scenario())


def test_pagination_and_permission_hooks() -> None:
    class Page(PageNumberPagination):
        page_size = 1

    class Private(Items):
        permission_classes = (IsAuthenticated,)

    class Paged(Items):
        pagination_class = Page

    async def scenario() -> None:
        async with configured():
            app = router_app(Paged)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                for name in ("alpha", "beta"):
                    assert (
                        await client.post("/api/items/", json={"name": name, "price": "1"})
                    ).status_code == 201
                first = (await client.get("/api/items/")).json()
                assert first["count"] == 2
                assert first["previous"] is None
                assert first["next"] == "http://test/api/items/?page=2"
                assert len(first["results"]) == 1
                last = (await client.get("/api/items/?page=last")).json()
                assert last["next"] is None
                assert last["previous"] == "http://test/api/items/"
                assert (await client.get("/api/items/?page=3")).status_code == 404
        async with configured():
            app = router_app(Private)
            async with (
                app.router.lifespan_context(app),
                httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test"
                ) as client,
            ):
                response = await client.get("/api/items/")
                assert response.status_code == 403
                assert response.json() == {
                    "detail": "Authentication credentials were not provided."
                }

    asyncio.run(scenario())


def test_perform_create_failure_rolls_back_and_object_permissions_follow_scope() -> None:
    from obox.rest_framework.exceptions import ValidationError
    from obox.rest_framework.permissions import BasePermission

    class Failing(Items):
        async def perform_create(self, serializer: Any) -> None:
            await serializer.save()
            raise ValidationError({"name": ["Rejected after persistence"]})

    class DenyObject(BasePermission):
        def has_object_permission(self, request: Any, view: Any, obj: Any) -> bool:
            return False

    class Scoped(Items):
        authentication_classes = ()
        permission_classes = (DenyObject,)

        def get_queryset(self) -> Any:
            return super().get_queryset().where(Item.name == "visible")

    async def scenario() -> None:
        async with configured():
            app = router_app(Failing)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post("/api/items/", json={"name": "alpha", "price": "1"})
                assert response.status_code == 400
                assert (await client.get("/api/items/")).json() == []
            records = []
            for name in ("visible", "hidden"):
                form = ItemSerializer(data={"name": name, "price": "1"})
                assert await form.is_valid()
                records.append(await form.save())
            app = router_app(Scoped)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert len((await client.get("/api/items/")).json()) == 1
                assert (await client.get(f"/api/items/{records[1].id}/")).status_code == 404
                assert (await client.get(f"/api/items/{records[0].id}/")).status_code == 403

    asyncio.run(scenario())


def test_session_authentication_requires_csrf_and_basic_uses_auth_backend() -> None:
    from obox.conf import settings
    from obox.contrib.auth.models import Base as AuthBase
    from obox.contrib.auth.models import User
    from obox.contrib.auth.passwords import hash_password
    from obox.db import _factory, _session
    from obox.rest_framework.authentication import BasicAuthentication
    from obox.views.generic import TemplateView

    class Private(Items):
        permission_classes = (IsAuthenticated,)

    class Basic(Private):
        authentication_classes = (BasicAuthentication,)

    async def scenario() -> None:
        async with configured():
            settings.INSTALLED_APPS = ["obox.contrib.auth"]
            settings.DEBUG = True
            engine = _factory().kw["bind"]
            async with engine.begin() as connection:
                await connection.run_sync(AuthBase.metadata.create_all)
            async with _session() as session:
                session.add(
                    User(username="reader", password_hash=await hash_password("test-value"))
                )
            app = router_app(Basic)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                denied = await client.get("/api/items/")
                assert denied.status_code == 401
                assert denied.headers["www-authenticate"] == 'Basic realm="api"'
                allowed = await client.post(
                    "/api/items/",
                    json={"name": "basic", "price": "1"},
                    auth=httpx.BasicAuth("reader", "test-value"),
                )
                assert allowed.status_code == 201
        async with configured():
            from obox._auth import Principal

            from tests.test_v01_builder import Actors

            app = router_app(Private)
            app.add_middleware(Actors, actor=Principal(id=1, is_authenticated=True))
            _mount(
                app, [path("token/", TemplateView.as_view(template_name="obox/admin/index.html"))]
            )
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                assert (await client.post("/api/items/", json={})).status_code == 403
                await client.get("/token/")
                allowed = await client.post(
                    "/api/items/",
                    json={"name": "session", "price": "1"},
                    headers={"X-CSRFToken": client.cookies["csrftoken"]},
                )
                assert allowed.status_code == 201

    asyncio.run(scenario())


def test_django_model_permission_templates_and_unauthenticated_denial() -> None:
    from types import SimpleNamespace
    from typing import ClassVar

    from obox._auth import Principal
    from obox.rest_framework.permissions import DjangoModelPermissions

    class RequireView(DjangoModelPermissions):
        perms_map: ClassVar[dict[str, list[str]]] = {
            **DjangoModelPermissions.perms_map,
            "GET": ["%(app_label)s.view_%(model_name)s"],
        }

    from obox.rest_framework.views import APIView

    with pytest.raises(TypeError, match="invalid keyword"):
        APIView.as_view(unknown_option=True)

    async def scenario() -> None:
        permission = RequireView()
        assert permission.get_required_permissions("POST", Item) == ["tests.add_item"]
        request = SimpleNamespace(method="GET", user=Principal(is_authenticated=True))
        assert not await permission.has_permission(request, Items())
        request.user = Principal(is_authenticated=True, permissions=frozenset({"tests.view_item"}))
        assert await permission.has_permission(request, Items())

    asyncio.run(scenario())
