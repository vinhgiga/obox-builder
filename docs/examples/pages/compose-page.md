# Compose a page

Create a page with two components on the first row and one on the second:

```python
from obox_builder import AppConfig, Page

from app.sales.builders import SaleBuilder
from app.sales.components import RevenueByRegion, RevenueByDistributor


class SalesConfig(AppConfig):
    name = "app.sales"
    label = "sales"
    builders = (SaleBuilder,)
    components = (RevenueByRegion, RevenueByDistributor)
    pages = (
        Page(
            "revenue",
            title="Revenue",
            components=((RevenueByRegion, RevenueByDistributor),),
            permission="sales.sales.list",
        ),
    )
```

Each component remains subject to its own source policy and permissions. A page
permission can further restrict who may open the page; it does not grant access
to the component's data.
