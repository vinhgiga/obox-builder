# Auth and permissions

```python
# settings.py
INSTALLED_APPS = ["obox.contrib.auth", "obox.builder", "app.products"]
AUTH = {"api_tokens": True}
REST_FRAMEWORK = {
    "DEFAULT_PERMISSION_CLASSES": ["obox.rest_framework.permissions.IsAuthenticated"],
}
```

Run migrations and `obox createsuperuser`. The builder requires active
staff and model grants such as `products.view_product` or `products.change_product`.
Superusers have model access. REST can use IsAuthenticated, IsAdminUser or
DjangoModelPermissions per viewset. Override get_queryset to constrain rows and
has_object_permission for per-object checks.

Contrib resources use separate grants such as `products.products.update`.
A builder grant does not implicitly grant a product operation. Product policies
can restrict readable/writable fields, row scope and trusted preset values.
