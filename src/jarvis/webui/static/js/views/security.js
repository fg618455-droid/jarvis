/* Security: the policy, what is waiting for a decision, and what was decided. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { chip, clear, el, empty, table, toast } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("security.title") }),
    el("p", { text: t("security.lead") }),
  ]);

  const pendingCard = el("section", { class: "card" });
  const policyRow = el("div", { class: "grid" });
  const logCard = el("section", { class: "card" });

  root.append(head, pendingCard, policyRow, logCard);

  async function refresh() {
    const payload = await api.security();
    paintPending(pendingCard, payload.pending || [], refresh);
    paintPolicy(policyRow, payload);
    paintLog(logCard, payload.decisions || []);
  }

  await refresh();

  const offAsk = live.on("confirmation", () => refresh().catch(() => {}));
  const offResolved = live.on("confirmation_resolved", () => refresh().catch(() => {}));
  // A request expires on its own, so the countdown needs a clock of its own.
  const timer = setInterval(() => refresh().catch(() => {}), 2000);

  return () => {
    offAsk();
    offResolved();
    clearInterval(timer);
  };
}

function paintPending(container, pending, refresh) {
  clear(container);
  container.append(el("header", {}, [el("h2", { text: t("security.pending") })]));

  if (!pending.length) {
    container.append(empty(t("security.noPending")));
    return;
  }

  for (const request of pending) {
    const args = el("pre");
    args.textContent = JSON.stringify(request.action_args, null, 2);

    container.append(
      el("div", { class: "confirm-card" }, [
        el("strong", { text: request.action_name }),
        args,
        el("span", {
          class: "muted num",
          text: `${Math.ceil(request.seconds_left)} s`,
        }),
        el("div", { class: "confirm-actions" }, [
          el("button", {
            class: "btn primary",
            type: "button",
            text: t("security.approve"),
            onclick: () => decide(request.request_id, true, refresh),
          }),
          el("button", {
            class: "btn danger",
            type: "button",
            text: t("security.deny"),
            onclick: () => decide(request.request_id, false, refresh),
          }),
        ]),
      ]),
    );
  }
}

async function decide(requestId, approved, refresh) {
  try {
    await api.decide(requestId, approved);
  } catch (error) {
    toast(error.message, "bad");
  }
  await refresh();
}

function paintPolicy(container, payload) {
  clear(container);

  container.append(
    el("section", { class: "card" }, [
      el("header", {}, [el("h2", { text: t("security.level") })]),
      el("div", { class: "stat" }, [
        el("span", { class: "stat-value small", text: payload.level || "—" }),
        el("span", {
          class: "stat-sub",
          text: `${t("security.timeout")}: ${payload.timeout_seconds ?? "—"} s`,
        }),
      ]),
    ]),

    el("section", { class: "card" }, [
      el("header", {}, [el("h2", { text: t("security.channels") })]),
      el(
        "div",
        { class: "rows" },
        (payload.channels || []).map((channel) =>
          el("div", { class: "row" }, [
            el("span", { class: "key", text: channel.name }),
            el("span", { class: "val" }, [
              chip(
                channel.available === null
                  ? t("common.unknown")
                  : channel.available
                    ? t("security.available")
                    : t("security.unavailable"),
                channel.available ? "ok" : channel.available === null ? null : "warn",
              ),
            ]),
          ]),
        ),
      ),
    ]),
  );
}

const OUTCOME_KEYS = {
  approved: "security.outcome.approved",
  denied: "security.outcome.denied",
  "timed out": "security.outcome.timedOut",
};

function paintLog(container, decisions) {
  clear(container);
  container.append(el("header", {}, [el("h2", { text: t("security.decisions") })]));

  if (!decisions.length) {
    container.append(empty(t("security.noDecisions")));
    return;
  }

  container.append(
    el("div", { class: "scroll" }, [
      table(
        [
          { label: t("common.name"), render: (entry) => entry.action_name },
          {
            label: t("common.actions"),
            render: (entry) =>
              chip(
                t(OUTCOME_KEYS[entry.outcome] || "common.unknown"),
                entry.outcome === "approved" ? "ok" : "warn",
              ),
          },
          { label: "", numeric: true, render: (entry) => fmt.dateTime(entry.at) },
        ],
        [...decisions].reverse(),
      ),
    ]),
  );
}
