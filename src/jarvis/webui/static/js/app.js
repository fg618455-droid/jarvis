/* Bootstrap: the frame, the live header, and the two places there are.

   There is one interface and one way out of it. The deck holds the face and
   every reading around it; Settings is the only thing that replaces the deck
   rather than opening over it, because editing the configuration is the one
   task that is not about watching the assistant work.

   Everything else is an address on the deck. `#/tools` opens the deck with
   the tools panel over its right rail, which means every bookmark and every
   link the views already hand each other still arrives somewhere sensible,
   and the face is behind all of them. */

import { api } from "./api.js";
import { mountDeck, PANELS } from "./deck.js";
import * as fmt from "./fmt.js";
import { language, languages, setLanguage, t } from "./i18n.js";
import { live } from "./sse.js";
import { applyTheme, activeTheme, THEMES } from "./theme.js";
import { el, icon, ICONS, toast } from "./ui.js";

const DECK = "deck";
const SETTINGS = "settings";

/* Addresses that used to be pages of their own. The face and the overview
   are the deck now; the rest are panels on it, so they keep their names. */
const ALIASES = {
  "": DECK,
  overview: DECK,
  visualizer: DECK,
  llm: "llm-routes",
};

const dom = {
  main: document.querySelector(".main"),
  root: document.getElementById("view-root"),
  dot: document.getElementById("phase-dot"),
  phase: document.getElementById("phase-text"),
  passive: document.getElementById("passive-indicator"),
  passiveText: document.getElementById("passive-text"),
  conversation: document.getElementById("conversation-indicator"),
  conversationText: document.getElementById("conversation-indicator-text"),
  uptimeLabel: document.getElementById("uptime-label"),
  uptime: document.getElementById("uptime-value"),
  lastTurnLabel: document.getElementById("last-turn-label"),
  lastTurn: document.getElementById("last-turn-value"),
  language: document.getElementById("language"),
  theme: document.getElementById("theme"),
  settingsButton: document.getElementById("settings-button"),
};

const state = {
  status: null,
  connected: false,
};

let deck = null;
let settingsCleanup = null;

/* ── Where we are ──────────────────────────────────────────────────── */

function requested() {
  const raw = location.hash.replace(/^#\/?/, "").split("?")[0];
  const name = ALIASES[raw] ?? raw;
  if (name === SETTINGS) return SETTINGS;
  return PANELS.includes(name) ? name : DECK;
}

function go(name) {
  const target = `#/${name}`;
  if (location.hash === target) render();
  else location.hash = target;
}

async function render() {
  const where = requested();

  if (where === SETTINGS) {
    await showSettings();
    return;
  }

  await showDeck();
  if (where === DECK) deck.closePanel();
  else deck.openPanel(where, { onClose: () => go(DECK) });

  markSettings(false);
  document.title = `${t(`nav.${where === DECK ? "deck" : where === "llm-routes" ? "llm" : where}`)} · ${t("app.title")}`;
}

async function showDeck() {
  if (deck) return;
  await teardownSettings();
  dom.main.classList.add("is-deck");
  dom.root.replaceChildren();
  const host = el("div");
  dom.root.append(host);
  deck = mountDeck(host, { onOpenPanel: (panel) => go(panel) });
  deck.paintPhase(state.status?.phase || "idle", state.connected);
}

function teardownDeck() {
  if (!deck) return;
  deck.destroy();
  deck = null;
  dom.main.classList.remove("is-deck");
}

async function teardownSettings() {
  if (!settingsCleanup) return;
  try {
    settingsCleanup();
  } catch (error) {
    console.error("settings cleanup failed", error);
  }
  settingsCleanup = null;
}

async function showSettings() {
  teardownDeck();
  await teardownSettings();
  markSettings(true);
  document.title = `${t("nav.settings")} · ${t("app.title")}`;

  dom.root.replaceChildren();
  const view = el("div", { class: "view view-settings" });
  view.append(
    el(
      "button",
      {
        type: "button",
        class: "settings-back",
        onclick: () => go(DECK),
      },
      [icon(ICONS.back), el("span", { text: t("deck.backToDeck") })],
    ),
  );
  dom.root.append(view);

  try {
    const module = await import("./views/settings.js");
    settingsCleanup = (await module.mount(view)) || null;
  } catch (error) {
    console.error("settings failed", error);
    view.append(el("div", { class: "empty", text: String(error.message || error) }));
  }
}

function markSettings(current) {
  if (current) dom.settingsButton.setAttribute("aria-current", "page");
  else dom.settingsButton.removeAttribute("aria-current");
}

/* ── Header ────────────────────────────────────────────────────────── */

function paintHeader() {
  dom.uptimeLabel.textContent = t("common.uptime");
  dom.lastTurnLabel.textContent = t("common.lastTurn");
  const passive = state.status?.passive;
  dom.passive.hidden = !passive?.enabled;
  dom.passiveText.textContent = passive?.enabled ? t("passive.recording") : "";
  // A conversation means the wake word is not being asked for, which is
  // worth saying wherever the page happens to be rather than only where it
  // was switched on.
  const conversation = state.status?.conversation;
  dom.conversation.hidden = !conversation?.active;
  dom.conversationText.textContent = conversation?.active
    ? t("conversation.modeTitle")
    : "";

  if (!state.connected && !state.status) {
    dom.dot.dataset.phase = "offline";
    dom.phase.textContent = t("phase.offline");
    return;
  }

  const status = state.status;
  if (!status) return;

  if (status.daemon_running === false) {
    dom.dot.dataset.phase = "offline";
    dom.phase.textContent = t("phase.offline");
    dom.uptime.textContent = "—";
    dom.lastTurn.textContent = "—";
    deck?.paintPhase("offline", state.connected);
    return;
  }

  dom.dot.dataset.phase = status.phase;
  dom.phase.textContent = state.connected
    ? t(`phase.${status.phase}`)
    : t("common.reconnecting");
  dom.uptime.textContent = fmt.seconds(status.uptime_seconds);
  dom.lastTurn.textContent = status.last_turn ? fmt.ms(status.last_turn.total_ms) : "—";
  deck?.paintPhase(status.phase, state.connected);
}

/* ── Live wiring ───────────────────────────────────────────────────── */

function wireLive() {
  live.on("connection", ({ connected }) => {
    state.connected = connected;
    paintHeader();
  });

  live.on("status", (status) => {
    state.status = status;
    paintHeader();
  });

  live.on("phase", (phase) => {
    if (!state.status) return;
    Object.assign(state.status, phase);
    paintHeader();
  });

  live.on("passive", (passive) => {
    if (!state.status) state.status = {};
    state.status.passive = passive;
    paintHeader();
  });

  live.on("conversation", (conversation) => {
    if (!state.status) state.status = {};
    state.status.conversation = conversation;
    paintHeader();
  });

  live.on("turn", (turn) => {
    if (state.status) state.status.last_turn = turn;
    paintHeader();
  });

  live.on("error", ({ message }) => toast(message, "bad"));

  // Something is waiting for an answer, and the widget that says how many
  // reads it from the shared snapshot, so the snapshot is retaken rather
  // than waiting up to ten seconds to agree with the toast.
  live.on("confirmation", () => {
    toast(t("security.pending"));
    deck?.refresh();
  });
  live.on("confirmation_resolved", () => deck?.refresh());

  live.start();
}

/* ── Pickers ───────────────────────────────────────────────────────── */

function buildThemePicker() {
  dom.theme.replaceChildren(
    ...THEMES.map((theme) =>
      el("option", {
        value: theme.id,
        text: theme.label,
        selected: theme.id === activeTheme(),
      }),
    ),
  );
  dom.theme.addEventListener("change", () => applyTheme(dom.theme.value));
}

function buildLanguagePicker(rerender) {
  dom.language.replaceChildren(
    ...languages().map((code) =>
      el("option", { value: code, text: code.toUpperCase(), selected: code === language() }),
    ),
  );
  dom.language.addEventListener("change", () => {
    setLanguage(dom.language.value);
    document.documentElement.lang = dom.language.value;
    rerender();
  });
}

/* ── Start ─────────────────────────────────────────────────────────── */

function main() {
  applyTheme(activeTheme());
  document.documentElement.lang = language();

  dom.settingsButton.append(icon(ICONS.settings));
  dom.settingsButton.setAttribute("aria-label", t("nav.settings"));
  dom.settingsButton.addEventListener("click", () => go(SETTINGS));

  buildThemePicker();
  buildLanguagePicker(() => {
    dom.settingsButton.setAttribute("aria-label", t("nav.settings"));
    // The deck names every widget, so a language change rebuilds it rather
    // than translating eleven cards in place.
    teardownDeck();
    paintHeader();
    render();
  });

  paintHeader();
  wireLive();

  window.addEventListener("hashchange", () => render());
  render();

  api.status().then((status) => {
    state.status = status;
    paintHeader();
  }).catch(() => paintHeader());

  setInterval(() => {
    if (state.status?.daemon_running !== false && Number.isFinite(state.status?.uptime_seconds)) {
      state.status.uptime_seconds += 1;
      dom.uptime.textContent = fmt.seconds(state.status.uptime_seconds);
    }
  }, 1000);
}

main();
