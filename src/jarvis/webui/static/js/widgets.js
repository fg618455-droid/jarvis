/* The widgets around the face.

   A widget is a reading and a way into the detail behind it. It is not a
   small version of its panel: it answers one question at a glance, and the
   panel answers the rest. A widget that tried to be its own view would turn
   the deck back into the twelve pages it replaced.

   Every widget takes the same shared snapshot and paints from it. The deck
   fetches once for all of them, so eleven readings on screen are not eleven
   timers against the daemon, and every widget is looking at the same moment
   rather than at eleven slightly different ones.

   A widget never invents a reading. A source that failed or has not
   answered yet shows an em dash, because a zero that means "no answer" is
   indistinguishable from a zero that means "none", and on the security
   widget those two are very different facts. */

import * as fmt from "./fmt.js";
import { t } from "./i18n.js";
import { chip, el, icon, ICONS } from "./ui.js";

const NOTHING = "—";

/* A security level is a reading with a direction, so it carries a tone rather
   than borrowing one. `critical` and `paranoid` both stop something before it
   runs; `off` stops nothing, and a gate that has been switched off is the one
   state on this widget worth catching an eye. A level this does not name is
   painted plainly rather than guessed at. */
const LEVEL_TONES = {
  off: "warn",
  critical: "ok",
  paranoid: "ok",
};

function reading(value, unit) {
  return el("div", { class: "widget-reading" }, [
    el("span", { class: "num", text: value ?? NOTHING }),
    unit && el("span", { class: "unit", text: unit }),
  ]);
}

function note(text) {
  return el("div", { class: "widget-note", text: text || "" });
}

/* ── The definitions ───────────────────────────────────────────────── */

/* `rail` places it, `tile` shrinks it, `panel` is both the address its
   detail opens at and the name it is announced by. */
export const WIDGETS = [
  {
    panel: "briefing",
    rail: "left",
    icon: ICONS.briefing,
    build() {
      const headline = reading(NOTHING);
      const detail = note("");
      return {
        body: [headline, detail],
        update({ briefing }) {
          if (!briefing) return;
          const items = briefing.items || [];
          headline.querySelector(".num").textContent = briefing.available
            ? String(items.length)
            : NOTHING;
          const unit = headline.querySelector(".unit");
          if (unit) unit.remove();
          headline.append(
            el("span", {
              class: "unit",
              text: briefing.available ? t("briefing.itemsUnit") : t("briefing.notConfigured"),
            }),
          );
          detail.textContent = items.length ? items[0].title : (briefing.summary || "");
        },
      };
    },
  },
  {
    panel: "system",
    rail: "left",
    icon: ICONS.system,
    build() {
      const memory = reading(NOTHING);
      const model = note("");
      return {
        body: [memory, model],
        update({ system }) {
          const gpu = system?.gpu;
          const value = gpu?.used_mb && gpu?.total_mb
            ? `${fmt.megabytes(gpu.used_mb)}`
            : NOTHING;
          memory.querySelector(".num").textContent = value;
          const unit = memory.querySelector(".unit");
          const total = gpu?.total_mb ? `/ ${fmt.megabytes(gpu.total_mb)}` : "";
          if (unit) unit.textContent = total;
          else if (total) memory.append(el("span", { class: "unit", text: total }));

          const resident = system?.models?.resident || system?.models?.loaded || [];
          model.textContent = resident.length
            ? resident.map((entry) => entry.name).filter(Boolean).join(", ")
            : t("deck.noLocalModel");
        },
      };
    },
  },
  {
    panel: "memory",
    rail: "left",
    icon: ICONS.memory,
    build() {
      const nodes = reading(NOTHING, t("memory.nodes"));
      const detail = note("");
      return {
        body: [nodes, detail],
        update({ memory }) {
          if (!memory) return;
          nodes.querySelector(".num").textContent = fmt.number(memory.total_nodes ?? 0);
          detail.textContent = Number.isFinite(memory.total_tokens)
            ? t("memory.tokensNote", { n: fmt.number(memory.total_tokens) })
            : "";
        },
      };
    },
  },
  {
    panel: "security",
    rail: "left",
    icon: ICONS.security,
    build() {
      const chips = el("div", { class: "widget-chips" });
      const detail = note("");
      return {
        body: [chips, detail],
        update({ security }) {
          if (!security) return;
          const waiting = (security.pending || []).length;
          const level = security.level || "";
          // Which level is in force and whether anything is queued are true at
          // different times, so they get a chip each. Toned together, a gate
          // that is switched off reads as healthy for as long as the queue
          // happens to be empty, which is exactly when nobody checks.
          chips.replaceChildren(
            ...[
              chip(level || NOTHING, LEVEL_TONES[level] ?? null),
              waiting && chip(t("security.waitingShort", { n: waiting }), "warn"),
            ].filter(Boolean),
          );
          detail.textContent = waiting
            ? t("security.waitingCount", { n: waiting })
            : t("security.nothingWaiting");
        },
      };
    },
  },
  {
    panel: "passive",
    rail: "left",
    icon: ICONS.microphone,
    build() {
      const state = el("div", { class: "widget-body" });
      const detail = note("");
      return {
        body: [state, detail],
        update({ passive }) {
          if (!passive) return;
          state.replaceChildren(
            chip(
              passive.enabled ? t("passive.recording") : t("passive.off"),
              passive.enabled ? "bad" : null,
            ),
          );
          detail.textContent = passive.enabled
            ? t("passive.undigested", { n: passive.undigested_count ?? 0 })
            : t("passive.offNote");
        },
      };
    },
  },

  /* ── The right rail ─────────────────────────────────────────────── */

  {
    panel: "conversation",
    rail: "right",
    icon: ICONS.conversation,
    build() {
      const said = el("div", { class: "widget-note" });
      const replied = el("div", { class: "widget-note" });
      const when = note("");
      return {
        body: [said, replied, when],
        // A card with no turn in it has nothing to grow into, and the rail
        // needs to know that: told to stretch anyway it becomes a third of a
        // rail of empty box, which is the hole it was meant to close wearing
        // a border.
        empty: ({ status }) => !status?.last_turn,
        update({ status }) {
          const turn = status?.last_turn;
          if (!turn) {
            said.textContent = t("deck.noTurns");
            replied.textContent = "";
            when.textContent = "";
            return;
          }
          said.textContent = turn.transcript || NOTHING;
          replied.textContent = turn.reply || NOTHING;
          when.textContent = Number.isFinite(turn.total_ms) ? fmt.ms(turn.total_ms) : "";
        },
      };
    },
  },
  {
    panel: "tools",
    rail: "right",
    tile: true,
    icon: ICONS.tools,
    build() {
      const count = reading(NOTHING);
      return {
        body: [count],
        update({ tools }) {
          if (!tools) return;
          count.querySelector(".num").textContent = String((tools.tools || []).length);
        },
      };
    },
  },
  {
    panel: "mcp",
    rail: "right",
    tile: true,
    icon: ICONS.mcp,
    build() {
      const count = reading(NOTHING);
      return {
        body: [count],
        update({ tools }) {
          if (!tools) return;
          const servers = tools.servers || [];
          const connected = servers.filter((server) => server.connected).length;
          count.querySelector(".num").textContent = servers.length
            ? `${connected}/${servers.length}`
            : "0";
        },
      };
    },
  },
  {
    panel: "llm-routes",
    rail: "right",
    tile: true,
    icon: ICONS.llm,
    build() {
      const active = el("div", { class: "widget-note", text: NOTHING });
      return {
        body: [active],
        update({ routes }) {
          if (!routes) return;
          const chat = (routes.effective_chains?.chat || []).find((entry) => entry.active);
          active.textContent = chat ? chat.model || chat.name : NOTHING;
        },
      };
    },
  },
  {
    panel: "crew",
    rail: "right",
    tile: true,
    icon: ICONS.crew,
    build() {
      const count = reading(NOTHING);
      return {
        body: [count],
        update({ crew }) {
          if (!crew) return;
          if (!crew.configured) {
            count.querySelector(".num").textContent = NOTHING;
            return;
          }
          const agents = crew.agents || [];
          const working = agents.filter((agent) => agent.total > 0).length;
          count.querySelector(".num").textContent = crew.reachable
            ? `${working}/${agents.length}`
            : NOTHING;
        },
      };
    },
  },
  {
    panel: "logs",
    rail: "right",
    tile: true,
    icon: ICONS.logs,
    build() {
      const count = reading(NOTHING);
      return {
        body: [count],
        update({ logs }) {
          if (!logs) return;
          count.querySelector(".num").textContent = String((logs.entries || []).length);
        },
      };
    },
  },
];

/* ── Building one ──────────────────────────────────────────────────── */

export function buildWidget(definition, onOpen) {
  const built = definition.build();
  const name = t(`nav.${definition.panel === "llm-routes" ? "llm" : definition.panel}`);

  const node = el(
    "article",
    {
      class: `widget${definition.tile ? " widget-tile" : ""}`,
      dataset: { panel: definition.panel },
    },
    [
      el("header", { class: "widget-head" }, [
        definition.icon && el("span", { class: "widget-icon" }, [icon(definition.icon)]),
        el("span", { class: "widget-title", text: name }),
        el(
          "button",
          {
            type: "button",
            class: "widget-open",
            "aria-label": t("deck.openNamed", { name }),
            onclick: () => onOpen(definition.panel),
          },
          [icon(ICONS.open)],
        ),
      ]),
      el("div", { class: "widget-body" }, built.body),
    ],
  );

  return {
    node,
    update(snapshot) {
      built.update(snapshot);
      // Whether a card has anything to show is a layout fact as well as a
      // reading, so it is written where a stylesheet can see it.
      if (built.empty) node.dataset.empty = built.empty(snapshot) ? "true" : "false";
    },
  };
}
