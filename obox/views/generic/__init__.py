from obox.views.generic.base import TemplateView, View
from obox.views.generic.decorators import action
from obox.views.generic.detail import DetailView
from obox.views.generic.edit import CreateView, DeleteView, FormView, UpdateView
from obox.views.generic.inlines import StackedInline, TabularInline
from obox.views.generic.list import GridView, ListView
from obox.views.generic.model import ModelViewSet

__all__ = [
    "CreateView",
    "DeleteView",
    "DetailView",
    "FormView",
    "GridView",
    "ListView",
    "ModelViewSet",
    "StackedInline",
    "TabularInline",
    "TemplateView",
    "UpdateView",
    "View",
    "ViewConfig",
    "action",
]
from obox.views.generic._query import ViewConfig
