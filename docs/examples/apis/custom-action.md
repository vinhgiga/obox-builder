# Add a custom REST route

```python
from obox.api import ApiResponse, Context, ModelApi, route
from obox.contrib.jobs.services import enqueue

from .models import Product
from .resources import Products


class SyncResult(ApiResponse):
    job_id: str


class ProductApi(ModelApi[Product]):
    resource = Products

    @route.post("/sync", permission="products.product.sync", status_code=202)
    async def sync(self, ctx: Context) -> SyncResult:
        task = await enqueue(ctx.session, "products.sync")
        return SyncResult(job_id=task.id)
```

Enable the jobs app and register the `products.sync` task. Declare
`ProductApi` in `AppConfig.apis` or pass it as `api=ProductApi` when
registering `Products` with Obox. The permission `products.product.sync`
is available to assign to roles.
