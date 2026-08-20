/* Mission Control: recent activity from the agent crew running on the NAS.

   The crew lives outside this daemon, on a machine that is not always on.
   Every state the endpoint can report — no endpoint configured, endpoint
   configured but not answering, or live data — gets its own plain message
   rather than a table that quietly shows nothing. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, table, toast } from "../ui.js";

const POLL_MS = 10000;

const STATUS_TONE = { success: "ok", failure: "bad", partial: "warn" };

// Must match AGENT_THREADS in ask_crew.py — the roster the Telegram bridge
// and this chat panel both delegate to.
const CREW_AGENTS = ["jarvis", "dev", "research", "assistant", "schule", "scribe", "reach"];

function statusChip(status) {
  return chip(t(`crew.status.${status}`) || status, STATUS_TONE[status]);
}

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("crew.title") }),
    el("p", { text: t("crew.lead") }),
  ]);
  // The chat panel is built once and left alone by the periodic refresh
  // below — clearing and rebuilding it every POLL_MS would drop whatever
  // the user was mid-typing and wipe the conversation so far.
  const chatCard = el("section", { class: "card" });
  const body = el("div");
  root.append(head, chatCard, body);

  const chat = buildChatPanel(chatCard);

  async function refresh() {
    const payload = await api.crew();
    chat.setAvailable(Boolean(payload.configured && payload.reachable));
    paint(body, payload);
  }

  await refresh().catch(() =>
    paint(body, { configured: false, reachable: false, entries: [], agents: [], daily: [] }),
  );
  const timer = setInterval(() => refresh().catch(() => {}), POLL_MS);
  return () => clearInterval(timer);
}

function buildChatPanel(container) {
  container.append(el("header", {}, [el("h2", { text: t("crew.chatTitle") })]));

  const agentSelect = el(
    "select",
    {},
    CREW_AGENTS.map((agent) => el("option", { value: agent, text: agent })),
  );
  const input = el("input", { type: "text", placeholder: t("crew.chatPlaceholder") });
  const send = el("button", { class: "btn primary", type: "button", text: t("crew.chatSend") });
  const log = el("div", { class: "rows crew-chat-log" });

  function appendLine(agent, speaker, text) {
    log.append(
      el("div", { class: "row" }, [
        el("span", { class: "key", text: `${agent} · ${speaker}` }),
        el("span", { class: "val", text }),
      ]),
    );
    log.scrollTop = log.scrollHeight;
  }

  async function submit() {
    const message = input.value.trim();
    if (!message || send.disabled) return;
    const agent = agentSelect.value;

    appendLine(agent, t("crew.chatYou"), message);
    input.value = "";
    send.disabled = true;
    const previous = send.textContent;
    send.textContent = t("crew.chatSending");
    try {
      const result = await api.crewChat(agent, message);
      appendLine(
        agent, agent,
        result.reachable === false ? (result.error || t("crew.unreachable")) : (result.reply || "—"),
      );
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      send.disabled = false;
      send.textContent = previous;
      input.focus();
    }
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });

  container.append(el("div", { class: "composer-row" }, [agentSelect, input, send]), log);

  return {
    setAvailable(available) {
      agentSelect.disabled = !available;
      input.disabled = !available;
      send.disabled = !available;
    },
  };
}

function paint(container, payload) {
  clear(container);

  if (!payload.configured) {
    container.append(el("section", { class: "card" }, [empty(t("crew.notConfigured"))]));
    return;
  }

  if (!payload.reachable) {
    container.append(el("section", { class: "card" }, [empty(t("crew.unreachable"))]));
    return;
  }

  const heatmapCard = el("section", { class: "card" });
  const agentsRow = el("div", { class: "grid" });
  const activityCard = el("section", { class: "card" });
  container.append(heatmapCard, agentsRow, activityCard);

  paintHeatmap(heatmapCard, payload.daily || []);
  paintAgents(agentsRow, payload.agents || []);
  paintActivity(activityCard, payload.entries || []);
}

function paintHeatmap(container, daily) {
  container.append(el("header", {}, [el("h2", { text: t("crew.dailyActivity") })]));

  if (!daily.length) {
    container.append(empty(t("crew.empty")));
    return;
  }

  const max = Math.max(...daily.map((day) => day.count), 1);
  container.append(
    el(
      "div",
      { class: "heatgrid" },
      daily.map((day) =>
        el("span", {
          class: "heatcell",
          style: `opacity: ${day.count ? Math.max(0.18, day.count / max) : 0.06}`,
          title: `${day.date}: ${day.count}`,
        }),
      ),
    ),
  );
}

function paintAgents(container, agents) {
  if (!agents.length) {
    container.append(el("section", { class: "card" }, [empty(t("crew.empty"))]));
    return;
  }

  for (const agent of agents) {
    container.append(
      el("section", { class: "card" }, [
        el("header", {}, [el("h2", { text: agent.name })]),
        el("div", { class: "rows" }, [
          el("div", { class: "row" }, [
            el("span", { class: "key", text: t("crew.status.success") }),
            el("span", { class: "val num", text: String(agent.success) }),
          ]),
          el("div", { class: "row" }, [
            el("span", { class: "key", text: t("crew.status.partial") }),
            el("span", { class: "val num", text: String(agent.partial) }),
          ]),
          el("div", { class: "row" }, [
            el("span", { class: "key", text: t("crew.status.failure") }),
            el("span", { class: "val num", text: String(agent.failure) }),
          ]),
        ]),
      ]),
    );
  }
}

function paintActivity(container, entries) {
  container.append(
    el("header", {}, [
      el("h2", { text: t("crew.activity") }),
      el("span", { class: "aside", text: `${entries.length}` }),
    ]),
  );

  if (!entries.length) {
    container.append(empty(t("crew.empty")));
    return;
  }

  container.append(
    el("div", { class: "scroll" }, [
      table(
        [
          {
            label: t("crew.column.when"),
            numeric: true,
            render: (entry) => fmt.ago(Date.parse(entry.created_at) / 1000, t),
          },
          { label: t("crew.column.agent"), render: (entry) => entry.agent_name },
          { label: t("crew.column.status"), render: (entry) => statusChip(entry.status) },
          { label: t("crew.column.model"), render: (entry) => entry.model_used },
          {
            label: t("crew.column.task"),
            render: (entry) => fmt.truncate(entry.task_description, 90),
          },
        ],
        entries,
      ),
    ]),
  );
}
