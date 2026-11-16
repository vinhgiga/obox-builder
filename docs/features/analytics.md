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
