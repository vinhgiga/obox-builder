# Add a custom REST route

```python
from fastapi import APIRouter, Depends
from obox import permission_required

router = APIRouter(prefix="/v0/product")


@router.get("/summary", dependencies=[Depends(permission_required("products.view_summary"))])
async def summary():
    return {"title": "Product summary"}
```

Include the router on the host FastAPI app before registering the product CRUD
routes so `/v0/product/summary` is checked before the dynamic detail route.
The permission `products.view_summary` is available to assign to roles.
