import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const client = new URL("../../obox_builder/builder/static/http.js", import.meta.url).href;
const source = readFileSync(
  new URL("../../obox_builder/contrib/analytics/static/analytics.js", import.meta.url),
  "utf8",
).replace('"./http.js"', JSON.stringify(client));
const {
  chartData,
  chartConfiguration,
  DashboardLoader,
  pooled,
  recordURL,
  selectWidget,
  setWidgetView,
  widgetRecordSelections,
} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("drill-down preserves filters and distinguishes totals, null and empty groups", () => {
  const filters = new URLSearchParams({ "filter.region": "East" });
  const total = new URL(recordURL("/analytics/sales/pivot/records", filters));
  assert.equal(total.searchParams.get("filter.region"), "East");
  assert.equal(total.searchParams.has("row"), false);
  const empty = new URL(recordURL(total.href, filters, null, ""));
  assert.equal(empty.searchParams.get("row"), "null");
  assert.equal(empty.searchParams.get("column"), '""');
  const text = '<img src=x onerror="alert(1)">';
  assert.equal(
    JSON.parse(new URL(recordURL(total.href, filters, text)).searchParams.get("row")),
    text,
  );
});

test("charts retain exact values for tooltips and omit zero/null circular slices", () => {
  const data = { rows: [null, "", "large"], rowTotals: [null, "0.00", "9007199254740993.01"] };
  const bar = chartData(data, "bar", "Empty");
  assert.equal(bar[0].label, "Empty");
  assert.equal(bar[1].label, "");
  assert.equal(bar[2].value, "9007199254740993.01");
  for (const kind of ["pie", "donut"]) {
    assert.deepEqual(chartData(data, kind, "Empty"), [
      { key: "large", value: "9007199254740993.01" },
    ]);
  }
});

test("chart menu switches views and resizes the chart when it is shown", () => {
  const chartHolder = { hidden: true };
  const table = { hidden: true };
  const buttons = ["chart", "table"].map((widgetView) => ({
    dataset: { widgetView },
    hidden: widgetView === "chart",
  }));
  const widget = {
    dataset: {},
    querySelector(selector) {
      return selector === "[data-chart]" ? chartHolder : table;
    },
    querySelectorAll() {
      return buttons;
    },
  };
  let resizes = 0;
  const chart = {
    resize() {
      resizes++;
    },
  };

  setWidgetView(widget, "table", chart);
  assert.equal(widget.dataset.view, "table");
  assert.equal(chartHolder.hidden, true);
  assert.equal(table.hidden, false);
  assert.equal(buttons[0].hidden, false);
  assert.equal(buttons[1].hidden, true);
  assert.equal(resizes, 0);

  setWidgetView(widget, "chart", chart);
  assert.equal(chartHolder.hidden, false);
  assert.equal(table.hidden, true);
  assert.equal(buttons[0].hidden, true);
  assert.equal(buttons[1].hidden, false);
  assert.equal(resizes, 1);
});

test("only the selected chart exposes its menu", () => {
  const widget = () => {
    const attributes = new Set();
    return {
      toggleAttribute(name, enabled) {
        if (enabled) attributes.add(name);
        else attributes.delete(name);
      },
      hasAttribute: (name) => attributes.has(name),
    };
  };
  const charts = [widget(), widget()];

  selectWidget(charts, charts[1]);
  assert.equal(charts[0].hasAttribute("data-selected"), false);
  assert.equal(charts[1].hasAttribute("data-selected"), true);
  selectWidget(charts, null);
  assert.equal(charts[1].hasAttribute("data-selected"), false);
});

test("expanded chart records follow chart categories and current filters", () => {
  const widget = { dataset: { drill: "true", recordsUrl: "/records" } };
  const filters = new URLSearchParams({ "filter.region": "South" });
  const selections = widgetRecordSelections(widget, { rows: ["North", null] }, filters, {
    empty: "Empty",
    allRecords: "All records",
  });

  assert.deepEqual(
    selections.map(({ label }) => label),
    ["North", "Empty", "All records"],
  );
  assert.equal(new URL(selections[0].url).searchParams.get("filter.region"), "South");
  assert.equal(new URL(selections[0].url).searchParams.get("row"), '"North"');
  assert.equal(new URL(selections.at(-1).url).searchParams.has("row"), false);
});

test("widget loading caps concurrent requests at two", async () => {
  let active = 0;
  let maximum = 0;
  const visited = [];
  await pooled(
    [1, 2, 3, 4, 5],
    async (item) => {
      maximum = Math.max(maximum, ++active);
      await new Promise((resolve) => setImmediate(resolve));
      visited.push(item);
      active--;
    },
    new AbortController().signal,
  );
  assert.equal(maximum, 2);
  assert.equal(active, 0);
  assert.deepEqual(visited.sort(), [1, 2, 3, 4, 5]);
});

test("new filters cancel old work and stale responses never replace the latest results", async () => {
  const loader = new DashboardLoader();
  let resolveOld;
  let oldSignal;
  const applied = [];
  const first = loader.load(
    ["old"],
    async (_item, signal) => {
      oldSignal = signal;
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    },
    (...args) => applied.push(args),
  );
  await loader.load(
    ["new"],
    async () => "fresh",
    (...args) => applied.push(args),
  );
  resolveOld("stale");
  await first;
  assert.equal(oldSignal.aborted, true);
  assert.deepEqual(applied, [["new", "fresh", null]]);
});

test("a widget failure leaves other widgets usable", async () => {
  const results = new Map();
  await new DashboardLoader().load(
    ["broken", "ok", "also-ok"],
    async (item) => {
      if (item === "broken") throw new Error("Too many groups");
      return item;
    },
    (item, value, error) => results.set(item, error?.message || value),
  );
  assert.deepEqual(Object.fromEntries(results), {
    broken: "Too many groups",
    ok: "ok",
    "also-ok": "also-ok",
  });
});

test("chart configuration reads camelCase axis contracts", () => {
  const config = chartConfiguration(
    {
      categories: ["A"],
      xType: "category",
      grouped: false,
      config: {
        kind: "bar",
        orientation: "vertical",
        stack: "none",
        legend: "auto",
        primaryAxis: { label: "Revenue", minimum: -10, maximum: 100 },
        secondaryAxis: {},
      },
      series: [
        {
          id: "sales",
          axis: "primary",
          label: "Sales",
          kind: "bar",
          valueType: "number",
          points: [{ x: "A", y: "2.50" }],
        },
      ],
    },
    { empty: "Empty" },
    () => {},
  );
  assert.equal(config.options.scales.primary.min, -10);
  assert.equal(config.options.scales.primary.max, 100);
  assert.equal(config.options.scales.primary.title.text, "Revenue");
});
