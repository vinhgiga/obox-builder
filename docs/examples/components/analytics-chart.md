# Add an analytics chart

An analytics chart reads from a registered resource builder. Declare a measure
and category dimension, then add the chart to the app's component list and a
page:

```python
from obox_builder import AppConfig, Page
from obox_builder.contrib.analytics import Bar, Sum

from app.sales.builders import SaleBuilder


class RevenueByRegion(Bar):
    name = "byRegion"
    source = SaleBuilder
    title = "Revenue by region"
    x = "region"
    y = Sum("revenue")
    order = "-y"


class SalesConfig(AppConfig):
    name = "app.sales"
    label = "sales"
    builders = (SaleBuilder,)
    components = (RevenueByRegion,)
    pages = (Page("revenue", components=((RevenueByRegion,),)),)
```

Install `Analytics()` in the Obox runtime. The source policy must permit
aggregation of the configured fields; query budgets are applied to the result.
For a complete example and chart options, see [Analytics](../../features/analytics.md).
