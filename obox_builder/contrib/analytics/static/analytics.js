import { request } from "./http.js";

export function recordURL(base, filters, row, column) {
  const url = new URL(base, globalThis.location?.href || "http://localhost");
  url.search = filters.toString();
  if (row !== undefined) url.searchParams.set("row", JSON.stringify(row));
  if (column !== undefined) url.searchParams.set("column", JSON.stringify(column));
  return url.href;
}

export function chartData(data, kind, empty) {
  const points = data.rows.map((key, index) => ({ key, value: data.row_totals[index] }));
  return kind === "pie" || kind === "donut"
    ? points.filter((point) => Number(point.value) > 0)
    : points.map((point) => ({ ...point, label: point.key === null ? empty : String(point.key) }));
}

export async function pooled(items, run, signal, concurrency = 2) {
  let next = 0;
  await Promise.all(
    Array.from({ length: concurrency }, async () => {
      while (next < items.length && !signal.aborted) {
        const item = items[next++];
        await run(item);
      }
    }),
  );
}

export class DashboardLoader {
  constructor() {
    this.controller = null;
  }

  async load(items, receive, apply) {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    await pooled(
      items,
      async (item) => {
        try {
          const value = await receive(item, controller.signal);
          if (!controller.signal.aborted) apply(item, value, null);
        } catch (error) {
          if (!controller.signal.aborted) apply(item, null, error);
        }
      },
      controller.signal,
    );
  }
}

function node(tag, text, parent) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  parent?.append(element);
  return element;
}

function renderTable(widget, data, filters, labels) {
  const parent = widget.querySelector("[data-table]");
  parent.replaceChildren();
  const table = node("table", undefined, parent);
  node("caption", widget.querySelector("h2").textContent, table);
  const head = node("tr", undefined, node("thead", undefined, table));
  const first = node("th", widget.dataset.rowLabel, head);
  first.scope = "col";
  const hasColumns = Boolean(widget.dataset.columnLabel);
  const showRowTotals = hasColumns && widget.dataset.rowTotals === "true";
  for (const value of data.columns) {
    const th = node(
      "th",
      hasColumns ? (value === null ? labels.empty : String(value)) : labels.value,
      head,
    );
    th.scope = "col";
  }
  if (showRowTotals) node("th", labels.total, head).scope = "col";
  const body = node("tbody", undefined, table);
  function cell(parentRow, value, row, column, header = false) {
    const td = node(header ? "th" : "td", undefined, parentRow);
    if (header) td.scope = "row";
    const link = node("a", value === null ? "—" : String(value), td);
    link.href = recordURL(widget.dataset.recordsUrl, filters, row, column);
    link.setAttribute("aria-label", `${labels.records}: ${link.textContent}`);
  }
  data.rows.forEach((key, index) => {
    const row = node("tr", undefined, body);
    node("th", key === null ? labels.empty : String(key), row).scope = "row";
    data.columns.forEach((column, columnIndex) => {
      cell(row, data.cells[index][columnIndex], key, hasColumns ? column : undefined);
    });
    if (showRowTotals) cell(row, data.row_totals[index], key, undefined);
  });
  if (widget.dataset.columnTotals === "true") {
    const row = node("tr", undefined, node("tfoot", undefined, table));
    node("th", labels.total, row).scope = "row";
    data.columns.forEach((column, index) => {
      cell(row, data.column_totals[index], undefined, hasColumns ? column : undefined);
    });
    if (showRowTotals) cell(row, data.total, undefined, undefined);
  }
}

function renderChart(widget, data, filters, labels) {
  const holder = widget.querySelector("[data-chart]");
  const points = chartData(data, widget.dataset.kind, labels.empty);
  if (!points.length) return;
  if (!globalThis.Chart) throw new Error(labels.error);
  const canvas = holder.querySelector("canvas");
  holder.hidden = false;
  const colors = ["#2563eb", "#0891b2", "#7c3aed", "#db2777", "#d97706", "#059669"];
  return new globalThis.Chart(canvas, {
    type: widget.dataset.kind === "donut" ? "doughnut" : widget.dataset.kind,
    data: {
      labels: points.map((point) => (point.key === null ? labels.empty : String(point.key))),
      datasets: [
        {
          label: labels.value,
          data: points.map((point) => (point.value === null ? null : Number(point.value))),
          backgroundColor: points.map((_, index) => colors[index % colors.length]),
          borderColor: "#2563eb",
        },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: {
        tooltip: {
          callbacks: { label: (context) => String(points[context.dataIndex].value ?? "—") },
        },
      },
      onClick: (_event, elements) => {
        if (elements.length) {
          globalThis.location.assign(
            recordURL(widget.dataset.recordsUrl, filters, points[elements[0].index].key),
          );
        }
      },
    },
  });
}

export function mount(root) {
  const labels = root.dataset;
  const widgets = Array.from(root.querySelectorAll("[data-widget]"));
  const charts = new Map();
  const loader = new DashboardLoader();
  async function load(filters) {
    for (const widget of widgets) {
      charts.get(widget)?.destroy();
      charts.delete(widget);
      widget.querySelector("[data-chart]").hidden = true;
      widget.querySelector("[data-table]").replaceChildren();
      widget.querySelector("[data-status]").textContent = labels.loading;
      widget.setAttribute("aria-busy", "true");
    }
    await loader.load(
      widgets,
      async (widget, signal) => {
        const url = new URL(widget.dataset.url, globalThis.location.href);
        url.search = filters.toString();
        const response = await request(url, { signal, headers: { Accept: "application/json" } });
        const data = await response.json();
        if (!response.ok) {
          throw new Error(
            data.errors?.map((error) => error.detail).join(" ") || data.detail || labels.error,
          );
        }
        return data;
      },
      (widget, data, error) => {
        const status = widget.querySelector("[data-status]");
        widget.setAttribute("aria-busy", "false");
        if (error) {
          status.textContent = error.message || labels.error;
          return;
        }
        renderTable(widget, data, filters, labels);
        const isCircle = ["pie", "donut"].includes(widget.dataset.kind);
        const empty =
          !data.records || (isCircle && !chartData(data, widget.dataset.kind, labels.empty).length);
        status.textContent = empty ? labels.noData : "";
        if (!empty && widget.dataset.kind !== "pivot") {
          try {
            charts.set(widget, renderChart(widget, data, filters, labels));
          } catch (failure) {
            status.textContent = failure.message;
          }
        }
      },
    );
  }
  root.querySelector("form").addEventListener("submit", (event) => {
    event.preventDefault();
    const values = new FormData(event.currentTarget);
    const filters = new URLSearchParams();
    for (const [key, value] of values) {
      if (key.startsWith("filter_op.")) continue;
      const field = key.startsWith("filter.") ? key.slice(7) : null;
      const operator = field && values.get(`filter_op.${field}`);
      if (value || ["is_empty", "is_not_empty"].includes(operator)) {
        filters.set(key, value);
        if (operator) filters.set(`filter_op.${field}`, operator);
      }
    }
    const url = new URL(globalThis.location.href);
    url.search = filters.toString();
    globalThis.history.replaceState(null, "", url);
    void load(filters);
  });
  void load(new URLSearchParams(globalThis.location.search));
}

if (typeof document !== "undefined") {
  for (const root of document.querySelectorAll("[data-analytics]")) mount(root);
}
