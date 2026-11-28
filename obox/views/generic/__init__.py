from obox.views.generic.base import TemplateView, View
from obox.views.generic.detail import DetailView
from obox.views.generic.edit import CreateView, DeleteView, FormView, UpdateView
from obox.views.generic.list import GridView
from obox.views.generic.model import ModelViewSet

__all__ = [
    "CreateView",
    "DeleteView",
    "DetailView",
    "FormView",
    "GridView",
    "ModelViewSet",
    "TemplateView",
    "UpdateView",
    "View",
    "ViewConfig",
]
from obox.views.generic._query import ViewConfig
