/* LLM routes: schema-driven configuration beside effective runtime chains. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, toast } from "../ui.js";
import { holdingUnsaved } from "../unsaved.js";

const CHAT_BACKEND_CHOICES = [
  "auto", "ollama", "claude_subscription", "codex_subscription", "crew_chat",
];
const CREW_CHAT_AGENTS = [
  "", "jarvis", "dev", "research", "assistant", "schule", "scribe", "reach",
];
const CHAT_ONLY_PROVIDERS = new Set([
  "claude_subscription", "codex_subscription", "crew_chat",
]);

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("llm.title") }),
    el("p", { text: t("llm.lead") }),
  ]);
  const actions = el("div", { class: "actions" });
  const backendCard = el("section", { class: "card" });
  const chains = el("div", { class: "grid" });
  const editorCard = el("section", { class: "card route-config-card" });
  root.append(head, actions, backendCard, chains, editorCard);

  let payload = null;
  /* The editor holds a copy of the routes and writes them in one go, so
     until Save is pressed a change lives in the page and nowhere else. A
     reload replaces that copy with what is stored, which is what refresh
     does, so it is also what clears this. */
  let edited = false;

  async function refresh() {
    payload = await api.llmRoutes();
    edited = false;
    paintBackendSelectors(backendCard, payload, refresh, mayDiscard);
    paintChains(chains, payload.effective_chains || payload.chains || {});
    paintEditor(editorCard, payload, refresh, () => { edited = true; });
  }

  /* Reloading replaces the editor's copy with what is stored, so anything
     that reloads throws away what is typed into it. Every control that does
     asks first, for the same reason leaving the view does. */
  function mayDiscard() {
    return !edited || window.confirm(t("unsaved.discardConfirm"));
  }

  const probe = el("button", {
    class: "btn", type: "button", text: t("llm.probe"),
    onclick: async () => {
      if (!mayDiscard()) return;
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
    class: "btn", type: "button", text: t("llm.reset"),
    onclick: async () => {
      if (!mayDiscard()) return;
      await api.resetLlmRoutes();
      toast(t("llm.resetDone"));
      await refresh();
    },
  });
  actions.append(probe, reset);

  await refresh();

  return holdingUnsaved(() => edited);
}

function clone(value) {
  return JSON.parse(JSON.stringify(value));
}

function initialRoute(fields) {
  const route = { _index: null, base_url_redacted: false };
  for (const field of fields) {
    route[field.key] = field.default !== undefined && field.default !== null
      ? clone(field.default)
      : field.type === "bool" ? false
        : field.type === "list" ? [] : "";
  }
  return route;
}

function paintEditor(container, currentPayload, refresh, touch) {
  clear(container);
  const fields = currentPayload.route_fields || [];
  const placeholders = currentPayload.provider_placeholders || {};
  const routes = clone(currentPayload.configured_routes || []);
  const list = el("div", { class: "route-config-list" });

  function repaint() {
    clear(list);
    routes.forEach((route, index) => {
      const title = route.name || t("llm.routeNumber", { n: index + 1 });
      const card = el("article", { class: "route-config" });
      const titleNode = el("strong", { text: title });
      const controls = el("div", { class: "route-config-grid" });

      for (const field of fields) {
        const control = routeControl(
          field, route, placeholders,
          (value) => {
            route[field.key] = value;
            touch();
            if (field.key === "name") titleNode.textContent = value || title;
            if (field.key === "provider" && CHAT_ONLY_PROVIDERS.has(value)) {
              route.tier = "chat";
              repaint();
            }
          },
        );
        controls.append(el("label", { class: `route-field route-field-${field.key}` }, [
          field.type === "bool" ? null : el("span", { text: field.label }),
          control,
          field.description
            ? el("small", { class: "field-help", text: field.description }) : null,
        ]));
      }

      card.append(
        el("header", {}, [
          el("div", { class: "route-config-title" }, [
            titleNode,
            chip(route.provider || t("common.unknown")),
          ]),
          el("div", { class: "object-actions" }, [
            el("button", {
              class: "btn icon-btn", type: "button", text: "↑",
              title: t("settings.moveUp"),
              "aria-label": `${t("settings.moveUp")}: ${title}`,
              disabled: index === 0,
              onclick: () => {
                [routes[index - 1], routes[index]] = [routes[index], routes[index - 1]];
                touch();
                repaint();
              },
            }),
            el("button", {
              class: "btn icon-btn", type: "button", text: "↓",
              title: t("settings.moveDown"),
              "aria-label": `${t("settings.moveDown")}: ${title}`,
              disabled: index === routes.length - 1,
              onclick: () => {
                [routes[index + 1], routes[index]] = [routes[index], routes[index + 1]];
                touch();
                repaint();
              },
            }),
            el("button", {
              class: "btn icon-btn", type: "button", text: "×",
              title: t("common.remove"),
              "aria-label": `${t("common.remove")}: ${title}`,
              onclick: () => { routes.splice(index, 1); touch(); repaint(); },
            }),
          ]),
        ]),
        controls,
      );
      list.append(card);
    });
    if (!routes.length) list.append(empty(t("llm.noConfiguredRoutes")));
  }

  const add = el("button", {
    class: "btn", type: "button", text: t("llm.addRoute"),
    onclick: () => { routes.push(initialRoute(fields)); touch(); repaint(); },
  });
  const save = el("button", {
    class: "btn primary", type: "button", text: t("llm.save"),
    onclick: async () => {
      save.disabled = true;
      try {
        await api.saveLlmRoutes(routes);
        toast(t("llm.saved"));
        await refresh();
      } catch (error) {
        toast(error.message, "bad");
        save.disabled = false;
      }
    },
  });

  container.append(
    el("header", {}, [el("h2", { text: t("llm.editor") })]),
    el("p", { class: "aside", text: t("llm.editorLead") }),
    list,
    el("div", { class: "actions" }, [add, save]),
  );
  repaint();
}

function routeControl(field, route, placeholders, changed) {
  const value = route[field.key];
  if (field.type === "bool") {
    const input = el("input", {
      type: "checkbox", checked: Boolean(value), "aria-label": field.label,
    });
    input.addEventListener("change", () => changed(input.checked));
    return el("span", { class: "check compact" }, [input, field.label]);
  }

  if (field.type === "choice") {
    const choices = [...(field.choices || [])];
    if (value && !choices.some((choice) => String(choice.value) === String(value))) {
      choices.unshift({ value, label: String(value) });
    }
    const select = el("select", { "aria-label": field.label }, choices.map((choice) =>
      el("option", {
        value: choice.value, text: choice.label,
        selected: String(choice.value) === String(value ?? ""),
      })));
    if (field.key === "tier" && CHAT_ONLY_PROVIDERS.has(route.provider)) {
      select.disabled = true;
      select.title = t("llm.chatOnly");
    }
    select.addEventListener("change", () => changed(select.value));
    return select;
  }

  if (field.type === "list") {
    const options = field.choices || [
      { value: "chat", label: "chat" },
      { value: "stream", label: "stream" },
      { value: "tools", label: "tools" },
    ];
    const selected = new Set(Array.isArray(value) ? value : []);
    return el("div", { class: "route-capabilities", role: "group", "aria-label": field.label },
      options.map((option) => {
        const input = el("input", {
          type: "checkbox", checked: selected.has(option.value),
          "aria-label": option.label,
        });
        input.addEventListener("change", () => {
          if (input.checked) selected.add(option.value);
          else selected.delete(option.value);
          changed(options.map((item) => item.value).filter((item) => selected.has(item)));
        });
        return el("label", { class: "check compact" }, [input, option.label]);
      }),
    );
  }

  const numeric = field.type === "int" || field.type === "float";
  const hint = placeholders[route.provider]?.[field.key] || "";
  const input = el("input", {
    type: numeric ? "number" : field.is_secret ? "password" : "text",
    value: value ?? "", min: numeric ? field.min : null,
    max: numeric ? field.max : null,
    step: numeric ? (field.step ?? "any") : null,
    placeholder: hint, "aria-label": field.label,
    autocomplete: field.is_secret ? "new-password" : null,
  });
  input.addEventListener("input", () => changed(
    numeric ? (input.value === "" ? null : Number(input.value)) : input.value,
  ));
  return input;
}

function paintBackendSelectors(container, currentPayload, refresh, mayDiscard) {
  clear(container);
  const overrideSelect = el(
    "select", { class: "input", "aria-label": t("llm.backendOverride") },
    CHAT_BACKEND_CHOICES.map((value) =>
      el("option", { value, text: t(`llm.backend.${value}`) })),
  );
  overrideSelect.value = currentPayload.chat_backend_override || "auto";
  overrideSelect.addEventListener("change", async () => {
    // Saving this reloads the whole view, editor included.
    if (!mayDiscard()) {
      overrideSelect.value = currentPayload.chat_backend_override || "auto";
      return;
    }
    try {
      await api.setChatBackendOverride(overrideSelect.value);
      toast(t("llm.backendSaved"));
      await refresh();
    } catch (error) {
      toast(error.message, "bad");
    }
  });

  const agentSelect = el(
    "select", { class: "input", "aria-label": t("llm.crewChatAgent") },
    CREW_CHAT_AGENTS.map((value) =>
      el("option", { value, text: value || t("llm.crewChatAgentUnset") })),
  );
  agentSelect.value = currentPayload.crew_chat_agent || "";
  agentSelect.addEventListener("change", async () => {
    if (!mayDiscard()) {
      agentSelect.value = currentPayload.crew_chat_agent || "";
      return;
    }
    try {
      await api.setCrewChatAgent(agentSelect.value);
      toast(t("llm.backendSaved"));
      await refresh();
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
        ? chip(t("llm.blocked"), "warn") : chip(t("llm.ready"));
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
        chip(t(route.local ? "llm.local" : "llm.remote")),
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
    ? "route-metric route-error-metric" : "route-metric";
  return el("div", { class: metricClass }, [
    el("dt", { text: label }),
    el("dd", { class: valueClass, text: String(value ?? "—") }),
  ]);
}
