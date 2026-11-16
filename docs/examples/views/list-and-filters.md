# Generic views

```python
from sqlalchemy import select
from obox.views.generic import ListView, CreateView, UpdateView, DeleteView
from obox.urls import path
from app.products.models import Product
from app.products.forms import ProductForm


class ProductListView(ListView):
    model = Product
    template_name = "products/list.html"
    paginate_by = 20
    ordering = ("name",)

    def get_queryset(self):
        query = super().get_queryset()
        term = self.request.query_params.get("q")
        return query.where(Product.name.icontains(term)) if term else query


class ProductCreateView(CreateView):
    model = Product
    form_class = ProductForm
    success_url = "/products/"


class ProductUpdateView(UpdateView):
    model = Product
    form_class = ProductForm
    success_url = "/products/"


class ProductDeleteView(DeleteView):
    model = Product
    success_url = "/products/"


urlpatterns = [
    path("products/", ProductListView.as_view(), name="product-list"),
    path("products/add/", ProductCreateView.as_view()),
    path("products/<int:pk>/change/", ProductUpdateView.as_view()),
    path("products/<int:pk>/delete/", ProductDeleteView.as_view()),
]
```

get_queryset returns a Select without executing SQL. Each request receives its
own view/form. Invalid forms render status200, successful submissions redirect302.
A custom form template must render `csrf_token` in `csrfmiddlewaretoken`.
Authentication and row visibility are application choices; override dispatch or
querysets as appropriate. Builder routes add staff/model permission checks.
