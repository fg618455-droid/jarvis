/* Tools: the catalogue, the servers behind it, and what ran recently. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, table, toast } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("tools.title") }),
    el("p", { text: t("tools.lead") }),
    el("div", { class: "actions" }, [
      el("button", {
        class: "btn",
        type: "button",
        text: t("common.refresh"),
        onclick: async (event) => {
          event.target.disabled = true;
          try {
            const result = await api.refreshTools();
            toast(t("tools.refreshed", { n: result.tool_count }));
            await refresh();
          } catch (error) {
            toast(error.message, "bad");
          } finally {
            event.target.disabled = false;
          }
        },
      }),
    ]),
  ]);

  const serversRow = el("div", { class: "grid" });
  const tableCard = el("section", { class: "card" });
  root.append(head, serversRow, tableCard);

  async function refresh() {
    const payload = await api.tools();
    paintServers(serversRow, payload.servers || []);
    paintTools(tableCard, payload.tools || []);
  }

  await refresh();
  return () => {};
}

function paintServers(container, servers) {
  clear(container);
  if (!servers.length) {
    container.append(
      el("section", { class: "card" }, [
        el("header", {}, [el("h2", { text: t("tools.servers") })]),
        el("span", { class: "muted", text: t("common.none") }),
      ]),
    );
    return;
  }

  for (const server of servers) {
    container.append(
      el("section", { class: "card" }, [
        el("header", {}, [
          el("h2", { text: server.name }),
          el("span", { class: "aside" }, [
            chip(
              server.connected
                ? `${server.tool_count} ${t("tools.connected")}`
                : t("tools.disconnected"),
              server.connected ? "ok" : "warn",
            ),
          ]),
        ]),
        el("div", { class: "rows" }, [
          el("div", { class: "row" }, [
            el("span", { class: "key", text: "command" }),
            el("span", { class: "val num", text: `${server.command} ${(server.args || []).join(" ")}` }),
          ]),
          el("div", { class: "row" }, [
            el("span", { class: "key", text: "timeout" }),
            el("span", {
              class: "val num",
              text: server.timeout_sec ? `${server.timeout_sec} s` : "—",
            }),
          ]),
        ]),
      ]),
    );
  }
}

function paintTools(container, tools) {
  clear(container);
  container.append(
    el("header", {}, [
      el("h2", { text: t("tools.title") }),
      el("span", { class: "aside", text: `${tools.length}` }),
    ]),
  );

  if (!tools.length) {
    container.append(empty(t("common.none")));
    return;
  }

  const ordered = [...tools].sort((a, b) => {
    const usedA = a.last_use?.at || 0;
    const usedB = b.last_use?.at || 0;
    if (usedA !== usedB) return usedB - usedA;
    return a.name.localeCompare(b.name);
  });

  container.append(
    el("div", { class: "scroll" }, [
      table(
        [
          { label: t("common.name"), render: (tool) => tool.name },
          {
            label: t("tools.origin"),
            render: (tool) => chip(tool.server || t("tools.builtin")),
          },
          {
            label: t("common.description"),
            render: (tool) => fmt.truncate(tool.description, 90),
          },
          {
            label: t("tools.confirm"),
            render: (tool) =>
              tool.needs_confirmation === null
                ? "—"
                : tool.needs_confirmation
                  ? t("common.yes")
                  : t("common.no"),
          },
          {
            label: t("tools.lastUse"),
            numeric: true,
            render: (tool) =>
              tool.last_use
                ? el("span", {}, [
                    el("span", { text: fmt.ago(tool.last_use.at, t) }),
                    el("span", { class: "muted", text: ` ${fmt.ms(tool.last_use.duration_ms)}` }),
                  ])
                : t("common.never"),
          },
        ],
        ordered,
      ),
    ]),
  );
}
