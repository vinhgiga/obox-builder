# Compose a contrib page

```python
from obox.apps import AppConfig
from obox.contrib.resources.presentation import Page
from app.sales.components import RevenueByRegion, RevenueByDistributor


class SalesConfig(AppConfig):
    name = "app.sales"

    def ready(self):
        self.components = (RevenueByRegion, RevenueByDistributor)
        self.pages = (
            Page(
                "revenue",
                title="Revenue",
                components=((RevenueByRegion, RevenueByDistributor),),
                permission="sales.sales.list",
            ),
        )
```

Enable `obox.contrib.resources`. A page permission does not grant access to its
component data; each component retains source policy checks. Pages are product
presentation declarations. For ordinary template pages use core TemplateView.
