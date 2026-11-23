# Route a template with components

Declare HTML routes on the app config. The route selects a template and carries
its title, permission, and menu behavior.

```python
from obox.apps import AppConfig
from obox.routes import Route


class SalesConfig(AppConfig):
    name = "app.sales"
    ui_prefix = "/workspace/sales"
    routes = (
        Route(
            "/revenue",
            "sales/revenue.html",
            title="Revenue",
            permission="sales.view_sale",
        ),
    )
```

Templates decide which components appear and where they sit:

```jinja
{% extends base_template %}
{% block content %}
  <div class="row row-cards">
    <div class="col-md-6">
      {{ component("app.sales.components.RevenueByRegion") }}
    </div>
    <div class="col-md-6">
      {{ component("app.sales.components.RevenueByDistributor") }}
    </div>
  </div>
{% endblock %}
```

`component()` accepts an import path and literal `key`, `title`, and `filters`
options. The app owns each binding and its data routes. A component's source
policy and `components.access` permission protect its data independently from
the route permission. Components appear in the JSON API only when listed in
`AppConfig.components`.
