/* Memory: the graph on the left, the node under the cursor on the right.

   The weight bar next to each node is the same decay score the assistant
   ranks by, so what it actually leans on and what has gone stale are
   visible without reading a single node. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { card, chip, clear, el, empty, stat, toast } from "../ui.js";

const HALF_LIFE_DAYS = 14;

/* Mirrors _decay_score_sql in memory/graph.py: reads counted, discounted by
   how long ago they happened. */
function weight(node) {
  const lastAccess = node.last_accessed ? new Date(node.last_accessed).getTime() : Date.now();
  const ageDays = Math.max(0, (Date.now() - lastAccess) / 86400000);
  return (node.access_count || 0) / (1 + ageDays / HALF_LIFE_DAYS);
}

export async function mount(root) {
  const state = { selected: null, presets: new Set(), maxWeight: 1 };

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

  const statsRow = el("div", { class: "grid" });
  const treeCard = el("section", { class: "card" });
  const detailCard = el("section", { class: "card" });
  const diaryCard = el("section", { class: "card" });

  root.append(head, statsRow, el("div", { class: "split" }, [treeCard, detailCard]), diaryCard);

  /* ── Painting ─────────────────────────────────────────────────────── */

  async function refresh() {
    const [tree, stats, presets, memories] = await Promise.all([
      api.graphTree(),
      api.graphStats().catch(() => ({})),
      api.graphPresets().catch(() => ({ ids: [] })),
      api.memories().catch(() => ({ memories: [] })),
    ]);

    state.presets = new Set(presets.ids || []);
    state.maxWeight = Math.max(1, ...collect(tree).map(weight));

    paintStats(stats, memories);
    paintTree(tree);
    paintDiary(memories);
    if (!state.selected) paintEmptyDetail();
  }

  function collect(subtree, into = []) {
    if (!subtree || !subtree.node) return into;
    into.push(subtree.node);
    for (const child of subtree.children || []) collect(child, into);
    return into;
  }

  function paintStats(stats, memories) {
    clear(statsRow);
    statsRow.append(
      card(null, [stat(t("memory.stats.nodes"), fmt.number(stats.total_nodes ?? 0))]),
      card(null, [stat(t("memory.stats.tokens"), fmt.number(stats.total_tokens ?? 0))]),
      card(null, [
        stat(t("memory.stats.entries"), fmt.number((memories.memories || []).length)),
      ]),
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
