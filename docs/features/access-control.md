# Access control

The account management routes let administrators assign permissions to roles.
An active superuser has all permissions. An API token is limited to its scopes.

Each generated HTML or REST operation uses a model permission such as
`products.view_product`, `products.add_product`, `products.change_product` or
`products.delete_product`. A custom `Resource` can further restrict the records
returned or the fields a user can write.

Declare a named permission on native FastAPI routes.

```python
from fastapi import Depends
from obox import permission_required


@app.get("/reports", dependencies=[Depends(permission_required("reports.view_index"))])
async def reports():
    return {"reports": []}
```

Use `html=True` for browser views. Obox checks route permissions at startup.
Login, logout, the login CSRF endpoint and framework static assets have their
own access rules. Serve an OpenAPI document with a permission if needed.
