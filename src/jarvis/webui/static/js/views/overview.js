/* Overview: what the assistant is doing, and where the last turn went. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import {
  card,
  chip,
  clear,
  el,
  empty,
  meter,
  sparkline,
  stageBar,
  stageLegend,
  stat,
} from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("overview.title") }),
    el("p", { text: t("overview.lead") }),
  ]);

  const lastTurnCard = el("section", { class: "card" });
  const cardsRow = el("div", { class: "grid" });
  const exchangeCard = el("section", { class: "card" });
  const recentCard = el("section", { class: "card" });

  root.append(head, lastTurnCard, cardsRow, exchangeCard, recentCard);

  async function paint() {
    const [status, turnsPayload, tools, security, graph] = await Promise.all([
      api.status(),
      api.turns(20),
      api.tools().catch(() => ({ tools: [], servers: [] })),
      api.security().catch(() => ({ level: "?", pending: [] })),
      api.graphStats().catch(() => ({})),
    ]);

    const turns = turnsPayload.turns || [];
    const last = status.last_turn || turns[turns.length - 1] || null;

    paintLastTurn(lastTurnCard, last, turns);
    paintCards(cardsRow, { status, tools, security, graph });
    paintExchange(exchangeCard, last);
    paintRecent(recentCard, turns);
  }

  await paint();

  // A finished turn is the only thing on this view that changes on its own.
  const off = live.on("turn", () => paint().catch(() => {}));
  const timer = setInterval(() => paint().catch(() => {}), 15000);

  return () => {
    off();
    clearInterval(timer);
  };
}

function paintLastTurn(container, turn, turns) {
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

  container.append(
    el("div", { class: "turn-total" }, [
      el("span", { class: "big", text: fmt.ms(turn.total_ms) }),
      turn.source === "text" ? chip("text") : null,
      turn.language ? chip(turn.language) : null,
      el("span", { style: "margin-left:auto" }, [sparkline(totals)]),
    ]),
    stageBar(turn),
    stageLegend(turn),
  );
}

function paintCards(container, { status, tools, security, graph }) {
  clear(container);

  const builtin = (tools.tools || []).filter((tool) => tool.origin === "builtin").length;
  const mcp = (tools.tools || []).filter((tool) => tool.origin === "mcp").length;
  const discarded = Object.values(status.discarded || {}).reduce((a, b) => a + b, 0);

  container.append(
    card(t("overview.memoryCard"), [
      stat(t("memory.stats.nodes"), fmt.number(graph.total_nodes ?? 0)),
      el("span", {
        class: "stat-sub",
        text: `${fmt.number(graph.total_tokens ?? 0)} ${t("memory.stats.tokens").toLowerCase()}`,
      }),
    ]),

    card(t("overview.toolsCard"), [
      stat(t("nav.tools"), fmt.number(builtin + mcp)),
      el("span", {
        class: "stat-sub",
        text: `${builtin} ${t("overview.builtin")} · ${mcp} ${t("overview.mcp")}`,
      }),
    ]),

    card(t("overview.securityCard"), [
      stat(t("security.level"), security.level || "—"),
      el("span", {
        class: "stat-sub",
        text: security.pending?.length
          ? `${security.pending.length} ${t("overview.pending")}`
          : t("security.noPending"),
      }),
    ]),

    card(t("overview.discarded"), [
      stat(t("conversation.discarded"), fmt.number(discarded)),
      el(
        "div",
        { class: "turn-tools" },
        Object.entries(status.discarded || {}).map(([reason, count]) =>
          chip(`${reason} ${count}`),
        ),
      ),
    ]),
  );
}

function paintExchange(container, turn) {
  clear(container);
  container.append(el("header", {}, [el("h2", { text: t("overview.exchange") })]));

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

function paintRecent(container, turns) {
  clear(container);
  container.append(el("header", {}, [el("h2", { text: t("overview.recent") })]));

  if (!turns.length) {
    container.append(empty(t("conversation.empty")));
    return;
  }

  const list = el("div", { class: "rows scroll" });
  for (const turn of [...turns].reverse()) {
    list.append(
      el("div", { class: "row" }, [
        el("span", { class: "key num", text: fmt.time(turn.started_at) }),
        el("span", { class: "val" }, [
          el("span", { text: turn.transcript || "—" }),
          el("span", { class: "muted num", text: `  ${fmt.ms(turn.total_ms)}` }),
        ]),
      ]),
    );
  }
  container.append(list);
}
