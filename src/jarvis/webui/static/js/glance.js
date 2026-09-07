/* The line under the face, before anything has been said.

   A start screen that is a circle and a text box is honest and useless: it
   says the assistant is running and nothing about whether anything needs
   you. This is the four facts that would otherwise cost four panels — what
   today holds, whether the gate is where you left it, which model is
   answering, and how long the daemon has been up — on one line, in the place
   the eye already is.

   It is painted from the deck's shared snapshot rather than from endpoints
   of its own, so four readings here are not four more timers against the
   daemon and all four are looking at the same moment. Each one opens the
   panel it came from, because a reading that raises a question should be a
   way to the answer rather than a dead end.

   Nothing here invents a value. A source that failed or has not answered yet
   shows an em dash: a zero meaning "no answer" and a zero meaning "none" are
   very different facts, and on the gate they are the two that matter. */

import * as fmt from "./fmt.js";
import { t } from "./i18n.js";
import { chip, el } from "./ui.js";

const NOTHING = "—";

/* A gate that is switched off stops nothing, which is the one state on this
   line worth catching an eye. The two that stop something are toned as such;
   a level this does not name is painted plainly rather than guessed at. */
const LEVEL_TONES = {
  off: "warn",
  critical: "ok",
  paranoid: "ok",
};

/* Each reading, and where it comes from. `read` returns either a string, or
   a node when the value is a status rather than a number. */
const READINGS = [
  {
    key: "briefing",
    panel: "briefing",
    label: () => t("nav.briefing"),
    read: ({ briefing }) => {
      if (!briefing) return null;
      if (!briefing.available) return t("briefing.notConfigured");
      const items = briefing.items || [];
      return t("glance.itemsToday", { n: items.length });
    },
  },
  {
    key: "security",
    panel: null,
    label: () => t("nav.security"),
    read: ({ security }) => {
      if (!security) return null;
      const level = security.level || "";
      const waiting = (security.pending || []).length;
      // Which level is in force and whether anything is queued are true at
      // different times, so they get a chip each. Merged, the reassuring
      // tone would be showing at exactly the moment nobody looks: an empty
      // queue in front of a gate that stops nothing.
      return [
        chip(level || NOTHING, LEVEL_TONES[level] ?? null),
        waiting ? chip(t("security.waitingShort", { n: waiting }), "warn") : null,
      ].filter(Boolean);
    },
  },
  {
    key: "routes",
    panel: "llm-routes",
    label: () => t("nav.llm"),
    read: ({ routes }) => {
      if (!routes) return null;
      const chat = (routes.effective_chains?.chat || []).find((entry) => entry.active);
      return chat ? chat.model || chat.name : NOTHING;
    },
  },
  {
    key: "status",
    panel: "system",
    label: () => t("common.uptime"),
    read: ({ status }) => {
      if (!status) return null;
      if (status.daemon_running === false) return t("phase.offline");
      return Number.isFinite(status.uptime_seconds)
        ? fmt.seconds(status.uptime_seconds)
        : NOTHING;
    },
  },
];

export function createGlance(root, { onOpenPanel } = {}) {
  root.classList.add("stage-glance");
  root.setAttribute("role", "group");
  root.setAttribute("aria-label", t("glance.title"));

  const built = READINGS.map((reading) => {
    const value = el("span", { class: "stage-reading-value", text: NOTHING });
    const label = el("span", { class: "stage-reading-label", text: reading.label() });

    /* A reading with somewhere to go is the button; one without is plain
       text. The gate has no panel of its own any more — what it would have
       shown is on this stage already — so it is the one that stays text. */
    const node = reading.panel
      ? el("button", {
          class: "stage-reading",
          type: "button",
          dataset: { reading: reading.key },
          "aria-label": `${reading.label()}: ${t("deck.openNamed", { name: reading.label() })}`,
          onclick: () => onOpenPanel && onOpenPanel(reading.panel),
        }, [label, value])
      : el("div", {
          class: "stage-reading",
          dataset: { reading: reading.key },
        }, [label, value]);

    return { reading, node, value };
  });

  root.replaceChildren(...built.map((entry) => entry.node));

  return {
    node: root,
    update(snapshot) {
      for (const { reading, value } of built) {
        let painted;
        try {
          painted = reading.read(snapshot || {});
        } catch {
          painted = null;
        }
        if (painted === null || painted === undefined) {
          value.textContent = NOTHING;
        } else if (Array.isArray(painted)) {
          value.replaceChildren(...painted);
        } else {
          value.textContent = String(painted);
        }
      }
    },
  };
}
