/* Memory: the graph on the left, the node under the cursor on the right.

   The weight bar next to each node is the same decay score the assistant
   ranks by, so what it actually leans on and what has gone stale are
   visible without reading a single node. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { card, chip, clear, el, empty, stat, table, toast } from "../ui.js";

const HALF_LIFE_DAYS = 14;

/* Mirrors _decay_score_sql in memory/graph.py: reads counted, discounted by
   how long ago they happened. */
function weight(node) {
  const lastAccess = node.last_accessed ? new Date(node.last_accessed).getTime() : Date.now();
  const ageDays = Math.max(0, (Date.now() - lastAccess) / 86400000);
  return (node.access_count || 0) / (1 + ageDays / HALF_LIFE_DAYS);
}

export async function mount(root) {
  const state = { selected: null, presets: new Set(), maxWeight: 1, maintenanceRunning: false };

  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("memory.title") }),
    el("p", { text: t("memory.lead") }),
    el("div", { class: "actions" }, [
      el("button", {
        class: "btn",
        type: "button",
        text: t("memory.node.new"),
        onclick: () => startNewNode(),
      }),
      el("button", {
        class: "btn",
        type: "button",
        text: t("common.refresh"),
        onclick: () => refresh(),
      }),
    ]),
  ]);

  const statsRow = el("div", { class: "readings" });
  const treeCard = el("section", { class: "card" });
  const detailCard = el("section", { class: "card" });
  const diaryCard = el("section", { class: "card" });
  const mealsCard = el("section", { class: "card" });
  const topicsCard = el("section", { class: "card" });
  const maintenanceCard = el("section", { class: "card maintenance" });

  root.append(
    head,
    statsRow,
    el("div", { class: "split" }, [treeCard, detailCard]),
    maintenanceCard,
    diaryCard,
    el("div", { class: "grid-2" }, [mealsCard, topicsCard]),
  );

  paintMaintenance();

  /* ── Painting ─────────────────────────────────────────────────────── */

  async function refresh() {
    const [tree, stats, presets, memories, meals, topics] = await Promise.all([
      api.graphTree(),
      api.graphStats().catch(() => ({})),
      api.graphPresets().catch(() => ({ ids: [] })),
      api.memories().catch(() => ({ memories: [] })),
      api.meals().catch(() => ({ meals: [] })),
      api.topics().catch(() => ({ topics: [] })),
    ]);

    state.presets = new Set(presets.ids || []);
    state.maxWeight = Math.max(1, ...collect(tree).map(weight));

    paintStats(stats, memories, meals);
    paintTree(tree);
    paintDiary(memories);
    paintMeals(meals);
    paintTopics(topics);
    if (!state.selected) paintEmptyDetail();
  }

  function collect(subtree, into = []) {
    if (!subtree || !subtree.node) return into;
    into.push(subtree.node);
    for (const child of subtree.children || []) collect(child, into);
    return into;
  }

  function paintStats(stats, memories, meals) {
    clear(statsRow);
    statsRow.append(
      card(null, [stat(t("memory.stats.nodes"), fmt.number(stats.total_nodes ?? 0))]),
      card(null, [stat(t("memory.stats.tokens"), fmt.number(stats.total_tokens ?? 0))]),
      card(null, [
        stat(t("memory.stats.entries"), fmt.number((memories.memories || []).length)),
      ]),
      card(null, [stat(t("memory.stats.meals"), fmt.number((meals.meals || []).length))]),
    );
  }

  function paintTree(tree) {
    clear(treeCard);
    treeCard.append(el("header", {}, [el("h2", { text: t("memory.tree") })]));

    if (!tree || !tree.node) {
      treeCard.append(empty(t("memory.empty")));
      return;
    }

    const list = el("div", { class: "tree" });
    walk(tree, 0, list);
    treeCard.append(list);
  }

  function walk(subtree, depth, list) {
    if (!subtree || !subtree.node) return;
    const node = subtree.node;
    const isBranch = depth <= 1;

    const row = el(
      "div",
      {
        class: `tree-node${isBranch ? " branch" : ""}`,
        role: "option",
        "aria-selected": state.selected === node.id ? "true" : "false",
        dataset: { id: node.id },
        onclick: () => select(node.id),
      },
      [
        el("span", {
          class: "tree-label",
          style: `padding-left: ${depth * 12}px`,
          text: node.name,
        }),
        el("span", { class: "tree-weight", title: t("memory.decay") }, [
          el("span", {
            style: `width: ${Math.min(100, (weight(node) / state.maxWeight) * 100).toFixed(1)}%`,
          }),
        ]),
      ],
    );
    list.append(row);

    for (const child of subtree.children || []) walk(child, depth + 1, list);
  }

  function markSelected() {
    for (const row of treeCard.querySelectorAll(".tree-node")) {
      row.setAttribute("aria-selected", row.dataset.id === state.selected ? "true" : "false");
    }
  }

  /* ── Detail ───────────────────────────────────────────────────────── */

  function paintEmptyDetail() {
    clear(detailCard);
    detailCard.append(
      el("header", {}, [el("h2", { text: t("nav.memory") })]),
      empty(t("memory.node.empty")),
    );
  }

  async function select(nodeId) {
    state.selected = nodeId;
    markSelected();
    const payload = await api.graphNode(nodeId);
    paintDetail(payload);
  }

  function paintDetail({ node, ancestors }) {
    clear(detailCard);
    const isPreset = state.presets.has(node.id);

    const name = el("input", { type: "text", value: node.name });
    const description = el("input", { type: "text", value: node.description || "" });
    const data = el("textarea", { spellcheck: "false" });
    data.value = node.data || "";

    detailCard.append(
      el("header", {}, [
        el("h2", { text: node.name }),
        el("span", { class: "aside", text: node.id }),
      ]),
      el("div", { class: "node-editor" }, [
        el("label", { text: t("memory.node.name") }),
        name,
        el("label", { text: t("memory.node.description") }),
        description,
        el("label", { text: t("memory.node.data") }),
        data,
        el("div", { class: "node-meta" }, [
          el("span", {
            text: `${t("memory.node.accesses")}: ${fmt.number(node.access_count || 0)}`,
          }),
          el("span", { text: `${t("memory.node.tokens")}: ${fmt.number(node.data_token_count || 0)}` }),
          el("span", { text: `${t("memory.node.created")}: ${fmt.date(node.created_at)}` }),
          el("span", { text: `${t("memory.node.updated")}: ${fmt.date(node.updated_at)}` }),
          (ancestors || []).length
            ? el("span", {
                text: `${t("memory.node.parent")}: ${ancestors.map((a) => a.name).join(" › ")}`,
              })
            : null,
        ]),
        el("div", { class: "node-actions" }, [
          el("button", {
            class: "btn primary",
            type: "button",
            text: t("common.save"),
            onclick: async () => {
              await api.updateNode(node.id, {
                name: name.value.trim(),
                description: description.value.trim(),
                data: data.value,
              });
              toast(t("memory.saved"));
              await refresh();
              await select(node.id);
            },
          }),
          el("button", {
            class: "btn",
            type: "button",
            text: t("memory.node.new"),
            onclick: () => startNewNode(node.id),
          }),
          el("button", {
            class: "btn danger",
            type: "button",
            text: t("common.delete"),
            disabled: isPreset,
            title: isPreset ? t("memory.node.protected") : null,
            onclick: async () => {
              if (!confirm(t("memory.deleteConfirm"))) return;
              await api.deleteNode(node.id);
              state.selected = null;
              await refresh();
              paintEmptyDetail();
            },
          }),
        ]),
      ]),
    );
  }

  function startNewNode(parentId) {
    clear(detailCard);
    const name = el("input", { type: "text", placeholder: t("memory.node.name") });
    const description = el("input", { type: "text", placeholder: t("memory.node.description") });
    const data = el("textarea", { spellcheck: "false", placeholder: t("memory.node.data") });

    detailCard.append(
      el("header", {}, [el("h2", { text: t("memory.node.new") })]),
      el("div", { class: "node-editor" }, [
        name,
        description,
        data,
        el("div", { class: "node-actions" }, [
          el("button", {
            class: "btn primary",
            type: "button",
            text: t("common.save"),
            onclick: async () => {
              if (!name.value.trim()) return;
              const created = await api.createNode({
                name: name.value.trim(),
                description: description.value.trim(),
                data: data.value,
                parent_id: parentId || state.selected || "user",
              });
              await refresh();
              if (created?.node?.id) await select(created.node.id);
            },
          }),
          el("button", {
            class: "btn",
            type: "button",
            text: t("common.cancel"),
            onclick: () => (state.selected ? select(state.selected) : paintEmptyDetail()),
          }),
        ]),
      ]),
    );
    name.focus();
  }

  /* ── Diary ────────────────────────────────────────────────────────── */

  function paintDiary(memories) {
    clear(diaryCard);
    diaryCard.append(el("header", {}, [el("h2", { text: t("memory.diary") })]));

    const entries = memories.memories || [];
    if (!entries.length) {
      diaryCard.append(empty(t("memory.diaryEmpty")));
      return;
    }

    const list = el("div", { class: "scroll" });
    for (const entry of entries) {
      list.append(
        el("div", { class: "diary-entry" }, [
          el("span", { class: "when", text: fmt.date(entry.date_utc) }),
          el("span", { text: entry.summary || "" }),
          entry.topics
            ? el(
                "div",
                { class: "diary-topics" },
                String(entry.topics)
                  .split(",")
                  .map((topic) => topic.trim())
                  .filter(Boolean)
                  .map((topic) => chip(topic)),
              )
            : null,
        ]),
      );
    }
    diaryCard.append(list);
  }

  function measurement(value, unitKey) {
    return value === null || value === undefined
      ? t("common.unknown")
      : el("span", {}, [
          el("span", { class: "num", text: fmt.number(value) }),
          ` ${t(unitKey)}`,
        ]);
  }

  function paintMeals(payload) {
    clear(mealsCard);
    mealsCard.append(el("header", {}, [el("h2", { text: t("memory.meals") })]));
    const meals = payload.meals || [];
    if (!meals.length) {
      mealsCard.append(empty(t("memory.mealsEmpty")));
      return;
    }
    mealsCard.append(
      el("div", { class: "scroll" }, [
        table(
          [
            { label: t("memory.meals.date"), render: (meal) => fmt.date(meal.ts_utc) },
            { label: t("memory.meals.description"), key: "description" },
            {
              label: t("memory.meals.energy"),
              numeric: true,
              render: (meal) => measurement(meal.calories_kcal, "memory.unit.kcal"),
            },
            {
              label: t("memory.meals.protein"),
              numeric: true,
              render: (meal) => measurement(meal.protein_g, "memory.unit.gram"),
            },
            {
              label: t("memory.meals.carbs"),
              numeric: true,
              render: (meal) => measurement(meal.carbs_g, "memory.unit.gram"),
            },
            {
              label: t("memory.meals.fat"),
              numeric: true,
              render: (meal) => measurement(meal.fat_g, "memory.unit.gram"),
            },
          ],
          meals,
        ),
      ]),
    );
  }

  function paintTopics(payload) {
    clear(topicsCard);
    topicsCard.append(el("header", {}, [el("h2", { text: t("memory.topics") })]));
    const topics = payload.topics || [];
    if (!topics.length) {
      topicsCard.append(empty(t("memory.topicsEmpty")));
      return;
    }
    topicsCard.append(
      el("div", { class: "scroll" }, [
        table(
          [
            { label: t("memory.topics.name"), key: "name" },
            { label: t("memory.topics.entries"), key: "count", numeric: true },
          ],
          topics,
        ),
      ]),
    );
  }

  /* ── Maintenance ──────────────────────────────────────────────────── */

  function paintMaintenance() {
    clear(maintenanceCard);
    maintenanceCard.append(
      el("header", {}, [el("h2", { text: t("memory.maintenance") })]),
      el("p", { class: "muted", text: t("memory.maintenance.lead") }),
    );

    const actions = [
      {
        id: "import-diary",
        label: "memory.maintenance.import",
        description: "memory.maintenance.import.description",
        stream: (onEvent) => api.importDiary(onEvent),
        summary: (event) => t("memory.maintenance.import.complete", {
          facts: fmt.number(event.total_facts || 0),
          rows: fmt.number(event.processed || 0),
        }),
      },
      {
        id: "consolidate-all",
        label: "memory.maintenance.consolidate",
        description: "memory.maintenance.consolidate.description",
        confirm: "memory.maintenance.consolidate.confirm",
        stream: (onEvent) => api.consolidateAll(onEvent),
        summary: (event) => t("memory.maintenance.consolidate.complete", {
          nodes: fmt.number(event.nodes || 0),
          before: fmt.number(event.total_before || 0),
          after: fmt.number(event.total_after || 0),
          delta: fmt.number(event.total_delta || 0),
        }),
      },
      {
        id: "scrub-deflections",
        label: "memory.maintenance.scrub",
        description: "memory.maintenance.scrub.description",
        confirm: "memory.maintenance.scrub.confirm",
        stream: (onEvent) => api.scrubDeflections(onEvent),
        summary: (event) => t("memory.maintenance.scrub.complete", {
          changed: fmt.number(event.rows_rewritten || 0),
          rows: fmt.number(event.rows || 0),
          kept: fmt.number(event.rows_would_empty || 0),
          embeddings: fmt.number(event.embeddings_refreshed || 0),
        }),
      },
      {
        id: "optimise-topics",
        label: "memory.maintenance.optimise",
        description: "memory.maintenance.optimise.description",
        confirm: "memory.maintenance.optimise.confirm",
        stream: (onEvent) => api.optimiseTopics(onEvent),
        summary: (event) => t("memory.maintenance.optimise.complete", {
          changed: fmt.number(event.rows_changed || 0),
          rows: fmt.number(event.rows || 0),
          merged: fmt.number(event.topics_merged || 0),
          expanded: fmt.number(event.topics_expanded || 0),
        }),
      },
    ];

    const controls = [];
    const actionGrid = el("div", { class: "maintenance-grid" });
    for (const action of actions) {
      const count = el("span", { class: "maintenance-count num" });
      const bar = el("span");
      const meter = el("div", { class: "maintenance-meter", hidden: true }, [bar]);
      const summary = el("p", {
        class: "maintenance-summary",
        text: t("memory.maintenance.ready"),
        role: "status",
        "aria-live": "polite",
      });
      const button = el("button", {
        class: "btn",
        type: "button",
        text: t(action.label),
      });
      const panel = el("article", { class: "maintenance-action", dataset: { action: action.id } }, [
        el("h3", { text: t(action.label) }),
        el("p", { text: t(action.description) }),
        el("div", { class: "maintenance-controls" }, [button, count]),
        meter,
        summary,
      ]);
      const control = { action, bar, button, count, meter, panel, summary };
      controls.push(control);
      button.addEventListener("click", () => runMaintenance(control, controls));
      actionGrid.append(panel);
    }
    maintenanceCard.append(actionGrid);
  }

  async function runMaintenance(control, controls) {
    if (state.maintenanceRunning) return;
    if (control.action.confirm && !confirm(t(control.action.confirm))) return;

    state.maintenanceRunning = true;
    let total = 0;
    let processed = 0;
    for (const item of controls) item.button.disabled = true;
    control.panel.classList.add("running");
    control.meter.hidden = false;
    control.bar.style.width = "0%";
    control.summary.textContent = t("memory.maintenance.starting");

    try {
      await control.action.stream((event) => {
        if (event.type === "start") {
          total = Number(event.total) || 0;
          processed = 0;
        } else if (event.type === "progress") {
          processed = event.processed === undefined ? processed + 1 : Number(event.processed) || 0;
          total = Number(event.total) || total;
        }

        if (event.type === "start" || event.type === "progress") {
          control.count.textContent = t("memory.maintenance.count", {
            processed: fmt.number(processed),
            total: fmt.number(total),
          });
          const fraction = total ? Math.min(1, processed / total) : 0;
          control.bar.style.width = `${(fraction * 100).toFixed(1)}%`;
          control.summary.textContent = t("memory.maintenance.running");
        } else if (event.type === "complete") {
          control.bar.style.width = "100%";
          control.summary.textContent = control.action.summary(event);
        } else if (event.type === "error") {
          control.summary.textContent = t("memory.maintenance.failed", {
            message: event.message || t("common.unknown"),
          });
        }
      });
      await refresh();
    } catch (error) {
      control.summary.textContent = t("memory.maintenance.failed", {
        message: error.message || t("common.unknown"),
      });
    } finally {
      state.maintenanceRunning = false;
      control.panel.classList.remove("running");
      for (const item of controls) item.button.disabled = false;
    }
  }

  await refresh();

  /* A node written while this page is open flashes once, so learning is
     something you can watch rather than something you find later. */
  const off = live.on("turn", async () => {
    const before = new Set(
      [...treeCard.querySelectorAll(".tree-node")].map((row) => row.dataset.id),
    );
    await refresh();
    for (const row of treeCard.querySelectorAll(".tree-node")) {
      if (!before.has(row.dataset.id)) row.classList.add("learned");
    }
  });

  return () => off();
}
