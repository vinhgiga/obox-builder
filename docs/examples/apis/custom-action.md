# Add a REST action

```python
from obox.rest_framework.decorators import action
from obox.rest_framework.response import Response
from app.products.views import ProductViewSet


class ProductDetailViewSet(ProductViewSet):
    @action(detail=True, methods=["get"])
    async def summary(self, request, **kwargs):
        product = await self.get_object()
        return Response({"id": product.id, "name": product.name})
```

Register the viewset on SimpleRouter. The action is exposed at
`/products/{pk}/summary/` and uses the same permissions and transaction.
Customize CRUD through awaited perform_create/perform_update/perform_destroy hooks.
