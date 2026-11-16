# Register an independent REST API

```python
from sqlalchemy import select
from obox.rest_framework.serializers import ModelSerializer
from obox.rest_framework.viewsets import ModelViewSet
from obox.rest_framework.routers import SimpleRouter
from obox.rest_framework.filters import SearchFilter, OrderingFilter
from obox.rest_framework.permissions import IsAuthenticated
from obox.urls import include, path
from app.products.models import Product


class ProductSerializer(ModelSerializer):
    class Meta:
        model = Product
        fields = ("id", "name", "price")


class ProductViewSet(ModelViewSet):
    queryset = select(Product)
    serializer_class = ProductSerializer
    permission_classes = (IsAuthenticated,)
    filter_backends = (SearchFilter, OrderingFilter)
    search_fields = ("name",)
    ordering_fields = ("name", "price")


router = SimpleRouter()
router.register("products", ProductViewSet, basename="product")
urlpatterns = [path("api/", include(router.urls))]
```

`GET /api/products/?search=note&ordering=-price` returns a JSON array by default.
PUT validates the full writable representation; PATCH validates supplied fields.
Errors return400 with field message lists; successful DELETE returns204.
ModelBuilder registration is not required. For pagination set a
PageNumberPagination subclass on pagination_class or configure REST_FRAMEWORK.
