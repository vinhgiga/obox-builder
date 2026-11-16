from __future__ import annotations

from typing import Any, cast

from fastapi import Request

from obox._async import invoke
from obox.db import _session
from obox.rest_framework.generics import GenericAPIView
from obox.rest_framework.response import Response


class GenericViewSet(GenericAPIView):
    action: str | None
    _actions: dict[str, str]
    basename: str | None = None
    detail: bool | None = None
    suffix: str | None = None

    @classmethod
    def as_view(cls, actions: dict[str, str] | None = None, **initkwargs: Any) -> Any:
        if not actions:
            raise TypeError("The actions argument must be provided.")
        mapping = dict(actions)
        if "get" in mapping:
            mapping.setdefault("head", mapping["get"])

        callback = super().as_view(_actions=mapping, **initkwargs)
        callback.actions = mapping
        callback.http_method_names = [*mapping, "options"]
        return callback

    def setup(self, request: Request, *args: Any, **kwargs: Any) -> None:
        mapping = self._actions
        self.action = (
            "metadata" if request.method == "OPTIONS" else mapping.get(request.method.lower())
        )
        self.action_map = mapping
        for method, action in mapping.items():
            setattr(self, method, getattr(self, action))
        super().setup(request, *args, **kwargs)


class ModelViewSet(GenericViewSet):
    async def list(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        queryset = await self.filter_queryset(await invoke(self.get_queryset))
        page = await self.paginate_queryset(queryset)
        if page is not None:
            return cast(
                Response, self.get_paginated_response(self.get_serializer(page, many=True).data)
            )
        async with _session() as session:
            objects = list((await session.scalars(queryset)).unique())
        return Response(self.get_serializer(objects, many=True).data)

    async def retrieve(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        obj = await self.get_object()
        return Response(self.get_serializer(obj).data)

    async def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        serializer = self.get_serializer(data=await self._data())
        await serializer.is_valid(raise_exception=True)
        await invoke(self.perform_create, serializer)
        return Response(serializer.data, status=201)

    async def update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        partial = kwargs.pop("partial", False)
        obj = await self.get_object()
        serializer = self.get_serializer(obj, data=await self._data(), partial=partial)
        await serializer.is_valid(raise_exception=True)
        await invoke(self.perform_update, serializer)
        return Response(serializer.data)

    async def partial_update(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        return await self.update(request, *args, **{**kwargs, "partial": True})

    async def destroy(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        await invoke(self.perform_destroy, await self.get_object())
        return Response(status=204)

    async def perform_create(self, serializer: Any) -> None:
        await serializer.save()

    async def perform_update(self, serializer: Any) -> None:
        await serializer.save()

    async def perform_destroy(self, instance: Any) -> None:
        async with _session() as session:
            await session.delete(instance)
            await session.flush()
