# Analytics

The optional Analytics plugin adds chart and pivot components over registered
resources. It uses the source resource's policy scope and field permissions;
an analytics component cannot broaden the records a caller may read.

Available chart kinds are bar, line, scatter, pie, and donut. Charts can group
and aggregate configured fields. Pivot components support row and optional
column dimensions with a configured measure. Query budgets bound rows, columns,
cells, groups, points, and execution time.

```python
from obox_builder import AppConfig, Component, Page
from obox_builder.contrib.analytics import Analytics, Bar, Sum


class RevenueByRegion(Bar):
    name = "byRegion"
    source = SaleBuilder
    x = "region"
    y = Sum("revenue")
    order = "-y"


class SalesConfig(AppConfig):
    name = "app.sales"
    label = "sales"
    builders = (SaleBuilder,)
    components: tuple[type[Component] | Component, ...] = (RevenueByRegion,)
    pages = (Page("revenue", components=((RevenueByRegion,),)),)
```

Install the plugin when composing the host app:

```python
from obox_builder.contrib.analytics import Analytics

obox = Obox(app, settings="setting", plugins=(Auth(), Analytics()))
```

The source builder must grant aggregate access to the fields used by the chart.
See the [analytics design specification](../extension-spec.md) for the detailed
component and aggregation contract; items marked proposed are not implemented
features.
