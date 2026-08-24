/* Building blocks.

   Small DOM helpers rather than a framework: the interface is a handful of
   views over JSON, and a build step would cost more than it saves. Text is
   always set through textContent, so nothing a tool or a web page put into
   memory can become markup here. */

import { t } from "./i18n.js";
import * as fmt from "./fmt.js";

export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === null || value === undefined || value === false) continue;
    // Deliberately no innerHTML anywhere: the interface renders transcripts,
    // memory contents, and tool output, all of which are text this program
    // did not write.
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key === "dataset") Object.assign(node.dataset, value);
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
  return node;
}

/* Whether a reader has asked for motion.
 *
 * `tokens.css` switches off every CSS animation and transition for a reader
 * who has not, but a graphic driven from JavaScript is neither: a bar whose
 * width is assigned on a timer keeps moving through that rule. Anything
 * painted that way has to ask, and must never be the only thing saying what
 * it says. */
export function motionAllowed() {
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export function card(title, body, aside) {
  const header = title
    ? el("header", {}, [el("h2", { text: title }), aside && el("span", { class: "aside" }, [aside])])
    : null;
  return el("section", { class: "card" }, [header, ...[].concat(body)]);
}

export function stat(label, value, sub) {
  return el("div", { class: "stat" }, [
    el("span", { class: "stat-label", text: label }),
    el("span", { class: "stat-value", text: value }),
    sub && el("span", { class: "stat-sub" }, [sub]),
  ]);
}

export function meter(fraction, tone) {
  const clamped = Math.max(0, Math.min(1, fraction || 0));
  const bar = el("div", { class: `meter${tone ? ` ${tone}` : ""}` }, [
    el("span", { style: `width: ${(clamped * 100).toFixed(1)}%` }),
  ]);
  return bar;
}

export function chip(text, tone) {
  return el("span", { class: `chip${tone ? ` ${tone}` : ""}`, text });
}

export function empty(message) {
  return el("div", { class: "empty", text: message });
}

export function table(columns, rows) {
  if (!rows.length) return empty(t("common.none"));
  return el("table", {}, [
    el("thead", {}, [
      el(
        "tr",
        {},
        columns.map((column) =>
          el("th", { class: column.numeric ? "num" : null, text: column.label }),
        ),
      ),
    ]),
    el(
      "tbody",
      {},
      rows.map((row) =>
        el(
          "tr",
          {},
          columns.map((column) => {
            const value = column.render ? column.render(row) : row[column.key];
            return el(
              "td",
              { class: column.numeric ? "num" : null },
              value instanceof Node ? [value] : [String(value ?? "—")],
            );
          }),
        ),
      ),
    ),
  ]);
}

export function rows(pairs) {
  return el(
    "div",
    { class: "rows" },
    pairs
      .filter(Boolean)
      .map(([key, value]) =>
        el("div", { class: "row" }, [
          el("span", { class: "key", text: key }),
          el("span", { class: "val" }, value instanceof Node ? [value] : [String(value ?? "—")]),
        ]),
      ),
  );
}

/* Stage colours: a single hue walked through lightness, so the eye reads
   the bar as one measurement split into parts rather than as a legend of
   unrelated categories. Tools break the run deliberately, because a tool
   call is the one segment whose cost is not the assistant's own. */
const STAGE_TONES = {
  stt: "#7dd3fc",
  tool_routing: "#5aa9cc",
  planner: "#4a8caa",
  recall: "#3d7189",
  llm: "#93a4b3",
  tts_synth: "#d9dee4",
};

const TOOL_TONE = "#fbbf24";

export function stageTone(name) {
  if (name.startsWith("tool:")) return TOOL_TONE;
  return STAGE_TONES[name] || "#4b5563";
}

export function stageTotals(turn) {
  const totals = new Map();
  for (const stage of turn.stages || []) {
    totals.set(stage.name, (totals.get(stage.name) || 0) + stage.duration_ms);
  }
  return totals;
}

export function stageBar(turn) {
  const totals = stageTotals(turn);
  const measured = [...totals.values()].reduce((a, b) => a + b, 0);
  const total = Math.max(turn.total_ms || 0, measured);
  if (!total) return empty(t("overview.noTurns"));

  const bar = el("div", { class: "stagebar" });
  for (const [name, duration] of totals) {
    bar.append(
      el("span", {
        style: `width: ${((duration / total) * 100).toFixed(2)}%; background: ${stageTone(name)}`,
        title: `${name}: ${fmt.ms(duration)}`,
      }),
    );
  }
  const rest = total - measured;
  if (rest > 1) {
    bar.append(
      el("span", {
        style: `width: ${((rest / total) * 100).toFixed(2)}%; background: var(--surface-3)`,
        title: fmt.ms(rest),
      }),
    );
  }
  return bar;
}

export function stageLegend(turn) {
  const totals = stageTotals(turn);
  return el(
    "div",
    { class: "stagelegend" },
    [...totals].map(([name, duration]) =>
      el("span", {}, [
        el("i", { class: "swatch", style: `background: ${stageTone(name)}` }),
        `${name} ${fmt.ms(duration)}`,
      ]),
    ),
  );
}

/* A sparkline of recent totals. Inline SVG keeps it dependency-free and
   crisp at any zoom. */
export function sparkline(values, { width = 160, height = 32 } = {}) {
  if (!values.length) return el("span", { class: "muted", text: "—" });
  const max = Math.max(...values, 1);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const points = values
    .map((value, index) => `${(index * step).toFixed(1)},${(height - (value / max) * height).toFixed(1)}`)
    .join(" ");

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.setAttribute("width", String(width));
  svg.setAttribute("height", String(height));
  svg.setAttribute("preserveAspectRatio", "none");

  const line = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
  line.setAttribute("points", points);
  line.setAttribute("fill", "none");
  line.setAttribute("stroke", "var(--accent)");
  line.setAttribute("stroke-width", "1.5");
  line.setAttribute("stroke-linejoin", "round");
  svg.append(line);
  return svg;
}

let toastHost = null;

export function toast(message, tone) {
  if (!toastHost) {
    toastHost = el("div", { class: "toasts" });
    document.body.append(toastHost);
  }
  const node = el("div", { class: `toast${tone ? ` ${tone}` : ""}`, text: message });
  toastHost.append(node);
  setTimeout(() => node.remove(), 4000);
}

export function icon(path) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "16");
  svg.setAttribute("height", "16");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.5");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", path);
  svg.append(shape);
  return svg;
}

export const ICONS = {
  overview: "M4 13h6V4H4v9Zm0 7h6v-5H4v5Zm10 0h6v-9h-6v9Zm0-16v5h6V4h-6Z",
  memory: "M12 3v18M12 7h4a3 3 0 0 1 0 6h-4M12 12H8a3 3 0 0 0 0 6h4",
  conversation: "M21 12a8 8 0 0 1-11.6 7.1L3 21l1.9-6.4A8 8 0 1 1 21 12Z",
  visualizer: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18ZM9 10h.01M15 10h.01M8 15c1.2 1.2 2.6 1.8 4 1.8s2.8-.6 4-1.8",
  microphone: "M12 3a3 3 0 0 1 3 3v5a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3ZM5 11a7 7 0 0 0 14 0M12 18v3",
  tools: "M14.7 6.3a4 4 0 0 0 5 5l-9.9 9.9a2.1 2.1 0 0 1-3-3l9.9-9.9Z",
  security: "M12 3 4 6v6c0 5 3.4 8.4 8 9 4.6-.6 8-4 8-9V6l-8-3Z",
  system: "M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3M6 6h12v12H6z",
  logs: "M5 3h10l4 4v14H5V3Zm9 1v4h4M8 12h8M8 16h8M8 8h2",
  llm: "M4 7h16v10H8l-4 4V7zm4 4h.01M12 11h.01M16 11h.01",
  crew: "M8 12a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM2 20a6 6 0 0 1 12 0M17 12a3 3 0 1 0 0-6M14 14.5A6 6 0 0 1 22 20",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6ZM19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-2.9 1.2v.1a2 2 0 1 1-4 0v-.2a1.7 1.7 0 0 0-2.9-1.1l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0-1.2-2.9H3a2 2 0 1 1 0-4h.2a1.7 1.7 0 0 0 1.1-2.9l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 2.9-1.2V3a2 2 0 1 1 4 0v.2a1.7 1.7 0 0 0 2.9 1.1l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0 1.2 2.9H21a2 2 0 1 1 0 4h-.2a1.7 1.7 0 0 0-1.4 1Z",
};
