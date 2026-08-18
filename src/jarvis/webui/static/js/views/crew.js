/* Mission Control: recent activity from the agent crew running on the NAS.

   The crew lives outside this daemon, on a machine that is not always on.
   Every state the endpoint can report — no endpoint configured, endpoint
   configured but not answering, or live data — gets its own plain message
   rather than a table that quietly shows nothing. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, table } from "../ui.js";

const POLL_MS = 10000;

const STATUS_TONE = { success: "ok", failure: "bad", partial: "warn" };

function statusChip(status) {
  return chip(t(`crew.status.${status}`) || status, STATUS_TONE[status]);
}

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("crew.title") }),
    el("p", { text: t("crew.lead") }),
  ]);
  const body = el("div");
  root.append(head, body);

  async function refresh() {
    const payload = await api.crew();
    paint(body, payload);
  }

  await refresh().catch(() => paint(body, { configured: false, reachable: false, entries: [], agents: [] }));
  const timer = setInterval(() => refresh().catch(() => {}), POLL_MS);
  return () => clearInterval(timer);
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

  const agentsRow = el("div", { class: "grid" });
  const activityCard = el("section", { class: "card" });
  container.append(agentsRow, activityCard);

  paintAgents(agentsRow, payload.agents || []);
  paintActivity(activityCard, payload.entries || []);
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
