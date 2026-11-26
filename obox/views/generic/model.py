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
    list_view: type[ListView] | None = None
    detail_view: type[DetailView] | None = None
    create_view: type[CreateView] | None = None
    update_view: type[UpdateView] | None = None
    delete_view: type[DeleteView] | None = None
