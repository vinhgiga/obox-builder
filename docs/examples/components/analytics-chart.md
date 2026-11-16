# Add an analytics chart

```python
from obox.apps import AppConfig
from obox.contrib.analytics import Bar, Sum
from obox.contrib.resources.presentation import Page
from app.sales.resources import SaleResource


class RevenueByRegion(Bar):
    name = "byRegion"
    source = SaleResource
    title = "Revenue by region"
    x = "region"
    y = Sum("revenue")
    order = "-y"


class SalesConfig(AppConfig):
    name = "app.sales"

    def ready(self):
        self.resources = (SaleResource,)
        self.components = (RevenueByRegion,)
        self.pages = (Page("revenue", components=((RevenueByRegion,),)),)
```

Enable resources and analytics before the application in INSTALLED_APPS. Define
SaleResource with an explicit model and policy. Source read/aggregate permissions
apply independently of page access. Product resource queries and chart results
share the existing contrib transaction/policy engine.
