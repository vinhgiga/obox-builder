"""Group the HTML views generated for a ModelResource."""

from typing import Generic, TypeVar

from obox.resources import ModelResource
from obox.views.generic.detail import DetailView
from obox.views.generic.edit import CreateView, DeleteView, UpdateView
from obox.views.generic.list import ListView

ModelT = TypeVar("ModelT")


class ModelViewSet(Generic[ModelT]):
    resource: type[ModelResource[ModelT]]
    slug: str | None = None
    prefix: str | None = None
    list_view: type[ListView[ModelT]] | None = None
    collection_views: tuple[type[ListView[ModelT]], ...] = ()
    detail_view: type[DetailView[ModelT]] | None = None
    create_view: type[CreateView[ModelT]] | None = None
    update_view: type[UpdateView[ModelT]] | None = None
    delete_view: type[DeleteView[ModelT]] | None = None
