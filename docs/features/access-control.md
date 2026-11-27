# Access control

The account management routes let administrators assign permissions to roles.
An active superuser has all permissions. An API token is limited to its scopes.

Each generated HTML or REST operation uses a model permission such as
`products.view_product`, `products.add_product`, `products.change_product` or
`products.delete_product`. A custom `ModelResource` can further restrict the records
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

## CSRF protection

- Generic HTML views and custom HTML routes with `permission_required(..., html=True)`
  validate an unsafe request using the `csrftoken` cookie and the masked
  `csrfmiddlewaretoken` form field or `X-CSRFToken` header.
- Built-in authentication views and account administration submit the signed
  token in `csrf_token`. Obox binds the token to the request cookies and the
  current identity. The base layout can include a separate sign-out form with
  this field alongside a generic CRUD form.
- Cookie-authenticated JSON writes send the signed token in `X-CSRF-Token`.
  Obtain it from `GET /v0/auth/csrf` and keep the cookies from that response.
  See the [REST API CSRF contract](../api-spec.md#csrf-protection).

The scaffold enables secure cookies when `OBOX_DEBUG=false`. Configure the
same deployment secret across workers. The built-in Auth backend verifies
credentials on each attempt. Apply shared login throttling in the deployment
layer or with a custom auth backend for both HTML and API sign-in.
