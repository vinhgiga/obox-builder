import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const client = new URL("../../obox/static/http.js", import.meta.url).href;
const source = readFileSync(
  new URL("../../obox/contrib/analytics/static/analytics.js", import.meta.url),
  "utf8",
).replace('"./http.js"', JSON.stringify(client));
const {
  chartData,
  chartConfiguration,
  ComponentLoader,
  pooled,
  pivotEntries,
  recordURL,
  selectWidget,
  setWidgetView,
  widgetRecordSelections,
} = await import(
  `data:text/javascript;base64,${Buffer.from(`${source}\nexport { pivotEntries };`).toString("base64")}`
);

const labels = { empty: "(blank)", total: "Total" };
const rowPaths = [
  [["str", "North"]],
  [
    ["str", "North"],
    ["str", "N-1"],
  ],
  [
    ["str", "North"],
    ["str", "N-2"],
  ],
  [["str", "South"]],
  [
    ["str", "South"],
    ["str", "S-1"],
  ],
];

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

test("pivot rows place value fields around nested dimensions and keep subtotals", () => {
  const entries = pivotEntries(
    rowPaths,
    ["region", "code"],
    ["Sum of amount", "Average of amount"],
    1,
    new Set(),
    labels,
    { columns: false, subtotals: true },
  );
  const northChildren = entries.filter(
    (entry) => entry.path.length === 2 && entry.path[0][1] === "North",
  );
  const northSubtotal = entries.filter((entry) => entry.index === 0);

  assert.deepEqual(
    northChildren.map(({ heading }) => heading.map(({ label }) => label)),
    [
      ["North", "Sum of amount", "N-1"],
      ["North", "Sum of amount", "N-2"],
      ["North", "Average of amount", "N-1"],
      ["North", "Average of amount", "N-2"],
    ],
  );
  assert.deepEqual(
    northSubtotal.map(({ heading }) => heading.map(({ label }) => label)),
    [
      ["North", "North Sum of amount"],
      ["North", "North Average of amount"],
    ],
  );
  assert.ok(
    entries.findIndex(({ index }) => index === 0) >
      entries.findIndex(({ path }) => path.length === 2),
  );
  assert.equal(
    northSubtotal.every(({ subtotal, showValue }) => subtotal && showValue),
    true,
  );
});

test("pivot columns insert measures at the configured level and order subtotals after details", () => {
  const entries = pivotEntries(
    [
      [["str", "NG"]],
      [
        ["str", "NG"],
        ["str", "store"],
      ],
      [
        ["str", "NG"],
        ["str", "web"],
      ],
    ],
    ["status", "channel"],
    ["Count of code", "Sum of amount"],
    1,
    new Set(),
    labels,
    { columns: true, subtotals: true },
  );

  assert.deepEqual(
    entries.map(({ heading }) => heading.map(({ label }) => label)),
    [
      ["NG", "Count of code", "store"],
      ["NG", "Count of code", "web"],
      ["NG", "Sum of amount", "store"],
      ["NG", "Sum of amount", "web"],
      ["NG Total", "Count of code"],
      ["NG Total", "Sum of amount"],
    ],
  );
  assert.deepEqual(
    entries.slice(-2).map(({ subtotal }) => subtotal),
    [true, true],
  );
});

test("pivot column measures can lead or trail the dimension hierarchy", () => {
  const paths = [
    [["str", "NG"]],
    [
      ["str", "NG"],
      ["str", "store"],
    ],
    [
      ["str", "NG"],
      ["str", "web"],
    ],
  ];
  const leading = pivotEntries(
    paths,
    ["status", "channel"],
    ["Count of code"],
    0,
    new Set(),
    labels,
    { columns: true, subtotals: false },
  );
  const trailing = pivotEntries(
    paths,
    ["status", "channel"],
    ["Count of code"],
    2,
    new Set(),
    labels,
    { columns: true, subtotals: false },
  );

  assert.deepEqual(
    leading[0].heading.map(({ label }) => label),
    ["Count of code", "NG", "store"],
  );
  assert.deepEqual(
    trailing[0].heading.map(({ label }) => label),
    ["NG", "store", "Count of code"],
  );
});

test("pivot collapse retains the selected group subtotal", () => {
  const folded = new Set([JSON.stringify([["str", "North"]])]);
  const entries = pivotEntries(
    rowPaths,
    ["region", "code"],
    ["Count of code", "Sum of amount"],
    1,
    folded,
    labels,
    { columns: false, subtotals: true },
  );

  assert.deepEqual(
    entries
      .filter(({ path }) => path[0][1] === "North")
      .map(({ heading }) => heading.map(({ label }) => label)),
    [
      ["North", "North Count of code"],
      ["North", "North Sum of amount"],
    ],
  );
  assert.equal(
    entries.some(({ path }) => path.length === 2 && path[0][1] === "North"),
    false,
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
  const loader = new ComponentLoader();
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
  await new ComponentLoader().load(
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

test("component scopes share the page request budget", async () => {
  let active = 0;
  let maximum = 0;
  const releases = [];
  const receive = () =>
    new Promise((resolve) => {
      active++;
      maximum = Math.max(maximum, active);
      releases.push(() => {
        active--;
        resolve({});
      });
    });
  const loading = Array.from({ length: 3 }, (_, index) =>
    new ComponentLoader().load([index], receive, () => {}),
  );
  await new Promise(setImmediate);
  assert.equal(releases.length, 2);
  releases.shift()();
  await new Promise(setImmediate);
  assert.equal(releases.length, 2);
  for (const release of releases) release();
  await Promise.all(loading);
  assert.equal(maximum, 2);
});

test("cancelling a queued component does not wait for another scope", async () => {
  const releases = [];
  const receive = () => new Promise((resolve) => releases.push(resolve));
  const active = [1, 2].map((index) => new ComponentLoader().load([index], receive, () => {}));
  await new Promise(setImmediate);
  let requests = 0;
  const queued = new ComponentLoader();
  const old = queued.load(
    [3],
    () => {
      requests++;
    },
    () => {},
  );
  await queued.load([], receive, () => {});
  await old;
  assert.equal(requests, 0);
  for (const release of releases) release({});
  await Promise.all(active);
});
