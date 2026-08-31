/* MCP servers: connecting one, and seeing whether it answered.

   An MCP server is a command line, an environment, and a name. All three are
   edited here as named controls rather than as raw JSON, the same way the
   LLM route editor works and for the same reason: a text area holding a
   configuration object is a way of asking someone to get the commas right.

   Two facts sit side by side on every card and are never merged. What is
   *configured* is what this page writes; what is *connected* is what the
   running daemon actually managed to launch and ask. A server saved a moment
   ago is configured and not connected, and saying so is the whole point:
   the alternative is a page that reports success for a command that does not
   exist on this machine. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, toast } from "../ui.js";

export async function mount(root) {
  let payload = await api.mcpServers();
  /* The edited copy. Everything is edited in the page and written in one
     go, because a server is only coherent once its command, its arguments,
     and its environment agree; saving each field as it is typed would write
     a broken launch line on the way to a working one. */
  let servers = clone(payload.servers);
  let dirty = false;

  const list = el("div", { class: "mcp-list" });

  const saveButton = el("button", {
    class: "btn primary",
    type: "button",
    text: t("common.save"),
    disabled: true,
    onclick: () => save(),
  });

  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("mcp.title") }),
    el("p", { text: t("mcp.lead") }),
  ]);

  const bar = el("div", { class: "mcp-bar" }, [
    el("button", {
      class: "btn",
      type: "button",
      text: t("mcp.add"),
      onclick: () => {
        servers.push({
          name: "", command: "", args: [], env: {},
          timeout_sec: null, idle_timeout_sec: null,
          tool_count: 0, connected: false,
        });
        touch();
        paint();
      },
    }),
    saveButton,
  ]);

  root.append(head, bar, list);
  paint();

  function touch() {
    dirty = true;
    saveButton.disabled = false;
  }

  async function save() {
    saveButton.disabled = true;
    try {
      await api.saveMcpServers(servers.map(forWire));
      payload = await api.mcpServers();
      servers = clone(payload.servers);
      dirty = false;
      // Tools are discovered when the daemon starts, so a server added now
      // is on disk now and reachable after a restart. Saying "saved" alone
      // would read as "connected".
      toast(t("mcp.savedRestart"));
      paint();
    } catch (error) {
      toast(error.message, "bad");
      saveButton.disabled = false;
    }
  }

  function paint() {
    clear(list);
    saveButton.disabled = !dirty;

    if (!servers.length) {
      list.append(empty(t("mcp.none")));
      return;
    }
    for (const [index, server] of servers.entries()) {
      list.append(card(server, index));
    }
  }

  function card(server, index) {
    const body = el("div", { class: "mcp-fields" }, [
      field(t("mcp.name"), input(server.name, (value) => { server.name = value; touch(); })),
      field(t("mcp.command"), input(server.command, (value) => {
        server.command = value;
        touch();
      }, "npx")),
      field(
        t("mcp.args"),
        area((server.args || []).join("\n"), (value) => {
          server.args = value.split("\n").map((line) => line.trim()).filter(Boolean);
          touch();
        }, t("mcp.argsPlaceholder")),
      ),
      field(t("mcp.timeout"), number(server.timeout_sec, (value) => {
        server.timeout_sec = value;
        touch();
      })),
      field(t("mcp.idleTimeout"), number(server.idle_timeout_sec, (value) => {
        server.idle_timeout_sec = value;
        touch();
      }), t("mcp.idleTimeoutNote")),
    ]);

    return el("section", { class: "card mcp-server" }, [
      el("header", {}, [
        el("h2", { text: server.name || t("mcp.unnamed") }),
        el("span", { class: "aside" }, [
          chip(
            server.connected
              ? t("mcp.connectedCount", { n: server.tool_count })
              : payload.discovered ? t("mcp.notConnected") : t("mcp.notDiscovered"),
            server.connected ? "ok" : "warn",
          ),
        ]),
      ]),
      body,
      environment(server),
      el("div", { class: "mcp-actions" }, [
        el("button", {
          class: "btn danger",
          type: "button",
          text: t("mcp.remove"),
          "aria-label": t("mcp.removeNamed", { name: server.name || t("mcp.unnamed") }),
          onclick: () => {
            servers.splice(index, 1);
            touch();
            paint();
          },
        }),
      ]),
    ]);
  }

  /* The environment is where a server's credentials live, so it is edited as
     a key and a value rather than as a block of `KEY=value` text: a masked
     value has to survive being displayed and saved untouched, and a text
     block would make the mask indistinguishable from someone typing eight
     bullets. */
  function environment(server) {
    const rows = el("div", { class: "mcp-env" });
    const entries = Object.entries(server.env || {});

    for (const [key, value] of entries) {
      rows.append(
        el("div", { class: "mcp-env-row" }, [
          input(key, (next) => {
            const current = server.env[key];
            delete server.env[key];
            server.env[next] = current;
            touch();
          }, t("mcp.envKey")),
          input(value, (next) => {
            server.env[key] = next;
            touch();
          }, t("mcp.envValue")),
          el("button", {
            class: "btn danger",
            type: "button",
            text: "×",
            "aria-label": t("mcp.removeEnv", { name: key }),
            onclick: () => {
              delete server.env[key];
              touch();
              paint();
            },
          }),
        ]),
      );
    }

    return el("div", { class: "mcp-env-block" }, [
      el("span", { class: "mcp-label", text: t("mcp.env") }),
      rows,
      el("button", {
        class: "btn",
        type: "button",
        text: t("mcp.addEnv"),
        onclick: () => {
          server.env = { ...(server.env || {}), "": "" };
          touch();
          paint();
        },
      }),
    ]);
  }

  return () => {};
}

/* ── Small controls ──────────────────────────────────────────────────── */

function field(label, control, note) {
  return el("label", { class: "mcp-field" }, [
    el("span", { class: "mcp-label", text: label }),
    control,
    note && el("span", { class: "mcp-note", text: note }),
  ]);
}

function input(value, onChange, placeholder) {
  return el("input", {
    type: "text",
    value: value ?? "",
    placeholder: placeholder || "",
    oninput: (event) => onChange(event.target.value),
  });
}

function area(value, onChange, placeholder) {
  return el("textarea", {
    placeholder: placeholder || "",
    oninput: (event) => onChange(event.target.value),
  }, [value ?? ""]);
}

function number(value, onChange) {
  return el("input", {
    type: "number",
    step: "0.5",
    min: "0",
    value: value ?? "",
    oninput: (event) => {
      const raw = event.target.value;
      onChange(raw === "" ? null : Number(raw));
    },
  });
}

function clone(servers) {
  return (servers || []).map((server) => ({
    ...server,
    args: [...(server.args || [])],
    env: { ...(server.env || {}) },
  }));
}

/* `_index` is the handle the endpoint uses to find the stored entry a
   submitted server came from, so an unchanged masked credential survives a
   rename. A server added in the page has no stored entry and therefore no
   index. */
function forWire(server) {
  return {
    _index: server._index,
    name: server.name,
    command: server.command,
    args: server.args || [],
    env: server.env || {},
    timeout_sec: server.timeout_sec ?? null,
    idle_timeout_sec: server.idle_timeout_sec ?? null,
  };
}
