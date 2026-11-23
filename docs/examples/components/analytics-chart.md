# Add an analytics chart

Define a chart using a mapped model, then reference it where it should appear in
the route's template.

```python
from obox.apps import AppConfig
from obox.contrib.analytics import Bar, Sum
from obox.routes import Route

from app.sales.models import Sale


class RevenueByRegion(Bar):
    name = "revenueByRegion"
    source = Sale
    fields = ("region", "revenue")
    title = "Revenue by region"
    x = "region"
    y = Sum("revenue")
    order = "-y"


class SalesConfig(AppConfig):
    name = "app.sales"
    routes = (
        Route(
            "/revenue",
            "sales/revenue.html",
            title="Revenue",
            permission="sales.view_sale",
        ),
    )
```

```jinja
{% extends base_template %}
{% block content %}
  {{ component("app.sales.components.RevenueByRegion") }}
{% endblock %}
```

The source model's authorization policy controls aggregate and field access.
The data endpoint also requires `sales.components.access`; a route permission
does not grant component data access. Add the component to `AppConfig.components`
only when it should also be published through the headless JSON API.
