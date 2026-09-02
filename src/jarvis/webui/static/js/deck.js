/* The deck: one page, built once, and never torn down while you use it.

   The face is the interface rather than a destination inside it, so the deck
   is mounted once and outlives every panel opened over it. That is the whole
   reason the router does not rebuild it: a face rebuilt on every navigation
   would reload its frame, restart its animation, and blink at the reader
   each time they looked at a different reading.

   What a panel shows is not new. Each one mounts the view module that has
   always held that detail, into the panel's body, exactly as the router used
   to mount it into the page. The views did not need rewriting to stop being
   pages; they only needed somewhere else to be. */

import { api } from "./api.js";
import { mountFace } from "./face.js";
import { t } from "./i18n.js";
import { createMic } from "./mic.js";
import { live } from "./sse.js";
import { el, icon, ICONS, toast } from "./ui.js";
import { buildWidget, WIDGETS } from "./widgets.js";

/* The detail behind each widget. A panel name is an address, so these are
   also the hashes the interface answers to. */
const PANEL_VIEWS = {
  conversation: () => import("./views/conversation.js"),
  memory: () => import("./views/memory.js"),
  tools: () => import("./views/tools.js"),
  mcp: () => import("./views/mcp.js"),
  security: () => import("./views/security.js"),
  system: () => import("./views/system.js"),
  "llm-routes": () => import("./views/llm.js"),
  logs: () => import("./views/logs.js"),
  passive: () => import("./views/passive.js"),
  crew: () => import("./views/crew.js"),
  briefing: () => import("./views/briefing.js"),
};

export const PANELS = Object.keys(PANEL_VIEWS);

/* How often the shared reading is retaken. Slower than the event stream,
   which carries anything that actually changed the moment it does; this is
   for the readings nothing pushes, like graphics memory. */
const SNAPSHOT_MS = 10000;

export function mountDeck(root, { onOpenPanel } = {}) {
  root.classList.add("deck");

  const railLeft = el("aside", {
    class: "deck-rail deck-rail-left",
    role: "group",
    "aria-label": t("deck.railLeft"),
  });
  const railRight = el("aside", {
    class: "deck-rail deck-rail-right",
    role: "group",
    "aria-label": t("deck.railRight"),
  });
  const stage = el("section", { class: "face-stage" });

  root.append(railLeft, stage, railRight);

  /* ── The face ───────────────────────────────────────────────────── */

  const mic = createMic({
    onState: (state) => {
      face.micButton.setAttribute("aria-pressed", state === "listening" ? "true" : "false");
    },
    onError: () => toast(t("conversation.micRefused"), "bad"),
  });

  const face = mountFace(stage, {
    onSend: async (text) => {
      try {
        await api.chat(text, false);
      } catch (error) {
        // 409 is the daemon saying a turn is already running, which is a
        // fact about the assistant rather than a failure of the page.
        toast(error.status === 409 ? t("conversation.busy") : error.message, "bad");
      }
    },
    onMicToggle: () => mic.toggle(),
  });

  /* ── The widgets ────────────────────────────────────────────────── */

  const built = [];
  const tiles = el("div", { class: "widget-tiles" });

  for (const definition of WIDGETS) {
    const widget = buildWidget(definition, (panel) => onOpenPanel(panel));
    built.push(widget);
    if (definition.rail === "left") railLeft.append(widget.node);
    else if (definition.tile) tiles.append(widget.node);
    else railRight.append(widget.node);
  }
  railRight.append(tiles);

  /* ── The shared reading ─────────────────────────────────────────── */

  const snapshot = {};

  /* One source failing is not the deck failing. Every reading is fetched
     independently and a rejection leaves that widget on its last honest
     value rather than blanking the whole rail. */
  async function into(key, call) {
    try {
      snapshot[key] = await call();
    } catch {
      /* The widget keeps showing nothing rather than showing a guess. */
    }
  }

  async function takeReading() {
    await Promise.all([
      into("status", () => api.status()),
      into("tools", () => api.tools()),
      into("security", () => api.security()),
      into("system", () => api.system()),
      // The node count and its token weight, which is what the memory widget
      // shows and what its panel is built from. `/api/stats` is the diary's
      // tally and carries neither, so a widget fed from there reads a field
      // that is not in the payload and paints a confident zero.
      into("memory", () => api.graphStats()),
      into("routes", () => api.llmRoutes()),
      into("logs", () => api.logs(200)),
      into("passive", () => api.passive("", 1)),
      into("briefing", () => api.briefing()),
    ]);
    paint();
  }

  function paint() {
    // The assistant is called after its wake word, which `/api/status` already
    // reports. The face therefore reads its own name from the reading every
    // widget is painted from rather than from an endpoint of its own.
    const wakeWord = snapshot.status?.audio?.wake_word;
    if (wakeWord) face.setName(wakeWord.charAt(0).toUpperCase() + wakeWord.slice(1));

    for (const widget of built) {
      try {
        widget.update(snapshot);
      } catch (error) {
        console.error("a widget failed to paint", error);
      }
    }
  }

  /* Mission Control reaches a machine that is often asleep, and the daemon
     already takes one reading for everyone watching. The deck therefore
     asks once and then follows the event the daemon publishes, rather than
     adding its own timer against a NAS. */
  into("crew", () => api.crew()).then(paint);

  const offCrew = live.on("crew", (reading) => {
    snapshot.crew = reading;
    paint();
  });
  const offStatus = live.on("status", (status) => {
    snapshot.status = status;
    paint();
  });
  const offTurn = live.on("turn", (turn) => {
    if (snapshot.status) snapshot.status.last_turn = turn;
    paint();
  });
  const offPassive = live.on("passive", (passive) => {
    snapshot.passive = { ...(snapshot.passive || {}), ...passive };
    paint();
  });

  takeReading();
  const timer = setInterval(takeReading, SNAPSHOT_MS);

  /* ── The panel ──────────────────────────────────────────────────── */

  let panelNode = null;
  let panelCleanup = null;
  let panelName = null;

  async function openPanel(name, { onClose } = {}) {
    if (!PANEL_VIEWS[name]) return;
    if (panelName === name) return;
    closePanel();
    panelName = name;

    // The panel names itself on its own body, so a view with its own idea of
    // how tall it is can say so without the panel having to guess. The
    // conversation is the one that does: it fills its container and scrolls
    // the exchange inside itself rather than being scrolled by the panel.
    const body = el("div", { class: `panel-body panel-body-${name}` });
    const view = el("div", { class: "view" });
    body.append(view);

    panelNode = el("div", { class: "panel", role: "dialog", "aria-label": panelTitle(name) }, [
      el("div", { class: "panel-head" }, [
        el("h1", { class: "panel-title", text: panelTitle(name) }),
        el(
          "button",
          {
            type: "button",
            class: "panel-close",
            "aria-label": t("deck.close"),
            onclick: () => onClose && onClose(),
          },
          [icon(ICONS.close)],
        ),
      ]),
      body,
    ]);
    root.append(panelNode);

    try {
      const module = await PANEL_VIEWS[name]();
      panelCleanup = (await module.mount(view)) || null;
    } catch (error) {
      console.error(`panel ${name} failed`, error);
      view.append(el("div", { class: "empty", text: String(error.message || error) }));
    }
  }

  function closePanel() {
    if (panelCleanup) {
      try {
        panelCleanup();
      } catch (error) {
        console.error("panel cleanup failed", error);
      }
      panelCleanup = null;
    }
    if (panelNode) {
      panelNode.remove();
      panelNode = null;
    }
    panelName = null;
  }

  function panelTitle(name) {
    return t(`nav.${name === "llm-routes" ? "llm" : name}`);
  }

  return {
    openPanel,
    closePanel,
    refresh: takeReading,
    get openPanelName() {
      return panelName;
    },
    paintPhase: face.paintPhase,
    setName: face.setName,
    destroy() {
      closePanel();
      clearInterval(timer);
      offCrew();
      offStatus();
      offTurn();
      offPassive();
      // The face paints itself and polls for its own reading, so removing the
      // node it drew into is not enough to stop either.
      face.destroy();
      mic.stop();
    },
  };
}
