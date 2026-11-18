# Analytics

Enable `obox.contrib.resources` and `obox.contrib.analytics` in INSTALLED_APPS.
Analytics reads registered Resource declarations and applies their source policy,
field access and query budgets. Page/component registration happens through the
owning application's ready hook. See [the chart example](../examples/components/analytics-chart.md).

The existing product API includes Bar, Line, Pie, Donut and Scatter charts,
Pivot and CustomChart, with measure/axis declarations exported from
`obox.contrib.analytics`. Supported aggregates and limits are checked against
source model metadata. HTML and query routes share policy-scoped services.

Analytics is optional contrib functionality. It does not add chart or pivot
concepts to ModelBuilder or the standardized REST core. The
[analytics integration contract](../extension-spec.md) describes the current boundary.

## Pivot

`Pivot` accepts a mapped model through `source=` or an iterable of record mappings, a
pandas DataFrame, or a Polars DataFrame through `data=`. Use `compute()` for a standalone
result, or register the same component with Analytics to expose it on a page or through
the component API.

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
