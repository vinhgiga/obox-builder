# Analytics

Enable `obox.contrib.analytics` in `INSTALLED_APPS`. Analytics components use a
mapped source model and apply its source policy, field access and query budgets.
Declare HTML routes on `AppConfig.routes`; templates place components with the
`component()` function. See [the chart example](../examples/components/analytics-chart.md).

Analytics provides Bar, Line, Pie, Donut and Scatter charts plus Pivot, with
measure and axis declarations exported from `obox.contrib.analytics`. Supported
aggregates and limits are checked against source model metadata. HTML and query
routes share policy-scoped services.

An analytics component used in a template gets app-scoped `/_components/` data
routes. Those endpoints require `<app>.components.access` plus the component's
permission and source policy. Add the component to `AppConfig.components` to
publish it through the JSON API as well. The
[route example](../examples/pages/compose-page.md) shows template composition.

Analytics is optional contrib functionality. It does not add chart or pivot
concepts to `ModelResource`, the generated HTML views or `ModelApi`. The
[analytics integration contract](../extension-spec.md) describes the current boundary.

## Pivot

`Pivot` accepts a mapped model through `source=` or an iterable of record mappings, a
pandas DataFrame, or a Polars DataFrame through `data=`. Use `compute()` for a standalone
result. Reference a chart component from a route template for HTML, or list it in
`AppConfig.components` to publish it through the JSON API.

```python
from obox.contrib.analytics import Avg, Count, Pivot, Sum

pivot = Pivot(
    name="sales",
    data=sales_frame,
    rows=("category", "code"),
    columns=("status", "channel"),
    values=(Count("code"), Sum("amount"), Avg("amount")),
    drill=True,
)

matrix = pivot.compute()
```

The field order sets the grouping hierarchy. Set `values_axis="rows"` to display
measures on rows. Set the zero-based `values_position` to place them before a
dimension. The default position is after the last dimension. Subtotals can be
hidden independently on each axis with `show_row_subtotals` and
`show_column_subtotals`. `Count()` counts all records, `Count("code")` counts
non-null codes, and `CountNumbers("amount")` counts numeric values.

In-memory data is snapshotted at construction. `fields` limits the columns the
component can publish and drill into. To calculate from a different input, call
`compute(data)` on a pivot configured for those columns. Model-backed pivots
using the original scalar axes and `measure` keep the original response format.
