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
  if (data.series) return renderSeriesTable(widget, data, filters, labels);
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

function selectionURL(base, filters, selector) {
  const url = new URL(recordURL(base, filters));
  url.searchParams.set("selector", selector);
  return url.href;
}

function renderSeriesTable(widget, data, filters, labels) {
  const parent = widget.querySelector("[data-table]");
  parent.replaceChildren();
  const table = node("table", undefined, parent);
  node("caption", widget.querySelector("h2").textContent, table);
  const head = node("tr", undefined, node("thead", undefined, table));
  for (const label of [data.xLabel, labels.series, labels.value]) {
    node("th", label, head).scope = "col";
  }
  if (data.config.percent) node("th", "%", head).scope = "col";
  const body = node("tbody", undefined, table);
  for (const series of data.series) {
    for (const point of series.points) {
      const row = node("tr", undefined, body);
      node("th", point.x === null ? labels.empty : String(point.x), row).scope = "row";
      node("td", series.label, row);
      const cell = node("td", undefined, row);
      const value = point.y === null ? "—" : String(point.y);
      if (point.selector) {
        const link = node("a", value, cell);
        link.href = selectionURL(widget.dataset.recordsUrl, filters, point.selector);
        link.setAttribute("aria-label", `${labels.records}: ${series.label}, ${value}`);
      } else cell.textContent = value;
      if (data.config.percent) node("td", percentage(point.percentage), row);
    }
  }
}

function percentage(value) {
  return value === null
    ? "—"
    : new Intl.NumberFormat(undefined, {
        style: "percent",
        maximumFractionDigits: 2,
      }).format(Number(value));
}

function color(key) {
  let hash = 2166136261;
  for (const character of JSON.stringify(key)) {
    hash = Math.imul(hash ^ character.charCodeAt(0), 16777619);
  }
  return `hsl(${(hash >>> 0) % 360}, 65%, 48%)`;
}

export function chartConfiguration(data, labels, open) {
  const config = data.config;
  const circular = ["pie", "donut"].includes(config.kind);
  const horizontal = config.orientation === "horizontal";
  const continuous =
    !circular &&
    ((config.kind === "scatter" && data.xType === "number") ||
      (config.kind !== "bar" && data.xType === "time"));
  const categories = data.categories;
  const indices = new Map(categories.map((value, index) => [JSON.stringify(value), index]));
  const valueAxis = horizontal ? "x" : "y";
  const categoryAxis = horizontal ? "y" : "x";
  const chartLabels = categories.map((value) => (value === null ? labels.empty : String(value)));
  const datasets = data.series.map((series) => {
    const seriesColor = color(data.grouped ? series.group : series.id);
    return {
      type: series.kind === "donut" ? "doughnut" : series.kind,
      label: series.label,
      indexAxis: horizontal ? "y" : "x",
      [`${valueAxis}AxisID`]: series.axis,
      [`${categoryAxis}AxisID`]: "category",
      backgroundColor: circular ? series.points.map((point) => color(point.x)) : seriesColor,
      borderColor: circular ? "#ffffff" : seriesColor,
      data: series.points.map((point) => {
        if (circular) return point.y === null ? null : Number(point.y);
        const x = continuous
          ? point.x === null
            ? null
            : data.xType === "time"
              ? Date.parse(point.x)
              : Number(point.x)
          : indices.get(JSON.stringify(point.x));
        const value = Object.hasOwn(point, "plotY") ? point.plotY : point.y;
        const y = value === null ? null : Number(value);
        return horizontal ? { x: y, y: x } : { x, y };
      }),
    };
  });
  const scales = {};
  if (!circular) {
    scales.category = {
      axis: categoryAxis,
      type: continuous ? "linear" : "category",
      title: { display: true, text: data.xLabel },
      stacked: config.stack !== "none",
      ...(data.xType === "time" && continuous
        ? {
            ticks: {
              callback: (value) => new Date(value).toLocaleString(undefined, { timeZone: "UTC" }),
            },
          }
        : {}),
    };
    for (const axis of ["primary", "secondary"]) {
      if (!data.series.some((series) => series.axis === axis)) continue;
      const settings = config[`${axis}_axis`];
      scales[axis] = {
        axis: valueAxis,
        type: "linear",
        position: horizontal
          ? axis === "primary"
            ? "bottom"
            : "top"
          : axis === "primary"
            ? "left"
            : "right",
        beginAtZero: settings.zero ?? config.kind === "bar",
        title: { display: Boolean(settings.label), text: settings.label },
        stacked: config.stack !== "none",
        ticks: {
          precision: data.series
            .filter((series) => series.axis === axis)
            .every((series) => series.valueType === "integer")
            ? 0
            : undefined,
        },
        ...(settings.minimum !== null && settings.minimum !== undefined
          ? { min: settings.minimum }
          : {}),
        ...(settings.maximum !== null && settings.maximum !== undefined
          ? { max: settings.maximum }
          : {}),
        ...(axis === "secondary" ? { grid: { drawOnChartArea: false } } : {}),
        ...(config.stack === "percent" ? { min: 0, max: 1, ticks: { callback: percentage } } : {}),
      };
    }
  }
  const pointAt = (context) => data.series[context.datasetIndex].points[context.dataIndex];
  return {
    type: config.kind === "donut" ? "doughnut" : config.kind,
    data: { labels: chartLabels, datasets },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      scales,
      indexAxis: horizontal ? "y" : "x",
      plugins: {
        legend: {
          display:
            config.legend !== "hidden" &&
            (config.legend !== "auto" || circular || datasets.length > 1),
          position: ["auto", "hidden"].includes(config.legend) ? "top" : config.legend,
        },
        tooltip: {
          callbacks: {
            title: (items) => (items.length ? String(pointAt(items[0]).x ?? labels.empty) : ""),
            label: (context) => {
              const point = pointAt(context);
              return `${context.dataset.label}: ${point.y ?? "—"}${config.percent ? ` (${percentage(point.percentage)})` : ""}`;
            },
          },
        },
      },
      onClick: (_event, elements) => {
        if (!elements.length) return;
        const element = elements[0];
        const point = data.series[element.datasetIndex].points[element.index];
        if (point.selector) open(point.selector);
      },
    },
    plugins: [
      {
        id: "oboxLabels",
        afterDatasetsDraw(chart) {
          if (!config.labels && !config.percent) return;
          const context = chart.ctx;
          context.save();
          context.fillStyle = "#111827";
          context.font = "12px sans-serif";
          context.textAlign = "center";
          chart.data.datasets.forEach((_dataset, datasetIndex) => {
            if (!chart.isDatasetVisible(datasetIndex)) return;
            chart.getDatasetMeta(datasetIndex).data.forEach((element, index) => {
              const point = data.series[datasetIndex].points[index];
              if (
                point.y === null ||
                point.state === "noFacts" ||
                (circular && Number(point.y) === 0)
              )
                return;
              const position = element.tooltipPosition();
              if (!Number.isFinite(position.x) || !Number.isFinite(position.y)) return;
              context.fillText(
                config.percent ? percentage(point.percentage) : String(point.y),
                position.x,
                position.y - 5,
              );
            });
          });
          context.restore();
        },
      },
    ],
  };
}

function renderChart(widget, data, filters, labels) {
  const holder = widget.querySelector("[data-chart]");
  if (data.series) {
    if (!globalThis.Chart) throw new Error(labels.error);
    holder.hidden = false;
    return new globalThis.Chart(
      holder.querySelector("canvas"),
      chartConfiguration(data, labels, (selector) =>
        globalThis.location.assign(selectionURL(widget.dataset.recordsUrl, filters, selector)),
      ),
    );
  }
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
  const prefix = root.dataset.filterPrefix || "";
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
          !data.records ||
          (isCircle &&
            (data.series
              ? !data.series.some((series) => series.points.some((point) => Number(point.y) > 0))
              : !chartData(data, widget.dataset.kind, labels.empty).length));
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
      const name = key.slice(prefix.length);
      if (name.startsWith("filter_op.")) continue;
      const field = name.startsWith("filter.") ? name.slice(7) : null;
      const operator = field && values.get(`${prefix}filter_op.${field}`);
      if (value || ["is_empty", "is_not_empty"].includes(operator)) {
        filters.set(key, value);
        if (operator) filters.set(`${prefix}filter_op.${field}`, operator);
      }
    }
    const url = new URL(globalThis.location.href);
    if (prefix) {
      for (const key of [...url.searchParams.keys()]) {
        if (key.startsWith(prefix)) url.searchParams.delete(key);
      }
      for (const [key, value] of filters) url.searchParams.set(key, value);
    } else url.search = filters.toString();
    globalThis.history.replaceState(null, "", url);
    void load(filters);
  });
  root.querySelector("[data-reset]")?.addEventListener("click", (event) => {
    if (!prefix) return;
    event.preventDefault();
    const url = new URL(globalThis.location.href);
    for (const key of [...url.searchParams.keys()]) {
      if (key.startsWith(prefix)) url.searchParams.delete(key);
    }
    globalThis.location.assign(url.href);
  });
  const initial = new URLSearchParams(globalThis.location.search);
  if (prefix) {
    for (const key of [...initial.keys()]) {
      if (!key.startsWith(prefix)) initial.delete(key);
    }
  }
  void load(initial);
}

if (typeof document !== "undefined") {
  for (const root of document.querySelectorAll("[data-analytics]")) mount(root);
}
