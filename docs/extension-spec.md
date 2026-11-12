# OBOX Extensions: Chart and Pivot API Specification

**Status:** Proposed replacement for the analytics public API; not an implementation report.

**Audience:** Application authors and framework, query-engine, and renderer implementers.

This specification defines chart and pivot extensions for obox-builder. It replaces the
design assumptions in `research.md`, which is an agent-written draft, while preserving
the framework's FastAPI/SQLAlchemy architecture. Django supplies the declaration style,
not an additional ORM or runtime dependency. The shared contracts in
[core-spec.md](core-spec.md) and [api-spec.md](api-spec.md) continue to apply.

**MUST**, **SHOULD**, and **MAY** express normative requirements. **[R]** identifies a
functional requirement; **[O]** identifies an obox-builder design decision.

## 1. Scope and design boundaries

### 1.1 Requirement areas

| ID | Area | Scope relevant to this specification |
| --- | --- | --- |
| R1 | Dashboard composition | Five chart types; grouped/stacked bars; multiple measures and two axes; source groups and filters; pivot axes, summaries, totals, split values, expansion, and underlying records. |
| R2 | Chart configuration | Raw values or count/numeric aggregates; date buckets; empty-category inclusion; source/X/Y ordering; grouping; zero baseline; point and legend drill; range zoom. |
| R3 | Pivot configuration | Table/view source, mandatory row and optional column, type-dependent summaries, split/composite choices, group/value ordering. |
| R4 | Record detail | Charts over linked records, fixed conditions, orientation, palette, legend position, record-count display, and pie/donut percentages. |
| R5 | Source bindings | Independently bound chart sources; copied view conditions; viewer-specific restrictions; filters connected to selected elements; titles, subtitles, labels, and select colors. |
| R6 | Custom visualization | A table/view supplies data to a custom visualization specification supporting layering, faceting, and advanced presentation. |
| R7 | Filters and host actions | Fixed versus viewer filters, tabs/dropdowns, record printing and CSV controls, and limitations of page printing. |
| R8 | Responsive interfaces | Dashboard charts, pivots, and record-detail charts adapt to narrow screens and touch interaction. |
| R9 | Summaries | Summary functions depend on field type and executor support. |

Production design follows
[Django's admin attributes and hooks](https://docs.djangoproject.com/en/6.0/ref/contrib/admin/),
[its aggregation cautions about joins](https://docs.djangoproject.com/en/6.0/topics/db/aggregation/#combining-multiple-aggregations),
[SQLAlchemy's aggregate/subquery model](https://docs.sqlalchemy.org/en/20/tutorial/data_select.html#aggregate-functions-with-group-by-having),
[PostgreSQL's aggregate semantics](https://www.postgresql.org/docs/18/functions-aggregate.html),
and [W3C guidance on accessible charts](https://www.w3.org/WAI/tutorials/images/complex/).
These are implementation references, not dependencies to add.

### 1.2 Design boundaries

- Distinguish the X/category dimension, a grouping dimension, and measure series.
  Multiple independent category axes are not core.
- Five primary series and one secondary series MUST be expressible. Resource
  budgets bound series; grouping partitions each configured series consistently.
- Require responsive web behavior without promising native apps or adding
  platform-specific restrictions to the Python API.
- Define browser print as a host action, without promising paginated reporting
  or a CSV/XLSX export service.
- Section 4 defines summary-function support and split-value total arithmetic
  explicitly; do not infer support from unrelated summary controls.

### 1.3 Repository baseline and exclusions

The existing implementation already has `Analytics`, object-based `Dashboard`,
four chart kinds, scalar `Measure`, aggregation, policy checks, bounded queries,
HTML pages, and record drill. It lacks the complete contract below. The draft's
greenfield `chartpivot` package, Django adapter, public compiler, compatibility
profiles, three separate version counters, and remote-source scanning are not adopted.

Keep the feature under `obox_builder.contrib.analytics`. Reuse resource services,
field metadata, filter parsing, authentication, transaction ownership, problem
responses, localization, and current renderer assets where suitable. Do not make
core resource services depend on HTTP, templates, or chart-renderer packages.

Out of scope: external data connectors; a visual editor; a general extension
marketplace; KPI, navigation, forms, org charts, or flowcharts; native mobile apps;
multi-level/multi-measure pivots; arbitrary multi-source joins; XLSX export;
scheduled reports; and persistent viewer configuration. Existing record CRUD is
reused when a permitted user opens a detail page.

## 2. Declarations and host integration

### 2.1 Smallest application declaration

```python
from obox_builder.contrib.analytics import Analytics, Chart, Dashboard, Pivot, Sum


class SalesDashboard(Dashboard):
    source = Sale

    revenue = Chart("bar", x="region", y=Sum("revenue"))
    matrix = Pivot(rows="owner", columns="region", value=Sum("revenue"))


# Register the resource builders first.
analytics = obox.install(Analytics(SalesDashboard))
```

`Obox.install()` already exists. The earlier plan's `app.use()` was illustrative;
this specification does not add a second plugin-registration method.

Use one `Chart` class for `bar`, `line`, `scatter`, `pie`, and `donut`, and one
`Pivot` class. `Count()` is the default value. There are no parallel class/object
dashboard authoring APIs or separate query engines per visualization kind.

All declarations and QueryState are exported from
`obox_builder.contrib.analytics`; `Widget` in signatures means the
`Chart | Pivot | CustomChart` union. Renderer-specific adapters live in optional
renderer modules. Source/expression descriptors remain Python objects, not a
second public serialized specification that application authors must maintain.

### 2.2 Dashboard and Group

```python
class Dashboard:
    source = None  # registered model, Source, or inherited by groups
    slug = None  # inferred from class name
    title = None  # inferred from class name
    description = ""
    where = None  # Q / existing FilterGroup
    filters = ()  # Filter declarations
    timezone = "UTC"
    columns = 1  # 1, 2, or 3 widgets/groups per responsive row
    allow_print = False

    async def has_view_permission(self, ctx: Context) -> bool: ...
    def get_queryset(self, ctx: Context, *, source: Source) -> Select: ...
    def get_filters(self, ctx: Context) -> Sequence[Filter]: ...
    def get_widgets(self, ctx: Context) -> Mapping[str, Widget | type[Group]]: ...


class Group(Dashboard):
    timezone = None  # inherit the enclosing dashboard
    allow_print = None  # inherit; a child can only narrow this gate
    width = "full"  # full or half, reduced to full on narrow screens
    background = False
```

`Group` is a source/filter container inside a dashboard, not an independently
registered page. Nested `Group` declarations are limited to one level. Direct
dashboard widgets form an implicit group using the dashboard source. Every group
uses one logical source. A dashboard may contain groups from different sources.

```python
class OperationsDashboard(Dashboard):
    class Sales(Group):
        source = Sale
        where = Q(status="Won")
        filters = (Filter("region"),)
        columns = 2

        revenue = Chart("bar", x="region", y=Sum("revenue"))
        trend = Chart("line", x=Dim("created_at", bucket="month"), y=Sum("revenue"))

    class Stock(Group):
        source = InventoryItem
        quantities = Pivot(rows="warehouse", value=Sum("quantity"))
```

The following binding rules are normative:

- Discover widgets/groups in declaration order through the class MRO. An
  override retains its inherited position; assigning `None` removes an inherited
  declaration. Compile immutable, application-local copies at installation;
  never modify class attributes in a request.
- Infer `SalesDashboard.slug` as `sales`. A group's lower-snake-case declaration
  name prefixes its descendants: `sales.revenue`. Local widget attribute names
  are stable IDs, not localized labels. Explicit `id` overrides must be unique
  in the dashboard. IDs use lowercase ASCII letters, digits, underscores and
  dots, start with a letter, and cannot contain empty path segments.
- Infer titles from declaration names, axis labels from field metadata, and
  measure labels from operation/field metadata. Explicit labels win.
- A child inherits its group's source; widgets cannot silently replace it.
  Source changes require another group. Dashboard filters must name their target
  groups; cross-source filters without explicit, type-compatible bindings fail
  installation. Group filters default to all widgets in that group.
- `get_queryset()` supplies an additional selection of source record IDs;
  intersect it with the authorized resource query. It cannot replace mandatory
  scopes. Its default is `select(source.model)`. Apply the dashboard hook and
  the selected group's hook to that group's source, intersecting both selections.
- `get_widgets()` may remove registered widgets or return immutable replacements
  with the same IDs/source and valid declarations. Default: all declarations.
  Dynamic replacements pass the same compilation and policy checks per request.
- `has_view_permission()` defaults to true as an additional host gate. A widget
  still requires resource and field permissions. Omit inaccessible widgets and
  empty groups; omit dashboards with no accessible widgets.
- Preserve declaration order in the UI. Each visual row uses its tallest
  configured widget height. Containers specify composition only; this is not a
  general page-layout API.

### 2.3 Reusable source selection and field metadata

```text
Source(model, *, where=None, order=(), fields=None)
DataField(expression, *, kind, dependencies, multiple=False,
          label=None, choices=None, format=None)
```

A registered SQLAlchemy model is shorthand for `Source(model)`. `Source` adds a
reusable fixed predicate, deterministic source order, or explicitly declared
query projections. It is the code-first equivalent of selecting a table/view;
it does not import UI view settings implicitly.

`fields` is a mapping of Python names to `DataField`. Use it only for computed
SQL projections or typed collection fields that model inspection cannot infer.
Expressions are trusted server-side SQLAlchemy expressions/mapped attributes,
never request strings or Python callbacks evaluated once per record. A
collection relationship may be a field expression when its target is registered.
Mapped scalar/computed fields remain the normal path.

The source schema MUST describe a stable field ID/public alias, kind,
nullability, scalar/collection cardinality, label, ordering, formatting, and
choice/reference IDs separately from their labels. Required kinds are integer,
decimal, number, text, boolean, date, datetime, duration, select, reference, and
typed collections of scalar/select/reference values. Numeric formula, lookup,
and rollup equivalents are typed query projections, not a formula-language API.

`DataField.dependencies` lists published resource fields used by its expression;
relationships also identify target dependencies. Its name must be explicitly
authorized by `aggregate_fields()`, and every dependency must be readable and
aggregate-authorized. Hiding a raw dependency does not make its derived value
safe. Reject untyped JSON/object grouping and unresolved dependencies. A
projection may add a new name but may not replace a mapped field's expression.
These projection names do not automatically become CRUD fields or HTTP filters.

For permission checks, the logical source's published fields are its published
mapped fields plus explicit `Source.fields` entries. Intersect these names with
`Policy.read_fields(ctx)` when it is non-null and with `aggregate_fields(ctx)`.
Mapped dependencies additionally pass the existing builder's readable-field
check; related dependencies pass the target builder's checks. This makes an
explicit analytics-only projection possible without publishing it in CRUD or
using `Source.fields` to bypass a deny-list.

Collections have declared set semantics: sort and deduplicate typed element IDs.
Infer reference identity from the target primary key and display labels from its
registered label field. Apply target scopes before exposing membership/labels;
if doing so cannot be guaranteed, deny that field. Attachment/button structures
are not chart dimensions. An application may project attachment metadata into a
separate explicitly authorized numeric field.

```python
won_sales = Source(Sale, where=Q(status="Won"), order=("created_at", "id"))
```

Python source-order terms use field names with an optional leading `-` for
descending order. Append the internal primary key as a stable tie-breaker;
internal identity/deduplication does not require exposing that key as a field.

### 2.4 Record-detail mount

```text
Related(relationship, dashboard, *, id=None)
```

```python
class CustomerBuilder(ModelBuilder[Customer]):
    class View(View):
        analytics = (Related("sales", SalesDashboard),)
```

`View.analytics` is a new optional sequence, default `()`. `Related` binds a
declared dashboard to one registered relationship of the current record. The
mount ID defaults to the relationship name. Every data-bearing group must use
the relationship's target model; unrelated groups fail installation.

Register mounts when Analytics is installed after the resource builders. A
dashboard discovered only through a mount is not a global reporting page.
Requests for it require that mount's parent context. Explicitly passing the same
dashboard to `Analytics(...)` additionally enables its standalone page.

For every mount query, authorize retrieval of the parent and readability of the
relationship; intersect relationship membership with the target's authorized
list scope. A supplied parent ID or selector never grants access. Parent and
target tenant scopes both apply. Empty relations return empty results. An
unauthorized/missing parent or mount returns 404 without membership disclosure.

The existing detail renderer places the mounted dashboard in the parent page.
Drill opens the target resource's normal detail route when retrieval is allowed;
editing there follows existing CRUD permissions, not chart permissions.

## 3. Shared configuration primitives

Signatures in this document are contract notation. They name proposed interfaces;
examples are not expected to run against the current release.

### 3.1 Fields and dimensions

```text
Field(name)                         # raw numeric Y value; never implies Sum
Dim(field, *, bucket=None, split=False, include_empty=True, label=None)
```

A string in a dimension slot means `Dim(string)`. `Field` in a chart value slot
means one plotted value per source record, with implicit source identity. Do not
require the author to repeat the primary-key field. A bare Y string is rejected
because it does not declare raw versus aggregate intent.

`bucket` accepts `hour`, `day`, `week`, `month`, `quarter`, and `year` for temporal
fields, otherwise `None`. Weeks start Monday and include the year; quarters/months
also include the year. Use the validated effective IANA timezone before
truncation. Date-only values are calendar dates, not UTC instants. Naive mapped
datetimes follow the core's UTC-storage convention. Buckets use half-open ranges
and canonical start instants/dates, not formatted labels. Repeated DST hours
remain distinct by offset/instant.

`include_empty=False` removes records with an empty dimension before aggregation.
For scalar fields, empty means null or an empty string, not false or zero. For
collections, null and an empty collection are empty. With inclusion enabled,
empty kinds retain distinct typed keys. Unobserved select options or calendar
buckets are not automatically invented.

`split=False` groups a collection by its canonical complete set. `split=True`
contributes to each distinct member. Splitting an empty collection produces an
empty-member category only when `include_empty=True`. Reject `split=True` for
scalar fields. Splitting changes membership, not the source fact's value.

### 3.2 Measures

```text
Count(field=None, *, distinct=False, where=None, id=None, label=None, format=None)
Sum(field, *, where=None, id=None, label=None, format=None)
Avg(field, *, where=None, id=None, label=None, format=None)
Min(field, *, where=None, id=None, label=None, format=None)
Max(field, *, where=None, id=None, label=None, format=None)
Measure(operation, field=None, *, where=None, id=None, label=None, format=None)
```

All constructors produce the same immutable `Measure` representation. Common
constructors are shorthand, not separate execution paths. IDs default to
`count`, `sum_revenue`, etc.; duplicate normalized IDs require explicit overrides.
Measure-local predicates narrow only that measure, including its count/drill.
Counts use integer formatting; ratio summaries use percent formatting. Other
operations inherit compatible input formatting unless explicitly overridden.

Required operations are `count`, `count_distinct`, `sum`, `avg`, `min`, and `max`.
`Count()` counts distinct source records within each result cell/group;
`Count(field)` counts non-null field values, including empty strings and false;
`Count(field, distinct=True)` counts distinct non-null scalar values. Distinct
collection values require an explicit projected scalar source and cannot be
guessed from display strings.

Pivot summaries additionally support the following scalar operation contract as
**[O]**, without claiming an undocumented native menu. The installed executor
must either implement an advertised operation exactly or reject it during
compilation; discovery lists only implemented operations.

| Operation | Input | Defined result |
| --- | --- | --- |
| `empty`, `filled` | Any typed field | Record counts using that field's empty predicate. |
| `percent_empty`, `percent_filled` | Any typed field | Corresponding count divided by eligible record count. |
| `checked`, `unchecked` | Boolean | Count true or false; null belongs to neither. |
| `percent_checked`, `percent_unchecked` | Boolean | Corresponding count divided by eligible record count, including null records in the denominator. |
| `percent_unique` | Scalar | Distinct non-null values divided by eligible record count. |
| `range`, `median`, `stddev` | Numeric | Max minus min; exact interpolated median; population standard deviation. |
| `date_range_days` | Date/datetime | Elapsed days between extrema; dates use calendar days, instants use elapsed seconds / 86,400. |
| `date_range_months` | Date/datetime | Completed calendar months between extrema in the effective timezone, using an anniversary clamped to the destination month's last day. |

Use `Min`/`Max` for earliest/latest temporal values in pivots. Dates/text summary
outputs cannot be numeric chart Y values. Histogram is a visualization, not a
scalar pivot operation. File-size totals use `Sum` on an authorized numeric
projection. A portable executor may use a bounded exact fallback for statistical
operations; it must reject excess input rather than approximate silently.

### 3.3 Fixed predicates and viewer controls

```python
Q(status="Won") & (Q(revenue__gte=100) | Q(priority="High"))
~Q(region="North")
```

```text
Filter(field=None, *, id=None, kind="dropdown", choices=None,
       fields=(), targets=None, default=None, label=None)
```

`Q` is a Python facade over the existing filter AST, not another serialized
query language. Keyword conditions AND together; plain names mean equality;
suffixes use the scalar operators from `api-spec.md`, with `isnull=True/False`
as shorthand for null checks. `Q(field=None)` is a null check, not equality to
the string "null". Preserve SQL null semantics under negation.
Trusted fixed predicates may use published fields outside the viewer-filter
allowlist, but never outside the readable/aggregate-authorized dependency set.
Relationship/collection membership operations must be source capabilities;
unknown suffixes fail compilation.

| Filter kind | Configuration | Viewer value and behavior |
| --- | --- | --- |
| `dropdown` | One field; ID defaults to field name | A typed value or nonempty list of values; OR within the selection, AND with other controls. `[]` clears the filter; null explicitly selects the null group. Date range conditions use the conditions control. |
| `tabs` | Stable ID and `choices: Mapping[str, Q]` | One declared choice ID; null selects no tab predicate. Labels default to humanized choice IDs. Defaults must identify a declared choice. |
| `conditions` | Stable ID and explicit `fields` allowlist | A list of query pairs using the existing POST `:query` field/operator/group grammar. Only filter terms are accepted, not select/order/pagination/expand controls. |

`targets` is a tuple of widget IDs or group IDs, resolved at installation; default
is every widget in the same group. Dashboard-level controls require explicit
targets. A field binding must resolve on every target source with the same kind;
otherwise use separate controls. Filter IDs are dashboard-unique, prefixed by
group name when declared inside a group. Reuse resource `filter_fields` for
viewer fields; derived projections are not viewer-filterable unless explicitly
registered in that allowlist with supported operators.

Filter choices are authorized, bounded metadata; do not send every record to
construct them in the browser. Dropdown choice queries include mandatory scopes,
source/group restrictions, and other active controls, excluding their own
selection. For multiple targets, intersect their authorized choice domains.
Widget-local predicates do not alter a shared dropdown's domain. Predeclared tab
choices are not data-dependent and do not expose unauthorized record counts.

Each viewer's state is request/URL-local. It never mutates declarations or other
viewers' defaults. Unknown controls, undeclared targets, invalid types, or
forbidden field/operator pairs produce validation errors.

## 4. Aggregation and security invariants

Define a source fact as one authorized source-model primary key, even when the
selection query contains joins. For a widget, its eligible facts are the
intersection of:

```text
resource list policy + mandatory global/tenant guards
AND authorized parent relationship membership, when mounted
AND Source.where + Dashboard.where + Group.where
AND get_queryset() selection + widget.where
AND active viewer controls targeting the widget
AND included/nonempty dimension conditions
```

A measure's fact set additionally applies its `where`. Source/group predicates
are applicable only to that source; a dashboard-wide predicate over mixed
sources must validate against each group or fail installation. No viewer input,
custom renderer, or hook may remove a mandatory conjunct.

### 4.1 Correct totals and membership

For eligible facts `F`, let `R(f)` and `C(f)` be the row/column membership sets.
For each measure `m`:

```text
cell(r, c) = m({f in F | r in R(f) and c in C(f)})
row(r)     = m({f in F | r in R(f)})
column(c)  = m({f in F | c in C(f)})
grand      = m(F)
```

With no column dimension, `C(f)` has one implicit member. These equations include
measure-local predicates. Deduplicate primary keys within every evaluated set.
Both split axes create a membership cross-product, but never duplicate a fact
inside one cell. Group totals are not sums of visible cells when membership
overlaps. Averages use the original non-null count and sum; distinct, median,
and statistical totals are evaluated over their fact set, not child results.
`SUM(DISTINCT amount)` is not a fix for duplicate source identities.

Apply filters before aggregation and ordering/visible limits afterwards. A
budget may bound grouped results or exact fallback work; it may not limit source
records first and return a plausible final aggregate. Core has no implicit top-N
or Other bucket. Viewport changes do not alter totals or drill predicates.

### 4.2 Values, empty results, and formatting

- `Count()` on no facts is zero. `Sum`, `Avg`, `Min`, `Max`, range, median, and
  stddev on empty/all-null input are null. Percentage with a zero denominator is
  null. Population stddev of one non-null value is zero.
- Distinguish `state="noFacts"`, `state="allNull"`, and `state="value"` for
  every materialized cell/point. `allNull` means matching facts exist but the
  selected operation has no non-null input and returns null. Counts and other
  defined scalar results use `value`, including zero from actual facts.
  Rendering a dash or visual zero never changes the semantic value/state.
- Decimal values remain decimal through aggregation. JSON decimal values are
  strings. Integers outside JavaScript's safe range are strings; typed metadata
  identifies the number kind. Never emit NaN/Infinity or silently round a raw
  value to fit a JavaScript number.
- Dates are ISO dates; datetime values are UTC ISO instants. Bucket metadata
  includes its effective timezone and end boundary. Labels/localized formats
  never participate in identity, equality, sorting, or drill.
- Ratios are numeric fractions, not preformatted percentages. Rounding belongs
  to presentation except unavoidable nonterminating arithmetic: division and
  square roots use 38 significant decimal digits, round-half-even. Do not
  quantize sums or input values to display precision. A backend that cannot
  preserve decimal arithmetic must use a bounded exact-input Decimal fallback
  or reject the query; SQLite NUMERIC affinity is not an exact-arithmetic grant.
  Field `format` metadata is a typed object with `kind`
  (`number`, `currency`, `percent`, `duration`, `date`, `datetime`), optional
  `precision`, and optional `currency`. No executable formatters or HTML.

### 4.3 Permissions and consistency

Require `Policy.allow("list", ctx)` and the SQL list scope before aggregation.
Every dimension, measure input, source-order field, fixed condition, computed
dependency, and exposed collection target must satisfy readable-field and
`aggregate_fields(ctx)` checks. The latter is an explicit grant covering all
rows admitted by the list scope; object-specific masking alone is insufficient.
No grant means deny. `Count()` still requires resource list access even though
it has no numeric input field.
The internal primary key used only for identity/deduplication/tie-breaking is
not an exposed source-order field. Explicitly plotting or ordering by a private
field still requires its normal grants.

Descriptions/options may omit forbidden widgets/fields but must not disclose
their names, choice labels, SQL, raw policy predicates, or counts. A direct
unknown/inaccessible dashboard or widget yields 404. An access change after
discovery still prevents query/drill. Drill record fields additionally use
normal record serialization and per-object masking. Detail links require
retrieve permission independently of aggregate permission.

One widget execution evaluates filters at one captured time and uses a consistent
database read view for all its aggregates/totals. Implement this with one
statement or an appropriate read transaction; separate widgets/requests do not
promise a shared snapshot. `generatedAt` records execution time, not time travel.
Drill re-evaluates the saved selection against current data and permissions;
records may have changed after the chart query. Its response reports its own
timestamp. Do not promise stable multi-request snapshots.

Default responses are `Cache-Control: private, no-store`. A shared aggregate
cache is not required. Any later cache must include source/spec revision,
authorized scope, tenant, filters, timezone, and data invalidation identity;
equal widget IDs do not establish equal access.

## 5. Chart contract

```text
Chart(kind, *, x, y=Count(), by=None, stack="none", order="x",
      orientation="vertical", x_axis=None, left_axis=None, right_axis=None,
      colors="auto", legend="auto", labels=False, record_count="none",
      percent=False, zoom=False, **common)

Series(value, *, id=None, label=None, kind=None, axis="left", color=None)
Axis(*, label=None, scale="auto", zero=None, minimum=None, maximum=None, format=None)
```

`common` is exactly the following keyword set, also shared by Pivot/CustomChart;
it is not a renderer-options escape hatch:

| Keyword | Default | Contract |
| --- | --- | --- |
| `id` | Declaration name | Stable dashboard-local ID; an explicit override must be unique. |
| `title` | Humanized declaration name | Plain-text display title, also used for drill. |
| `subtitle` | `""` | Optional plain-text supporting label. |
| `description` | `""` | Plain-text context; also shown with drill. |
| `description_as_tooltip` | `False` | Put the description behind an accessible information control. |
| `where` | `None` | Additional trusted fixed predicate. |
| `height` | `"md"` | `sm`, `md`, or `lg`; responsive row sizing. |
| `drill` | `False` | False disables records; true normalizes to `Drill()`; otherwise a Drill declaration. |
| `allow_print` | `None` | Inherit the host's print setting; false narrows it. A widget cannot enable printing if the host disables it. |

### 5.1 Channels and series

`x` is a field name or `Dim`; `by` is an optional field name or `Dim`.
`y` is a Measure, Field, or nonempty sequence of Measure/Series declarations.
A singular Measure/Field becomes one primary Series. A Series inherits the
chart kind unless specified. All primary series use the chart kind; secondary
series may use bar, line, or scatter. At least one primary series is required.

| Chart | Aggregate Y | Raw Y | Additional rules |
| --- | --- | --- | --- |
| `bar` | Numeric Measure(s), including Count | Numeric Field(s) | Vertical/horizontal; grouped or compatible aggregate stacks. |
| `line` | Numeric Measure(s) | Numeric Field(s) | Preserve requested order; gaps for null values. |
| `scatter` | Numeric Measure(s) | Numeric Field(s) | X may be numeric, temporal, or categorical with the corresponding scale. Duplicate coordinates retain independent identity. |
| `pie`, `donut` | Exactly one numeric Measure | Not supported | X supplies categories; no by, stacking, axes, multi-series, or range zoom. |

Every series in a chart uses the same raw/aggregate mode. In aggregate mode,
the grain is `(x, by, measure)`; in raw mode it is `(source record, series)`.
`by` partitions every series and determines group colors. A legend entry is
identified by `(series ID, group member ID)`, not concatenated labels. Many
measures plus grouping is supported with the same grain and resource budgets;
there is no hidden downgrade or special five-series limit.

For raw records, scalar X is not collapsed; bucketing/splitting may intentionally
produce multiple displayed memberships for the same point identity. Aggregate
charts require explicit Measures and never infer an operation from field type.
A string category containing commas is not a collection.

### 5.2 Ordering, stacking, and axes

- `order` is `source`, `x`, `-x`, `y`, `-y`, `y:<seriesId>`, or
  `-y:<seriesId>`. Bare Y uses the first primary series. With grouping, rank an
  X category by the selected measure over that category's eligible facts,
  not by summing overlapping groups. Y ordering is not pie-slice percentage.
- Group ordering uses choice order for selects, numeric ordering for numbers,
  chronological order for dates, and Unicode code-point order for text. Empty
  members sort last. Ties use canonical typed keys and then source PK.
  Localization changes labels only. This deterministic text ordering is an
  OBOX default, not a claim about reference-product collation.
- `source` uses `Source.order`, otherwise resource default ordering, otherwise
  PK ascending. Append PK ascending as a tie-breaker. An aggregated group's
  position is its first fact's position in that authorized order. Validate order
  dependencies even if they are not plotted. Do not force line charts to an
  ordering that differs from their declaration.
- `stack="none"` is unstacked. `normal` stacks compatible bar series using
  separate positive/negative accumulators. `percent` uses each X/axis stack's
  nonnegative sum as its denominator; reject negative values with a validation
  error. Zero denominator yields null percentage. Raw-value stacking is not
  supported. All stacked series must use compatible units/format metadata.
- Percent stacks preserve raw values and record lineage; the plotted ordinate
  is the separately returned fraction. Group/legend visibility changes only
  display and does not silently change its denominator. Their axis automatically
  uses percent formatting; explicit incompatible formats fail validation. A
  percent-stack axis cannot also contain an unnormalized series.
- `Axis.scale` is `auto`, `category`, `linear`, or `time`, validated against the
  channel. Auto uses categorical bars, temporal lines/scatter for temporal X,
  linear scatter for numeric X, and categorical X otherwise. Y is numeric.
  `zero=None` means include zero for bar Y, automatic bounds for line/scatter;
  explicit true includes zero. Bounds must be finite and ordered; they do not
  filter records or change totals. Pie/donut axis arguments fail compilation.
- `orientation="horizontal"` changes bar presentation, not semantic X/Y keys.
  It is invalid on other chart kinds. Two Y axes retain independent labels,
  formatting, and scales; measures sharing an axis require compatible units.

### 5.3 Presentation and interactions

`colors` accepts `auto`, `field`, a palette name registered by the renderer, or
an explicit mapping of member/series IDs to validated colors. `field` uses select
choice colors from `by`, otherwise X. Auto prefers declared choice colors and
falls back to a stable hash-based palette. Filtering must not recolor surviving
identities. Unavailable palettes or field colors fail configuration.

`legend` is `auto`, `hidden`, `left`, `right`, `top`, or `bottom`. Auto shows a
right legend for pie/donut or multiple visible series/groups; otherwise hides
it. `labels=True` shows formatted values. `record_count` is `none`, `chart`
(bar/line/scatter), or `legend` (pie/donut). `percent=True` adds pie/donut
percentages. Reject combinations that have no defined meaning.

Hover/focus shows formatted value, category/group/series identity, and an
authorized record count when enabled. Tooltip content is text, never raw HTML.
Pie/donut values must be nonnegative; an all-zero/empty input shows an empty
chart with its accessible table. Zero-valued categories remain in the result.

`zoom=True` enables range selection and reset for line/scatter. It changes the
viewport of the complete returned dataset only; it does not recompute aggregates
or percentages. A click/keyboard action drills a point, segment, or slice when
enabled. Legend drill selects that entry across all X categories; expose a
keyboard-accessible action in addition to the reference extension's double-click.
The selector identifies the measure and group. Coincident raw points remain
available through a chooser or the data table.

## 6. Pivot contract

```text
Pivot(*, rows, columns=None, value=Count(), row_order="group",
      column_order="group", show_row_label=True, show_column_label=True,
      show_row_totals=True, show_column_totals=True, show_grand_total=True,
      fill="dash", expand=True, format=None, **common)
```

- `rows` is exactly one field/Dim and is required. `columns` is one field/Dim
  or None. `value` is one Measure. Reject sequences of axes or measures rather
  than accepting undeclared hierarchy behavior.
- `row_order` and `column_order` independently accept `group`, `-group`,
  `value`, and `-value`. Group ordering follows section 5.2. Value ordering
  uses the independently evaluated axis total, with stable group ties.
- The two label switches control field labels, not member labels. Row totals
  appear at the right, column totals at the bottom, and grand total at their
  intersection when enabled. With no columns, render one value column and
  suppress the redundant right total; the optional bottom grand total remains.
  Hiding any total never changes sorting or other numeric results.
- `fill="dash"` displays missing/no-value cells as a dash; `null` leaves an
  empty display; `zero` displays zero only for no-fact intersections. An all-null
  matching set remains null/dash. Preserve the semantic value/state in all cases.
- Pivot `format` overrides measure presentation only. Date/text summaries remain
  typed values. The renderer cannot compute totals from formatted strings.
- `expand=True` provides fullscreen and cell expansion. Expansion preserves the
  result's query, ordering, and values. Drill can be disabled independently.
  Cell, row total, column total, and grand total each have their own selection.

No client-side total recomputation, automatic extra subtotal levels, conditional
formatting language, or export subsystem is required by this core contract.

## 7. Drill and runtime services

```text
Drill(*, fields=None, order=(), filter_fields=(), sort_fields=(), search=False,
      page_size=50, details=True, allow_print=False)
```

```python
class Analytics:
    def __init__(
        self,
        *dashboards: type[Dashboard],
        prefix="/analytics",
        max_rows=200,
        max_columns=50,
        max_cells=10_000,
        max_chart_groups=200,
        max_pie_groups=50,
        max_points=10_000,
        timeout_ms=3000,
        renderers=(),
    ): ...

    async def describe(self, *, ctx: Context, dashboard=None, context=None): ...
    async def query(self, dashboard, widget, *, ctx: Context, state=None): ...
    async def options(
        self, dashboard, filter_id, *, ctx: Context, state=None, search="", offset=0, limit=50
    ): ...
    async def records(
        self,
        dashboard,
        widget,
        selector,
        *,
        ctx: Context,
        offset=0,
        limit=50,
        order=(),
        conditions=(),
        search=None,
    ): ...
```

These methods return the typed descriptions/results below and raise existing
OBOX errors; no public `compile()/execute()` ceremony is required. A private
compiler validates/binds declarations during installation and dynamic
replacements during execution. The query service uses the existing resource
service's authorized selection and serializer; it owns no independent session
or transaction. Trusted non-HTTP callers use the same Context/policy checks.

`state` is `QueryState(filters={}, timezone=None, context=None, revision=None)`.
Omission uses server-declared defaults. Effective timezone precedence is request
state, nearest non-null group/dashboard declaration, then UTC. Invalid IANA zones
are rejected. Locale comes from Context/header negotiation. `revision` is an
optional optimistic check of the compiled public definition; a mismatch returns
409 and requires
rediscovery. Keep one wire `schemaVersion=1`; do not introduce separate semantic,
query, and result version systems.

`Drill.fields=None` inherits permitted fields from the resource list display.
Explicit fields are a narrowing projection. `order` defaults to PK ascending;
configured/requested ordering must also be resource-sortable and readable.
`filter_fields`/`sort_fields` default empty, disabling viewer record filtering
and sorting. Enabling them cannot extend resource allowlists. Record conditions
and optional search further narrow a selector; they cannot affect the chart.

Every result selection is a server-issued, signed opaque selector with a
five-minute expiry. Bind it to dashboard/widget definition revision, series,
typed membership predicate, filter state, effective timezone, parent context,
principal/tenant identity, and selected scope (point, legend, cell, or total).
Resolve it through the registered definition; never serialize SQL, secrets, or
policy predicates into it. Reuse framework signing machinery, not a new token
store or security subsystem. Selectors confer no access: every request reruns
authorization and source/relationship scopes. Reject tampering, expiry, principal
changes, or definition changes. A raw-point selector identifies a source record;
an aggregate selector identifies its contributing predicate.

Records are unique by source identity and paginated using offset/limit with a
stable PK tie-breaker. Default page size is 50; maximum is 200. Return `hasMore`
using one extra row, not an unbounded exact count. There is no cross-request
snapshot guarantee. `details=False` omits links; true still checks retrieval per
record. `allow_print` additionally requires the host/widget print setting.

### 7.1 Budgets and failure behavior

| Budget | Default | Meaning |
| --- | --- | --- |
| `max_rows` / `max_columns` | 200 / 50 | Observed pivot members on each axis. |
| `max_cells` | 10,000 | Pivot rectangular shape, including absent intersections. |
| `max_chart_groups` | 200 | Each chart category/group/expanded-series domain. |
| `max_pie_groups` | 50 | Pie/donut categories. |
| `max_points` | 10,000 | Plotted chart rows, custom dataset rows, and exact statistical fallback input. |
| `timeout_ms` | 3,000 | Whole widget/options/drill execution budget. |
| Viewer conditions | Depth 3; 100 nodes | Shared core parser limits; no second filter language. |
| Query state | 32,000 characters | Maximum encoded/decoded state length, checked before parsing. |

All numeric limits are positive integers, excluding bools. Count/check bounds
without fetching unlimited records. Push supported aggregation to SQL. Enforce
execution time at the database/driver boundary and release/cancel work on
disconnect; PostgreSQL statement timeout alone does not implement SQLite's
budget. Do not claim a timeout if only the browser request was abandoned.

A violation fails the widget with a stable error; it does not return partial or
approximate values. Options and drill pagination are explicit bounded record
windows, not incomplete aggregates. One failing widget does not block other
widgets; the dashboard does not share an AsyncSession concurrently across tasks.

## 8. HTTP interface

All routes are relative to configurable `Analytics.prefix`, default `/analytics`,
and have no trailing slash. This is the analytics namespace, not a new resource
collection under `/api`. JSON routes use the existing ApiRequest/ApiResponse,
Problem, authentication, trace headers, and permission rules, including bearer
scope restrictions when applicable. Read-only POST queries follow the framework's
read-operation CSRF rules and cause no persistent state changes.

| Method | Default path | Output |
| --- | --- | --- |
| GET | `/analytics:describe` | Accessible standalone dashboard summaries. |
| GET | `/analytics/{dashboard}` | Standalone dashboard HTML. |
| GET | `/analytics/{dashboard}:describe` | Authorized compiled dashboard description. |
| GET, POST | `/analytics/{dashboard}/{widget}/data` | ChartResult, PivotResult, or CustomResult JSON. |
| GET, POST | `/analytics/{dashboard}/{widget}/records` | RecordsResult JSON; GET also supports the existing HTML drill page. |
| GET, POST | `/analytics/{dashboard}/filters/{filter}/options` | Authorized, paginated dropdown choices. |

Specific discovery routes must precede dynamic routes; reject collisions at
installation. Mark JSON operations with concrete response models in OpenAPI.
Discovery provides resolved URLs, respecting mount/root-path/localization;
frontend code does not construct paths from class names.

### 8.1 Requests

POST data uses the complete QueryState body; every property is optional:

```json
{
  "filters": {"region": ["north"], "pipeline": "won"},
  "timezone": "Asia/Bangkok",
  "context": null,
  "revision": "definition-digest"
}
```

Mounted requests use a context registered on the parent View:

```json
{
  "context": {
    "resource": "customers",
    "recordId": "customer-42",
    "mountId": "sales"
  }
}
```

`RelatedContext` has exactly those three required string properties. The resource
is its public collection slug; record IDs use the core resource format. Context
is not an arbitrary relationship path/source override. A mount-only definition
without context fails with 422 `context_required`; a forbidden binding gives 404.

GET data accepts only `state`, containing one URL-encoded JSON QueryState;
omission means `{}`. GET description accepts optional `context` containing an
encoded RelatedContext. Do not accept a GET body. Do not also accept legacy
`filter.*`, `filter_op.*`, `ordering`, row/column filters, SQL, field lists, source
names, aggregation definitions, or arbitrary renderer options. Duplicate query
control parameters, unknown JSON properties, and oversized state fail validation.

Condition filter values reuse query pairs, with public field aliases:

```json
{
  "filters": {
    "advanced": [
      ["createdAt", "gte.2026-10-01T00:00:00Z"],
      ["or", "(region.eq.north,region.eq.south)"]
    ]
  }
}
```

POST records has required `selector` and optional `offset=0`, `limit=50`,
`order=[]`, `conditions=[]`, and `search=null`. Public ordering terms and
conditions follow the core grammar and the Drill allowlists. Its selector
already binds chart filters/context; clients cannot replace them.

```json
{"selector": "opaque-signed-selection", "offset": 0, "limit": 50}
```

GET records uses the same controls as single query parameters; `order` is
comma-separated and `conditions` is encoded query-pairs JSON. Explicit
`Accept: application/json` selects JSON; GET otherwise returns the HTML list.
POST always returns JSON. Headers/URLs must never contain raw record lists.

POST options accepts QueryState properties plus `search=""`, `offset=0`, and
`limit=50`, capped at 200. GET accepts its encoded body as `state`. Only dropdown
controls have this endpoint; tab choices are declared metadata. Selected values
must be revalidated independently of whichever options page the client fetched.

### 8.2 Descriptions

The index returns `{"schemaVersion":1,"dashboards":[...]}`; each summary has
`id`, `title`, `description`, `url`, and `describeUrl`. A dashboard description has
these required fields:

| Property | Type / meaning |
| --- | --- |
| `schemaVersion`, `revision` | Integer 1 and opaque definition digest. |
| `id`, `title`, `description` | Stable ID and plain-text presentation. |
| `contextRequired`, `timezone`, `columns`, `allowPrint` | Effective host configuration. |
| `groups` | Ordered GroupDescription objects: ID, title, width, background, columns, widgets. Direct widgets use an implicit group. |
| `filters` | FilterDescription objects: ID, kind, label, targets, default, input JSON Schema, options URL or tab choices. |

Each WidgetDescription contains common presentation/actions, `id`, `kind`,
`dataUrl`, `recordsUrl`, typed dimension/measure descriptors, effective order,
and the validated chart/pivot presentation configuration from sections 5/6.
Its shape is a Pydantic discriminated union. A CustomChart includes its renderer
name and validated renderer spec. No descriptor contains fixed predicates,
SQLAlchemy expressions, authorization rules, or forbidden fields. `drill=false`
omits records actions/URLs and all result selectors.

### 8.3 Result types

All successful data results have `schemaVersion=1`, `revision`, `widgetId`, a
`kind` discriminator, and this required `meta` object:

```json
{
  "generatedAt": "2026-11-11T10:00:00Z",
  "timezone": "Asia/Bangkok",
  "complete": true,
  "traceId": "request-correlation-id",
  "warnings": []
}
```

`complete` is always true for successful core queries; incomplete execution is
an error. Warnings have stable string codes, e.g. `non_additive_membership`.
These fields do not establish a persistent snapshot or expose query text.

Reusable wire types:

| Type | Required properties |
| --- | --- |
| `DimensionDescription` | `field` (public alias), `kind`, `multiple`, `split`, `bucket` (nullable), `label`, `includeEmpty`. Temporal buckets also provide timezone/boundary semantics. |
| `MeasureDescription` | `id`, `label`, `operation`, `field` (nullable public alias), `valueType`, `format` (nullable). |
| `Member` | `id` (stable opaque member ID), `value` (typed scalar or canonical scalar set), `label`. Bucket members also carry canonical start/end. |
| `Datum` | `value` (typed scalar/null), `state` (`value`, `allNull`, `noFacts`), `recordCount` (encoded integer), `drill` (nullable selector). |
| `Page` | `offset`, `limit`, `hasMore`. |

Member IDs are bound to field/type/bucket identity and remain stable across
filter/order/locale changes for the same definition. `value=null`, `value=""`,
and `value=[]` remain distinct. Decimal/integer strings are distinguished by
their declared type, not guessed by a renderer. Explicit series/widget/filter
IDs are dictionary identities and are not mechanically camel-cased.

#### ChartResult

`kind="chart"` has `mode="aggregate"|"raw"`, `dimensions={x,group}` (group
nullable), `series`, `members={x:Member[],group:Member[]}`, and ordered `rows`.
Each series extends MeasureDescription with `kind`/`axis`; a raw series uses
`operation="raw"` and its source field/type. Each row extends Datum with
`x` (member ID), `group` (nullable member ID), `seriesId`, `pointId` (nullable),
and `percentage` (nullable ratio). Raw `pointId` is an opaque stable point
identity, not an exposed primary key. Aggregate point identity is its key tuple.

```json
{
  "schemaVersion": 1,
  "revision": "rev1",
  "widgetId": "revenue",
  "kind": "chart",
  "mode": "aggregate",
  "dimensions": {
    "x": {"field":"region","kind":"text","multiple":false,"split":false,"bucket":null,"label":"Region","includeEmpty":true},
    "group": null
  },
  "series": [
    {"id":"sum_revenue","label":"Revenue","operation":"sum","field":"revenue","valueType":"decimal","format":null,"kind":"bar","axis":"left"}
  ],
  "members": {"x":[{"id":"north","value":"North","label":"North"}],"group":[]},
  "rows": [
    {"x":"north","group":null,"seriesId":"sum_revenue","pointId":null,"value":"30.00","state":"value","recordCount":2,"percentage":null,"drill":null}
  ],
  "meta": {"generatedAt":"2026-11-11T10:00:00Z","timezone":"UTC","complete":true,"traceId":"tr1","warnings":[]}
}
```

Rows are one measure/member contribution, not Chart.js/Vega options. Empty
charts have empty rows/member domains and complete metadata. A renderer may
densify missing combinations using explicit no-fact semantics, never by
inventing source records or merging coincident raw points.

#### PivotResult

`kind="pivot"` has `rowDimension`, `columnDimension` (nullable), `measure`,
ordered `rows`/`columns` Member arrays, sparse `cells`, and `totals`. Each cell
extends Datum with `row` and nullable `column` member IDs. Missing intersections
have no facts: Count is zero; other aggregate values are null. An all-null
matching set is represented explicitly. No-column mode has `columns=[]` and
`column=null` in each cell.

`totals` always contains `rows` and `columns` arrays plus `grand` Datum. Axis
total entries extend Datum with their `row` or `column` ID. In no-column mode
`totals.columns=[]`; grand is the bottom summary. Totals are returned even when
hidden, because value sorting and full-screen display use the same computation.
The renderer never derives them from `cells`. Include
`non_additive_membership` when split memberships overlap.

For example, a one-row, no-column pivot with drill disabled has this complete
shape (IDs are illustrative):

```json
{
  "schemaVersion": 1,
  "revision": "rev1",
  "widgetId": "matrix",
  "kind": "pivot",
  "rowDimension": {"field":"region","kind":"text","multiple":false,"split":false,"bucket":null,"label":"Region","includeEmpty":true},
  "columnDimension": null,
  "measure": {"id":"sum_revenue","label":"Revenue","operation":"sum","field":"revenue","valueType":"decimal","format":null},
  "rows": [{"id":"north","value":"North","label":"North"}],
  "columns": [],
  "cells": [
    {"row":"north","column":null,"value":"30.00","state":"value","recordCount":2,"drill":null}
  ],
  "totals": {
    "rows": [{"row":"north","value":"30.00","state":"value","recordCount":2,"drill":null}],
    "columns": [],
    "grand": {"value":"30.00","state":"value","recordCount":2,"drill":null}
  },
  "meta": {"generatedAt":"2026-11-11T10:00:00Z","timezone":"UTC","complete":true,"traceId":"tr1","warnings":[]}
}
```

#### RecordsResult and OptionsResult

RecordsResult has `schemaVersion`, `revision`, `widgetId`, `kind="records"`,
`items`, `page`, and `meta`. Each item has `id`, `values`, and nullable
`detailUrl`. Generate the concrete `values` schema
from the authorized resource projection and use ordinary per-record masking.
Record IDs follow the core API encoding. Do not embed bulk fact-lineage ID lists
into aggregate HTML/results; authorized dimension values can themselves be
record references without granting access to those records.

OptionsResult has `schemaVersion`, `revision`, `filterId`, `kind="options"`,
`items`, `page`, and `meta`; each item has
typed `value` and plain-text `label`. Neither response includes SQL or an
unbounded list of candidate IDs. Both use the same metadata/number conventions.
For these paginated responses, `meta.complete=true` means the requested window
completed; `page.hasMore` states whether more candidates exist, without claiming
the window is the entire source.

### 8.4 Errors

Raise `ImproperlyConfigured` with a declaration path for invalid static fields,
sources, collection policies, kinds, axes, measure types, duplicate IDs,
unsupported operations, hooks, or renderer capabilities. Fail installation
before serving a partially configured analytics subsystem. Dynamic equivalents
fail the affected request.

JSON failures use the existing Problem model, including status, code, detail,
traceId, and field errors. Do not introduce an ErrorResult success envelope.

| Status | Code | Meaning |
| --- | --- | --- |
| 401 | Existing authentication code | Authentication is required. |
| 404 | `not_found` | Unknown/inaccessible dashboard, widget, parent, or mount. |
| 403 | `permission_denied` | Previously available action/selection is no longer permitted. |
| 409 | `definition_changed` | Submitted revision/selector refers to an obsolete definition. |
| 422 | `validation_error`, `unknown_field`, `unsupported_operator` | Malformed state or invalid/forbidden viewer term. |
| 422 | `context_required`, `invalid_selector`, `unsupported_aggregation`, `negative_chart` | Missing mount context, bad/expired selector, unavailable operation, or negative pie/percent-stack input. |
| 422 | `aggregate_limit` | Group/cell/point/fallback budget exceeded. |
| 503 | `aggregate_timeout`, `source_unavailable` | Execution deadline or database/source failure. |

Use generic errors for selector tampering/expiry without revealing embedded
identity or predicates. Error details must not include SQL, tokens, credentials,
raw policy data, or traceback text. Exceptions remain server-observable by trace ID.

## 9. Custom chart extension point

```text
CustomChart(renderer, *, fields, spec, **common)
```

```python
from obox_builder.contrib.analytics import CustomChart
from obox_builder.contrib.analytics.vega import VegaLite


class ExplorationDashboard(Dashboard):
    source = Sale
    comparison = CustomChart(
        "vega_lite",
        fields=("region", "revenue", "profit"),
        spec={
            "facet": {"column": {"field": "region", "type": "nominal"}},
            "spec": {
                "layer": [
                    {
                        "mark": "point",
                        "encoding": {
                            "x": {"field": "revenue", "type": "quantitative"},
                            "y": {"field": "profit", "type": "quantitative"},
                        },
                    },
                    {"mark": "rule", "encoding": {"y": {"datum": 0}}},
                ],
            },
        },
    )


analytics = obox.install(Analytics(ExplorationDashboard, renderers=(VegaLite(),)))
```

`VegaLite` is an optional renderer adapter exported by
`obox_builder.contrib.analytics.vega`. Installing the ordinary Analytics plugin
does not load or require Vega assets. Using an unregistered renderer fails
installation. No Python equivalent of every Vega-Lite grammar operator is added.
The adapter supports the grammar's marks, transforms, layer, facet, concatenate,
and repeat within its declared schema version and authorized dataset boundary.
See the official [composition grammar](https://vega.github.io/vega-lite/docs/composition.html)
and [named datasets](https://vega.github.io/vega-lite/docs/data.html#named).

`fields` is an explicit, nonempty source projection, subject to the same fixed
filters, context, budgets, and field permissions as standard charts. `spec` is
developer-authored JSON-compatible data, not viewer-supplied code. Field
references use the source's public aliases; validate them against the projection
and renderer JSON Schema. Source fields need read access, not merely permission
to aggregate them. Raw Field charts likewise apply per-record read policy. If a
configured channel/projection cannot be released for a selected record, fail
the widget with `permission_denied` instead of leaking it or silently replacing
it with a value that changes the analysis.

Bind the root dataset to the reserved name `source`; authors can omit its `data`
property. Nested data may reference `source` or explicitly declared static
datasets validated at installation. Reject replacement of `source`, unknown
dataset references, and external data URLs, including nested lookup/layer
sources. Disable external navigation/image requests by default. Supply a
restricted loader at renderer construction, before any load can occur, as
required by [Vega's View API](https://vega.github.io/vega/docs/api/view/#view_loader).
Use the renderer's restricted expression support; do not eval arbitrary JS,
inject HTML, or register request-provided functions.

`CustomResult` has `schemaVersion`, `revision`, `widgetId`, `kind="custom"`,
`renderer`, authorized field descriptions, `rows` of `{pointId, values}`, and
the usual `meta`. `values` has a concrete typed projection schema. The complete
authorized dataset must fit `max_points`; never run browser aggregates over a
silently truncated page. Numeric raw values retain canonical encoding alongside
any plotting conversions. Custom presentation transforms may use browser
arithmetic; they are not authoritative OBOX Measure results. Use server-computed
projections/measures for exact financial/statistical results.

For core custom charts, `drill` must be false. Reconstructing arbitrary transform
lineage is not promised by the reference Vega-Lite extension and is not silently
emulated. A later adapter may advertise a separately specified drill capability;
it must retain the same server authorization boundary.

Renderer registration is a trusted server/plugin concern. A Renderer adapter has
a unique `name`, pinned `spec_schema`, packaged `assets`, and
`validate(spec, fields) -> None`. Its browser module implements
`mount(element, spec, result) -> handle`, where `handle.update(spec, result)`
replaces data and `handle.destroy()` releases listeners, views, and resources.
Renderer assets are versioned and locally served; a custom renderer cannot
replace the dataset query service or supply arbitrary asset URLs through HTTP.

## 10. UI behavior

- Render independently loading widgets with accessible titles and status text.
  Empty data, zero values, validation failure, timeout, and renderer failure are
  distinguishable. A renderer failure leaves the authorized data table usable.
- Every standard chart has a semantic data-table alternative with categories,
  groups, measures, original values, and permitted drill actions. Custom charts
  expose their projected dataset and textual description. Do not rely on color
  alone; labels, focus states, and keyboard actions convey identity.
- Make description tooltips, legend actions, zoom reset, fullscreen, and cell
  drill keyboard-operable. Fullscreen restores focus on close. Touch users can
  reach the same actions without hover/double-click.
- Show only the controls/actions present in the authorized description, but
  keep server authorization mandatory. Header labels use localization while
  number/date formatting follows effective locale/timezone.
- Rapid filter/context changes cancel in-flight work and ignore responses from
  older generations. Clear old data while loading so it is not presented under
  a new filter label. Destroy old chart instances and custom handles on removal.
- Use bounded request concurrency and fresh contexts/sessions per HTTP request.
  Do not use one SQLAlchemy AsyncSession concurrently for sibling widgets.
- Responsive rows share height; pivots can scroll/virtualize their already
  bounded full result. Fullscreen/virtualization never changes its fact set.
- Print controls invoke browser printing for permitted rendered widgets or the
  current drill page. Include titles, filters, timezone, generated timestamp,
  and table alternatives. Printing does not enable CSV/PNG/XLSX export, promise
  fixed page breaks, or prevent a user from printing already delivered content.

## 11. Worked examples and expected results

All examples below assume an explicitly authorized fixture model/source,
including permissions for any fixed-filter and ordering fields used. They are
acceptance fixtures, not test files or requirements to change application models.

### 11.1 Grouped, stacked, and percentage bars

Fixture S uses Decimal revenue/profit, text region/distributor, and a set-valued
tags field:

| ID | Region | Distributor | Tags | Revenue | Profit |
| --- | --- | --- | --- | ---: | ---: |
| r1 | North | Viettel | A, B | 10.00 | 2.00 |
| r2 | North | FPT | A | 20.00 | 4.00 |
| r3 | South | Viettel | B | 5.00 | 1.00 |
| r4 | South | FPT | empty set | 15.00 | 3.00 |

```python
revenue = Chart(
    "bar",
    x="region",
    y=Sum("revenue"),
    by="distributor",
    stack="normal",
    drill=True,
)
```

Expected values are North/Viettel=10, North/FPT=20, South/Viettel=5, and
South/FPT=15. Region totals are 30 and 20; the fact-set total is 50. Switching to
`stack="percent"` returns fractions 1/3 and 2/3 for North, 1/4 and 3/4 for South,
while raw values remain unchanged. Segment drill selects r1/r2/r3/r4 respectively;
Viettel legend drill selects r1+r3 across X categories. Hiding FPT does not
change North/Viettel's original denominator. A zero stack has null fractions;
a negative input produces `negative_chart` for percent stacking.

### 11.2 Multiple measures, two axes, and circles

Assume the source also publishes an authorized decimal `margin` projection
equal to profit/revenue and corresponding percent format metadata.

```python
performance = Chart(
    "bar",
    x="region",
    y=(
        Sum("revenue"),
        Sum("profit"),
        Series(Avg("margin"), kind="line", axis="right"),
    ),
    right_axis=Axis(label="Margin", zero=True),
)
composition = Chart(
    "donut",
    x="region",
    y=Sum("revenue"),
    legend="bottom",
    record_count="legend",
    percent=True,
)
```

Performance returns revenue/profit 30/6 for North and 20/4 for South; both right
axis averages are 0.2. Left monetary measures and the right ratio retain separate
formats/axes. The donut returns North=30 and South=20 with fractions 0.6 and
0.4 and record counts 2 each. A single-series pie is the same query with a
different mark. A five-primary-plus-one-secondary declaration must also work
when its configured fields/types/units satisfy these rules and budgets.

### 11.3 Raw records, source order, and time

```python
correlation = Chart("scatter", x="cost", y=Field("revenue"), zoom=True, drill=True)
raw_trend = Chart("line", x="sequence", y=Field("score"), order="source")
```

Two authorized records with identical `(cost, revenue)=(8,10)` produce two raw
points with different point IDs and independent drill selectors. They remain
available through the data table even when marks overlap. No extra `detail="id"`
argument is needed. A Source ordered as sequence 3,1,2 must preserve that order
for `raw_trend`; `order="x"` produces 1,2,3 instead. Aggregate source ordering
uses each category's first source position, not its text label.

For `Dim("created_at", bucket="month")`, instants `2026-01-31T17:30:00Z` and
`2026-02-01T00:30:00Z` share February in Asia/Bangkok but belong to January and
February in UTC. New York instants `2026-11-01T05:30:00Z` and
`2026-11-01T06:30:00Z` occupy different hourly buckets during the repeated local
hour. A Monday-based week containing 2027-01-01 starts 2026-12-28, not a separate
week identified only by month/year display text.

### 11.4 Split pivot and non-additive totals

Fixture P is r1, r2, r3 from Fixture S; r4 is not part of this fixture.

```python
matrix = Pivot(
    rows=Dim("tags", split=True),
    columns="region",
    value=Sum("revenue"),
    row_order="-value",
    drill=True,
)
```

| Tags | North | South | Row total |
| --- | ---: | ---: | ---: |
| A | 30.00 | — | 30.00 |
| B | 10.00 | 5.00 | 15.00 |
| Column total | 30.00 | 5.00 | 35.00 |

Grand total is 35, not 45. Cell A/North drills to r1+r2; B/North to r1;
B/South to r3; North total to r1+r2; grand to r1+r2+r3. Changing to `Avg`
produces A=15, B=7.5, grand=35/3, not the unweighted mean of row means.
`Count("region", distinct=True)` has grand=2, regardless of repeated North cells.

With `split=False`, r1 belongs to composite category [A,B], r2 to [A], and r3
to [B]. [B,A] is the same composite as [A,B]. If a fact has tags [A,B] and
territories [North,South], splitting both axes puts it into four cells, once per
cell and once in grand. Duplicate items in either collection do not multiply it.

`Pivot(rows="distributor", value=Sum("revenue"), row_order="-value")` over P
has one value column, FPT=20 then Viettel=15, with no duplicate right-total column.
Hide/show flags do not change those values or their ordering.

### 11.5 Empty values and safe source scoping

Null, empty string, empty set, false, and zero use different typed identities.
An absent cell has Count=0/Sum=null and `noFacts`. A cell containing one record
whose numeric value is null has Sum=null, `allNull`, and recordCount=1. A cell
containing a real zero has Sum="0" and `value`. `fill="zero"` affects only the
first case. `Count(field)` counts an empty string; `filled` does not. On no
eligible records, counts are zero and percentage summaries are null.

For a customer-mounted SalesDashboard, opening customer-42 selects only its
readable related sales even if other customers match the same region filter.
Changing the request to a forbidden parent yields 404. A source relationship
pointing to another tenant never widens the target policy. A mount-only
dashboard queried without context fails. Revoking access between query and drill
prevents access despite a valid selector. Fixed predicates, viewer controls, and
measure-local conditions compose by intersection.

## 12. Traceability and acceptance matrix

R IDs refer to linked official sources in section 1; O means an explicit library
guarantee rather than a claim about native behavior. Each row is a required
acceptance scenario for a later implementation, not a request to add test files
as part of this documentation change.

| ID | Evidence / surface | API entry | Required observable outcome |
| --- | --- | --- | --- |
| H01 | R1: dashboard composition | Dashboard, nested Group | Multiple sources coexist without cross-source filter leakage. |
| H02 | R2/R3: table/view selection | Source, source order/where | Reusing a source preserves fixed conditions/order; unrelated UI view changes do not affect it. |
| H03 | R4: linked-record chart | View.analytics, Related | Query and every drill are parent-membership scoped. |
| H04 | R5: independent element sources | Separate source groups | Charts may use different registered models; their filter bindings are explicit. |
| H05 | R1/R4: presentation/context | Common options, Group settings | Titles, descriptions/tooltips, subtitles, width/background, and shared row heights render consistently. |
| H06 | O: declarations and hooks | Dashboard hooks, inference | Inheritance/override/removal order is stable; request hooks do not mutate shared classes or bypass policy. |
| C01 | R1/R2/R4/R5: chart marks | Chart.kind | All five kinds bind/query/render, including horizontal bars. |
| C02 | R2: record plotting | Field, raw mode | Coincident records retain separate identities; raw line/bar values are not silently aggregated. |
| C03 | R1/R2: value selection | Count/Sum/Avg/Min/Max | Default count and each numeric operation produce the expected typed value. |
| C04 | R2: date granularity | Dim.bucket | Hour/day/week/month/quarter/year remain correctly ordered and distinguish years/timezones. |
| C05 | R2: empty categories | Dim.include_empty | Null/empty inclusion is independent of false/zero and absent intersections. |
| C06 | R1/R2: grouping | Chart.by, Dim.split | Each group has stable color/identity and correct record membership. |
| C07 | R1/R2: stacking | Chart.stack | None/normal/percent work with correct zero/negative handling and original values. |
| C08 | R1: multiple series | Chart.y, Series | Five primary plus one secondary fit; unsupported types fail explicitly. |
| C09 | R1: mixed marks/two axes | Series.axis/kind, Axis | Independent scale/format; primary kind consistency; secondary mixed mark. |
| C10 | R2: three sorting modes | Chart.order, Source.order | Source/X/Y sorting, directions, and ties follow section 5.2; lines are not forcibly reordered. |
| C11 | R2: zero baseline | Axis.zero/bounds | Scaling changes display only and retains negative/raw values. |
| C12 | R4/R5: colors and legends | colors, legend | Choice colors, palettes, positions/hidden legend, and filter-stable identity. |
| C13 | R4: count/percentage labels | record_count, percent, labels | Record counts and value percentages use the correct denominator and display location. |
| C14 | R1/R2: data inspection | Tooltip, Drill | Point/segment/slice drills match their contributing facts; tooltips cannot inject HTML. |
| C15 | R2: legend drill | Legend selection | Selected group/measure across X drills correctly; keyboard/touch equivalents exist. |
| C16 | R2: range exploration | zoom/reset | Line/scatter viewport changes preserve the underlying query and percentages. |
| C17 | R2/R5: field classes | Source metadata, DataField | Text/select/users/links/time/numeric/computed equivalents are type-checked; untyped objects rejected. |
| P01 | R1/R3: row/column axes | Pivot.rows/columns | Row required; optional column gives one value column, not an empty pivot. |
| P02 | R1/R3: summaries | Pivot.value | Count and typed field summaries; only executor-supported operations are advertised. |
| P03 | R1/R3: independent ordering | row_order, column_order | Group/value asc/desc use independent, correct axis totals and deterministic ties. |
| P04 | R1: label/total controls | Pivot show_* switches | Labels and right/bottom/grand totals toggle without altering data/order. |
| P05 | R1/R3: collection grouping | Dim.split | Composite and split select/reference/user sets work on one/both axes. |
| P06 | O: split total arithmetic | Fact-set aggregation | Fixture P produces grand=35; overlapping cells do not double-count totals. |
| P07 | O: non-additive functions | Avg/distinct/statistics | Weighted/exact fact-set totals; no average/sum of child summaries. |
| P08 | R1: expansion | Pivot.expand | Cell/fullscreen expansion preserves values, sort, and active filters. |
| P09 | R1: underlying records | Drill on cell/totals | Each selection returns the correct unique contributing record set. |
| F01 | R1/R5/R7: fixed restrictions | Q, where, get_queryset | Host/source/widget restrictions cannot be cleared by viewer controls. |
| F02 | R1/R5/R7: viewer controls | Filter dropdown/tabs/conditions | Declared targets update; independent viewers do not affect each other's state. |
| F03 | R5: viewer-specific records | Policy and context-aware hooks | User/group membership restrictions remain server-enforced. |
| F04 | R1/R7: combined filters | Shared filter AST | Multiple controls and nested groups intersect; invalid fields/operators/depth fail. |
| F05 | O: choice safety | Analytics.options | Options/search/pages reveal only authorized choices across bound targets. |
| D01 | R1: record inspection | Drill.fields/order/details | Configured fields/order, normal per-object masks, independently permitted detail links. |
| D02 | R7/R8: record interaction | Drill sort/filter/search allowlists | Supported desktop controls narrow the selection; clients cannot add undeclared fields. |
| D03 | R1/R7 versus R2/R3 | allow_print | Host-controlled browser print; no false claim of native extension export or fixed pagination. |
| X01 | R6: custom visualization | CustomChart, VegaLite | Layer/facet/advanced spec uses only the authorized projected source. |
| X02 | O: custom data boundary | Renderer schema and loader | Unknown fields, external/nested URLs, runtime code and unbounded input are rejected. |
| X03 | O: custom lifecycle | mount/update/destroy | Filter changes release old views, preserve raw encoding, and update the data table. |
| S01 | O: row/field isolation | ResourceService, Policy | Query/options/drill/metadata/custom/raw paths enforce tenant and field grants. |
| S02 | O: selection integrity | Signed selectors | Tampered/expired/wrong-user/stale-definition selectors do not reveal records. |
| S03 | O: numeric semantics | Datum/valueType | Decimal/large integer/date encodings and null/zero/no-fact states remain distinct. |
| S04 | O: production budgets | Analytics limits | Cardinality/timeout/fallback limits fail explicitly, never fabricate full aggregates. |
| S05 | O: runtime/transport parity | Service, GET/POST | Equivalent state yields equivalent values through HTML, JSON, or direct service use. |
| S06 | O: wire contract | Pydantic unions, Problem | camelCase schemas match OpenAPI; unknown fields reject; errors contain no secrets. |
| S07 | O/R8: responsive access | Table alternative, actions | Keyboard/touch/mobile layout supports inspection without hover-only controls. |
| S08 | O: request races | Loader generations | A slower old response cannot replace newer filter/context data. |
| S09 | O: read consistency | Execution read view | Cell/axis/grand totals agree within one query; later drill advertises its own timestamp. |

## 13. Delivery and implementation boundaries

This file is the deliverable. It does not assert that the proposed signatures,
mount support, source projections, renderer adapters, or JSON schemas already
exist. No code, migrations, test files, or other documentation changes are
required to deliver this specification.

For a later implementation:

- Replace the current dashboard object API in a declared breaking release;
  translate `slug/widgets/measure/filters` declarations to named class widgets,
  `y` or `value`, and `where`. Preserve endpoint locations where specified, but
  do not silently retain conflicting legacy parameter grammars.
- Preserve core policies/services and the feature's dependency boundaries.
  Add `View.analytics` and the source-schema capabilities described here without
  making analytics-only projections writable resource fields.
- Implement and advertise capabilities per executor/renderer. Core chart kinds,
  Count/Sum/Avg/Min/Max/distinct, split membership, safe scopes, and the declared
  transport are required on SQLite and PostgreSQL. Optional statistical summaries
  and custom renderers fail installation if used without an exact supported
  execution path; they are not silently substituted.
- Review every acceptance row against the implementation and run appropriate
  validation when code is subsequently requested. Use uv for Python and pnpm
  for frontend tooling. Any required schema change must use Alembic; this design
  itself adds no persistent analytics tables.

The specification is complete when each in-scope reference behavior has a matrix
entry, every proposed entry point has defined input/output/failure semantics,
and every documented uncertainty is distinguished from an OBOX guarantee.
