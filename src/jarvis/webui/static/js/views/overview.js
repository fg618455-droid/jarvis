/* Overview: where the last turn went, and what every other view is holding.

   This is the only page that reads across the rest, so what it shows is a
   reading per destination and a way into each. It keeps no history of its
   own: the Conversation view holds the turns and holds them as a
   conversation, and a second, worse copy here would only teach a reader
   that the two disagree. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { chip, clear, el, empty, sparkline, stageBar, stageLegend } from "../ui.js";

const REFRESH_MS = 15000;

export async function mount(root) {
  const timeCard = el("section", { class: "card" });
  const readings = el("div", { class: "grid readings" });
  const exchangeCard = el("section", { class: "card" });

  root.append(
    el("div", { class: "view-head" }, [
      el("h1", { text: t("overview.title") }),
      el("p", { text: t("overview.lead") }),
    ]),
    timeCard,
    readings,
    exchangeCard,
  );

  async function paint() {
    const [status, turnsPayload, tools, security, graph] = await Promise.all([
      api.status(),
      api.turns(50),
      api.tools().catch(() => ({ tools: [], servers: [] })),
      api.security().catch(() => ({ level: "?", pending: [] })),
      api.graphStats().catch(() => ({})),
    ]);

    const turns = turnsPayload.turns || [];
    const last = status.last_turn || turns[turns.length - 1] || null;

    paintTime(timeCard, last, turns);
    paintReadings(readings, { status, tools, security, graph });
    paintExchange(exchangeCard, last);
  }

  await paint();

  // A finished turn is the only thing on this view that changes on its own.
  const off = live.on("turn", () => paint().catch(() => {}));
  const timer = setInterval(() => paint().catch(() => {}), REFRESH_MS);

  return () => {
    off();
    clearInterval(timer);
  };
}

/* ── Where the time went ────────────────────────────────────────────── */

/* The middle of the recent totals rather than their mean: one turn that
   waited on a cold model would drag an average somewhere no turn has ever
   actually been. */
function median(values) {
  if (!values.length) return null;
  const sorted = [...values].sort((a, b) => a - b);
  const middle = Math.floor(sorted.length / 2);
  return sorted.length % 2
    ? sorted[middle]
    : (sorted[middle - 1] + sorted[middle]) / 2;
}

function paintTime(container, turn, turns) {
  clear(container);
  container.append(
    el("header", {}, [
      el("h2", { text: t("overview.stages") }),
      el("span", { class: "aside", text: turn ? fmt.time(turn.started_at) : "" }),
    ]),
  );

  if (!turn) {
    container.append(empty(t("overview.noTurns")));
    return;
  }

  const totals = turns.map((item) => item.total_ms || 0).filter(Boolean);
  const typical = median(totals);

  container.append(
    el("div", { class: "turn-total" }, [
      el("span", { class: "big turn-total-big", text: fmt.ms(turn.total_ms) }),
      // The last wait on its own says nothing about whether it was normal.
      typical === null
        ? null
        : el("span", { class: "turn-typical" }, [
            el("span", { class: "stat-label", text: t("overview.median", { n: totals.length }) }),
            el("span", { class: "num", text: fmt.ms(typical) }),
          ]),
      turn.source === "text" ? chip(t("conversation.typed")) : null,
      el("span", { class: "turn-spark" }, [sparkline(totals)]),
    ]),
    stageBar(turn),
    stageLegend(turn),
  );
}

/* ── One reading per destination ────────────────────────────────────── */

function reading(view, title, value, sub) {
  return el("a", { class: "card card-link", href: `#/${view}` }, [
    el("header", {}, [el("h2", { text: title })]),
    el("div", { class: "stat" }, [
      el("span", { class: "stat-value", text: value }),
      sub ? el("span", { class: "stat-sub" }, [sub]) : null,
    ]),
  ]);
}

function paintReadings(container, { status, tools, security, graph }) {
  clear(container);

  const builtin = (tools.tools || []).filter((tool) => tool.origin === "builtin").length;
  const mcp = (tools.tools || []).filter((tool) => tool.origin === "mcp").length;
  const discarded = Object.entries(status.discarded || {}).filter(([, n]) => n);
  const discardedTotal = discarded.reduce((sum, [, n]) => sum + n, 0);

  container.append(
    reading(
      "memory",
      t("overview.memoryCard"),
      fmt.number(graph.total_nodes ?? 0),
      `${fmt.number(graph.total_tokens ?? 0)} ${t("memory.stats.tokens").toLowerCase()}`,
    ),
    reading(
      "tools",
      t("overview.toolsCard"),
      fmt.number(builtin + mcp),
      `${builtin} ${t("overview.builtin")} · ${mcp} ${t("overview.mcp")}`,
    ),
    reading(
      "security",
      t("overview.securityCard"),
      security.level || "—",
      security.pending?.length
        ? `${security.pending.length} ${t("overview.pending")}`
        : t("security.noPending"),
    ),
    reading(
      "conversation",
      t("overview.discarded"),
      fmt.number(discardedTotal),
      discarded.length
        ? discarded.map(([reason, count]) => `${reason} ${count}`).join(" · ")
        : t("common.none"),
    ),
  );
}

/* ── What just happened ─────────────────────────────────────────────── */

function paintExchange(container, turn) {
  clear(container);
  container.append(
    el("header", {}, [
      el("h2", { text: t("overview.exchange") }),
      el("a", { class: "aside", href: "#/conversation", text: t("overview.openConversation") }),
    ]),
  );

  if (!turn) {
    container.append(empty(t("conversation.empty")));
    return;
  }

  container.append(
    el("div", { class: "exchange" }, [
      el("div", { class: "line" }, [
        el("span", { class: "who", text: t("overview.you") }),
        el("span", { text: turn.transcript || "—" }),
      ]),
      el("div", { class: "line" }, [
        el("span", { class: "who", text: t("overview.jarvis") }),
        el("span", { text: turn.reply || turn.error || "—" }),
      ]),
      turn.tools?.length
        ? el("div", { class: "line" }, [
            el("span", { class: "who", text: t("nav.tools") }),
            el(
              "span",
              { class: "turn-tools" },
              turn.tools.map((tool) =>
                chip(`${tool.name} ${fmt.ms(tool.duration_ms)}`, tool.ok ? null : "bad"),
              ),
            ),
          ])
        : null,
    ]),
  );
}
