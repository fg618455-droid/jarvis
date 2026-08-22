/* Bootstrap: the shell, the navigation, the live header, and the router. */

import { api } from "./api.js";
import * as fmt from "./fmt.js";
import { language, languages, setLanguage, t } from "./i18n.js";
import { Router } from "./router.js";
import { live } from "./sse.js";
import { ICONS, el, icon, toast } from "./ui.js";

/* Eleven destinations in one column read as a list to be searched. Grouped
   by what they are for, they read as a map: what is happening now, what the
   assistant knows, and how the machine is set up. */
const NAV_GROUPS = [
  {
    name: "live",
    views: [
      { name: "overview", icon: ICONS.overview },
      { name: "conversation", icon: ICONS.conversation },
      { name: "passive", icon: ICONS.microphone },
      { name: "crew", icon: ICONS.crew },
    ],
  },
  {
    name: "knowledge",
    views: [
      { name: "memory", icon: ICONS.memory },
      { name: "tools", icon: ICONS.tools },
    ],
  },
  {
    name: "operations",
    views: [
      { name: "security", icon: ICONS.security },
      { name: "llm", icon: ICONS.llm },
      { name: "system", icon: ICONS.system },
      { name: "logs", icon: ICONS.logs },
      { name: "settings", icon: ICONS.settings },
    ],
  },
];

const ROUTES = {
  overview: () => import("./views/overview.js"),
  memory: () => import("./views/memory.js"),
  conversation: () => import("./views/conversation.js"),
  passive: () => import("./views/passive.js"),
  tools: () => import("./views/tools.js"),
  security: () => import("./views/security.js"),
  system: () => import("./views/system.js"),
  logs: () => import("./views/logs.js"),
  llm: () => import("./views/llm.js"),
  crew: () => import("./views/crew.js"),
  settings: () => import("./views/settings.js"),
};

const dom = {
  sidebar: document.getElementById("sidebar"),
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
};

const state = {
  status: null,
  connected: false,
  pendingConfirmations: 0,
};

/* ── Navigation ────────────────────────────────────────────────────── */

const navButtons = new Map();

function buildSidebar(router) {
  dom.sidebar.replaceChildren();
  navButtons.clear();

  for (const group of NAV_GROUPS) {
    const label = t(`nav.group.${group.name}`);
    const section = el("div", { class: "nav-group", role: "group", "aria-label": label }, [
      el("span", { class: "nav-group-label", text: label }),
    ]);

    for (const view of group.views) {
      const badge = el("span", { class: "badge" });
      const button = el(
        "button",
        {
          class: "nav-item",
          type: "button",
          onclick: () => router.go(view.name),
        },
        [icon(view.icon), el("span", { class: "text", text: t(`nav.${view.name}`) }), badge],
      );
      button._badge = badge;
      navButtons.set(view.name, button);
      section.append(button);
    }

    dom.sidebar.append(section);
  }

  const foot = el("div", { class: "sidebar-foot", id: "sidebar-foot" });
  dom.sidebar.append(foot);
  paintFoot();
}

function markCurrent(name) {
  for (const [view, button] of navButtons) {
    if (view === name) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
}

function setBadge(view, text) {
  const button = navButtons.get(view);
  if (button) button._badge.textContent = text || "";
}

/* ── Header ────────────────────────────────────────────────────────── */

function paintHeader() {
  dom.uptimeLabel.textContent = t("common.uptime");
  dom.lastTurnLabel.textContent = t("common.lastTurn");
  const passive = state.status?.passive;
  dom.passive.hidden = !passive?.enabled;
  dom.passiveText.textContent = passive?.enabled ? t("passive.recording") : "";
  // A conversation means the wake word is not being asked for, which is
  // worth saying on every view rather than only on the one that set it.
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

  dom.dot.dataset.phase = status.phase;
  dom.phase.textContent = state.connected
    ? t(`phase.${status.phase}`)
    : t("common.reconnecting");
  dom.uptime.textContent = fmt.seconds(status.uptime_seconds);
  dom.lastTurn.textContent = status.last_turn ? fmt.ms(status.last_turn.total_ms) : "—";
}

function paintFoot() {
  const foot = document.getElementById("sidebar-foot");
  if (!foot) return;
  const models = state.status?.models || {};
  foot.replaceChildren(
    el("span", { text: models.chat || "—" }),
    el("span", { class: "num", id: "foot-vram", text: "—" }),
  );
}

/* Graphics memory is the ceiling of this setup, so it sits in the frame
   rather than behind a tab. It is polled rather than pushed because it is a
   reading of the machine, not an event in the assistant. */
async function pollVram() {
  try {
    const system = await api.system();
    const gpu = system.gpu;
    const node = document.getElementById("foot-vram");
    if (!node) return;
    if (gpu?.used_mb && gpu?.total_mb) {
      node.textContent = `${fmt.megabytes(gpu.used_mb)} / ${fmt.megabytes(gpu.total_mb)}`;
    } else if (gpu?.total_mb) {
      node.textContent = fmt.megabytes(gpu.total_mb);
    } else {
      node.textContent = "—";
    }
  } catch {
    /* The daemon may be down; the header already says so. */
  }
}

/* ── Live wiring ───────────────────────────────────────────────────── */

function wireLive(router) {
  live.on("connection", ({ connected }) => {
    state.connected = connected;
    paintHeader();
  });

  live.on("status", (status) => {
    state.status = status;
    paintHeader();
    paintFoot();
    refreshPending();
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

  live.on("confirmation", () => {
    state.pendingConfirmations += 1;
    setBadge("security", String(state.pendingConfirmations));
    toast(t("security.pending"));
    if (router.current !== "security") return;
  });

  live.on("confirmation_resolved", () => {
    state.pendingConfirmations = Math.max(0, state.pendingConfirmations - 1);
    setBadge("security", state.pendingConfirmations ? String(state.pendingConfirmations) : "");
  });

  live.start();
}

async function refreshPending() {
  try {
    const security = await api.security();
    state.pendingConfirmations = security.pending.length;
    setBadge("security", state.pendingConfirmations ? String(state.pendingConfirmations) : "");
  } catch {
    /* Nothing to show while the daemon is unreachable. */
  }
}

/* ── Start ─────────────────────────────────────────────────────────── */

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

function main() {
  const router = new Router(dom.root, ROUTES, "overview");
  router.onChange((name) => {
    markCurrent(name);
    document.title = `${t(`nav.${name}`)} · ${t("app.title")}`;
  });

  document.documentElement.lang = language();
  buildSidebar(router);
  buildLanguagePicker(() => {
    buildSidebar(router);
    markCurrent(router.current);
    paintHeader();
    router.go(router.current);
  });

  paintHeader();
  wireLive(router);
  router.start();

  api.status().then((status) => {
    state.status = status;
    paintHeader();
    paintFoot();
    pollVram();
  }).catch(() => paintHeader());

  setInterval(pollVram, 5000);
  setInterval(() => {
    if (state.status) {
      state.status.uptime_seconds += 1;
      dom.uptime.textContent = fmt.seconds(state.status.uptime_seconds);
    }
  }, 1000);
}

main();
