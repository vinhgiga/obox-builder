import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const client = new URL("../../obox_builder/builder/static/http.js", import.meta.url).href;
const source = readFileSync(
  new URL("../../obox_builder/contrib/analytics/static/analytics.js", import.meta.url),
  "utf8",
).replace('"./http.js"', JSON.stringify(client));
const { chartData, DashboardLoader, pooled, recordURL } = await import(
  `data:text/javascript;base64,${Buffer.from(source).toString("base64")}`
);

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
  const data = { rows: [null, "", "large"], row_totals: [null, "0.00", "9007199254740993.01"] };
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
