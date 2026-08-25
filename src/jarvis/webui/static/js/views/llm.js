/* LLM routes: ordered chains, persisted cooldowns, and explicit probes. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, toast } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("llm.title") }),
    el("p", { text: t("llm.lead") }),
  ]);
  const actions = el("div", { class: "actions" });
  const backendCard = el("section", { class: "card" });
  const chains = el("div", { class: "grid" });
  const editorCard = el("section", { class: "card" });
  root.append(head, actions, backendCard, chains, editorCard);

  let payload = null;
  let editor = null;

  async function refresh() {
    payload = await api.llmRoutes();
    paintBackendSelectors(backendCard, payload);
    paintChains(chains, payload.chains || {});
    paintEditor(editorCard, payload.chains || {});
  }

  const probe = el("button", {
    class: "btn",
    type: "button",
    text: t("llm.probe"),
    onclick: async () => {
      probe.disabled = true;
      try {
        const result = await api.probeLlmRoutes();
        const working = (result.results || []).filter((item) => item.ok).length;
        toast(t("llm.probeDone", { n: working }));
        await refresh();
      } catch (error) {
        toast(error.message, "bad");
      } finally {
        probe.disabled = false;
      }
    },
  });
  const reset = el("button", {
    class: "btn",
    type: "button",
    text: t("llm.reset"),
    onclick: async () => {
      await api.resetLlmRoutes();
      toast(t("llm.resetDone"));
      await refresh();
    },
  });
  actions.append(probe, reset);

  function paintEditor(container, chainData) {
    clear(container);
    const configured = ["fast", "chat"].flatMap((tier) =>
      (chainData[tier] || [])
        .filter((route) => !route.name.startsWith("local-"))
        .map((route) => ({
          name: route.name,
          provider: route.provider,
          base_url: route.base_url,
          api_key: route.masked_key,
          model: route.model,
          tier: route.tier,
          timeout_sec: route.timeout_sec,
        })),
    );
    editor = el("textarea", {
      class: "route-editor",
      rows: 16,
      spellcheck: "false",
      "aria-label": t("llm.editor"),
    });
    editor.value = JSON.stringify(configured, null, 2);
    const save = el("button", {
      class: "btn primary",
      type: "button",
      text: t("llm.save"),
      onclick: async () => {
        try {
          const routes = JSON.parse(editor.value);
          await api.saveLlmRoutes(routes);
          toast(t("llm.saved"));
          await refresh();
        } catch (error) {
          toast(error.message, "bad");
        }
      },
    });
    container.append(
      el("header", {}, [el("h2", { text: t("llm.editor") })]),
      editor,
      el("div", { class: "actions" }, [save]),
    );
  }

  await refresh();
}

const CHAT_BACKEND_CHOICES = ["auto", "ollama", "claude_subscription", "codex_subscription", "crew_chat"];
const CREW_CHAT_AGENTS = ["", "jarvis", "dev", "research", "assistant", "schule", "scribe", "reach"];

function paintBackendSelectors(container, currentPayload) {
  clear(container);

  const overrideSelect = el(
    "select",
    { class: "input", "aria-label": t("llm.backendOverride") },
    CHAT_BACKEND_CHOICES.map((value) => el("option", { value, text: t(`llm.backend.${value}`) })),
  );
  overrideSelect.value = currentPayload.chat_backend_override || "auto";
  overrideSelect.addEventListener("change", async () => {
    try {
      await api.setChatBackendOverride(overrideSelect.value);
      toast(t("llm.backendSaved"));
      await refreshBackendState(container);
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  const agentSelect = el(
    "select",
    { class: "input", "aria-label": t("llm.crewChatAgent") },
    CREW_CHAT_AGENTS.map((value) =>
      el("option", { value, text: value || t("llm.crewChatAgentUnset") })),
  );
  agentSelect.value = currentPayload.crew_chat_agent || "";
  agentSelect.addEventListener("change", async () => {
    try {
      await api.setCrewChatAgent(agentSelect.value);
      toast(t("llm.backendSaved"));
      await refreshBackendState(container);
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  container.append(
    el("header", {}, [el("h2", { text: t("llm.backendTitle") })]),
    el("p", { class: "aside", text: t("llm.backendLead") }),
    el("div", { class: "field-row" }, [
      el("label", {}, [el("span", { text: t("llm.backendOverride") }), overrideSelect]),
      el("label", {}, [el("span", { text: t("llm.crewChatAgent") }), agentSelect]),
    ]),
  );

  async function refreshBackendState(node) {
    const latest = await api.llmRoutes();
    paintBackendSelectors(node, latest);
  }
}

function paintChains(container, chains) {
  clear(container);
  for (const tier of ["fast", "chat", "private"]) {
    const routes = chains[tier] || [];
    container.append(el("section", { class: "card llm-chain" }, [
      el("header", {}, [
        el("h2", { text: t(`llm.tier.${tier}`) }),
        el("span", { class: "aside", text: `${routes.length}` }),
      ]),
      routeList(routes),
    ]));
  }
}

function routeList(routes) {
  if (!routes.length) return empty(t("common.none"));
  return el("div", { class: "route-list" }, routes.map((route) => {
    const status = route.active
      ? chip(t("llm.active"))
      : route.invalid || route.blocked_until
        ? chip(t("llm.blocked"), "warn")
        : chip(t("llm.ready"));
    const metrics = [
      routeMetric(t("llm.key"), route.masked_key || "—", "route-key"),
      routeMetric(t("llm.hits"), route.hits, "num"),
      routeMetric(t("llm.failures"), route.failures, "num"),
    ];
    if (route.last_error) {
      metrics.push(routeMetric(t("llm.lastError"), route.last_error, "route-error"));
    }

    return el("article", { class: "route-entry" }, [
      el("div", { class: "route-primary" }, [
        status,
        el("div", { class: "route-identity" }, [
          el("strong", { class: "route-name", text: route.name }),
          el("code", { class: "route-model", text: route.model || "—" }),
          el("span", { class: "route-provider", text: route.provider }),
        ]),
      ]),
      el("dl", { class: "route-metrics" }, metrics),
    ]);
  }));
}

function routeMetric(label, value, valueClass) {
  const metricClass = valueClass === "route-error"
    ? "route-metric route-error-metric"
    : "route-metric";
  return el("div", { class: metricClass }, [
    el("dt", { text: label }),
    el("dd", { class: valueClass, text: String(value ?? "—") }),
  ]);
}
