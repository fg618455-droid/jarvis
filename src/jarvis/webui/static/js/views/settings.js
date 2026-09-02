/* Settings: the config file, rendered from the same registry the Qt window uses.

   Only what actually changed is sent, so a save never rewrites a value the
   page merely displayed, and a masked credential returned unchanged leaves
   the stored one alone. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { chip, clear, el, toast } from "../ui.js";
import { holdingUnsaved } from "../unsaved.js";

export async function mount(root) {
  const payload = await api.settings();
  const changes = new Map();

  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("settings.title") }),
    el("p", { text: t("settings.lead") }),
  ]);

  const nav = el("div", { class: "settings-nav" });
  const panel = el("section", { class: "card" });
  const layout = el("div", { class: "settings-layout" }, [nav, panel]);

  const saveButton = el("button", {
    class: "btn primary",
    type: "button",
    text: t("common.save"),
    disabled: true,
    onclick: () => save(),
  });
  const restartButton = el("button", {
    class: "btn",
    type: "button",
    text: t("settings.restartNow"),
    disabled: payload.daemon_running === false,
    title: payload.daemon_running === false ? t("settings.noDaemon") : "",
    onclick: () => restart(),
  });
  const changeCount = el("span", { class: "muted" });
  const bar = el("div", { class: "settings-bar" }, [
    el("span", { class: "muted", text: `${t("settings.file")}: ${payload.path}` }),
    el("span", { class: "spacer" }),
    changeCount,
    restartButton,
    saveButton,
  ]);

  root.append(head, layout, bar);

  const used = payload.categories.filter((category) =>
    payload.fields.some((field) => field.category === category.key),
  );
  let current = used[0]?.key;

  function paintNav() {
    clear(nav);
    for (const category of used) {
      nav.append(
        el("button", {
          type: "button",
          text: category.label,
          "aria-current": category.key === current ? "true" : "false",
          onclick: () => {
            current = category.key;
            paintNav();
            paintPanel();
          },
        }),
      );
    }
  }

  function markChanged() {
    saveButton.disabled = changes.size === 0;
    changeCount.textContent = changes.size ? `${changes.size}` : "";
  }

  function record(field, value, control) {
    const unchanged = JSON.stringify(value) === JSON.stringify(field.value);
    if (unchanged) changes.delete(field.key);
    else changes.set(field.key, value);
    control.closest(".field")?.classList.toggle("changed", changes.has(field.key));
    markChanged();
  }

  function control(field) {
    if (field.type === "bool") {
      const box = el("input", { type: "checkbox", checked: Boolean(field.value) });
      box.addEventListener("change", () => {
        if (
          field.key === "passive_capture_enabled" &&
          box.checked &&
          !field.value
        ) {
          const provider = payload.fields.find((item) => item.key === "llm_provider")?.value || "";
          if (!window.confirm(t("passive.enableConfirm", { provider }))) {
            box.checked = false;
            return;
          }
        }
        record(field, box.checked, box);
      });
      return el("label", { class: "check" }, [box, field.label]);
    }

    if (field.type === "choice") {
      const select = el("select");
      for (const choice of field.choices || []) {
        select.append(
          el("option", {
            value: choice.value,
            text: choice.label,
            selected: String(choice.value) === String(field.value ?? ""),
          }),
        );
      }
      select.addEventListener("change", () => record(field, select.value, select));
      return select;
    }

    if (field.type === "list") {
      const area = el("textarea", { spellcheck: "false" });
      area.value = (field.value || []).join("\n");
      area.addEventListener("input", () =>
        record(
          field,
          area.value.split("\n").map((line) => line.trim()).filter(Boolean),
          area,
        ),
      );
      return area;
    }

    if (field.type === "object_list") {
      return objectListControl(field);
    }

    if (field.type === "int" || field.type === "float") {
      const input = el("input", {
        type: "number",
        value: field.value ?? "",
        min: field.min ?? null,
        max: field.max ?? null,
        step: field.step ?? (field.type === "int" ? 1 : "any"),
      });
      input.addEventListener("input", () =>
        record(field, input.value === "" ? null : Number(input.value), input),
      );
      return input;
    }

    const input = el("input", {
      type: field.is_secret ? "password" : "text",
      value: field.is_secret ? "" : (field.value ?? ""),
      placeholder: field.is_secret
        ? field.is_set
          ? field.value
          : t("settings.secretUnset")
        : field.nullable
          ? t("settings.default")
          : "",
    });
    input.addEventListener("input", () => record(field, input.value, input));
    return input;
  }

  function objectListControl(field) {
    const container = el("div", { class: "object-list" });
    const items = JSON.parse(JSON.stringify(field.value || []));

    function nestedControl(meta, value, changed) {
      if (meta.type === "bool") {
        const input = el("input", { type: "checkbox", checked: Boolean(value) });
        input.addEventListener("change", () => changed(input.checked));
        return el("label", { class: "check compact" }, [input, meta.label]);
      }
      if (meta.type === "choice") {
        const select = el("select");
        const choices = [...(meta.choices || [])];
        if (value !== undefined && value !== null && value !== ""
            && !choices.some((choice) => String(choice.value) === String(value))) {
          choices.unshift({ value, label: String(value) });
        }
        for (const choice of choices) {
          select.append(el("option", {
            value: choice.value,
            text: choice.label,
            selected: String(choice.value) === String(value ?? ""),
          }));
        }
        select.addEventListener("change", () => changed(select.value));
        return select;
      }
      if (meta.type === "list") {
        const input = el("input", { type: "text", value: (value || []).join(", ") });
        input.addEventListener("input", () => changed(
          input.value.split(",").map((part) => part.trim()).filter(Boolean),
        ));
        return input;
      }
      const numeric = meta.type === "int" || meta.type === "float";
      const input = el("input", {
        type: numeric ? "number" : (meta.is_secret ? "password" : "text"),
        value: value ?? "",
        min: numeric ? meta.min : null,
        max: numeric ? meta.max : null,
        step: numeric ? (meta.step ?? (meta.type === "int" ? 1 : "any")) : null,
      });
      input.addEventListener("input", () => changed(
        numeric ? (input.value === "" ? null : Number(input.value)) : input.value,
      ));
      return input;
    }

    function commit() {
      record(field, JSON.parse(JSON.stringify(items)), container);
    }

    function paint() {
      clear(container);
      const list = el("div", { class: "object-list-items" });
      items.forEach((item, index) => {
        const title = String(item.name || t("settings.item", { n: index + 1 }));
        const card = el("section", { class: "object-item" }, [
          el("header", {}, [
            el("strong", { text: title }),
            el("div", { class: "object-actions" }, [
              el("button", {
                class: "btn icon-btn", type: "button", text: "↑",
                title: t("settings.moveUp"), "aria-label": `${t("settings.moveUp")}: ${title}`,
                disabled: index === 0,
                onclick: () => {
                  [items[index - 1], items[index]] = [items[index], items[index - 1]];
                  commit(); paint();
                },
              }),
              el("button", {
                class: "btn icon-btn", type: "button", text: "↓",
                title: t("settings.moveDown"), "aria-label": `${t("settings.moveDown")}: ${title}`,
                disabled: index === items.length - 1,
                onclick: () => {
                  [items[index + 1], items[index]] = [items[index], items[index + 1]];
                  commit(); paint();
                },
              }),
              el("button", {
                class: "btn icon-btn", type: "button", text: "×",
                title: t("common.remove"), "aria-label": `${t("common.remove")}: ${title}`,
                onclick: () => { items.splice(index, 1); commit(); paint(); },
              }),
            ]),
          ]),
        ]);
        const grid = el("div", { class: "object-grid" });
        for (const meta of field.item_fields || []) {
          const editor = nestedControl(meta, item[meta.key], (value) => {
            item[meta.key] = value;
            commit();
            if (meta.key === "name") card.querySelector("strong").textContent = value || title;
          });
          grid.append(el("label", { class: "object-field" }, [
            meta.type === "bool" ? null : el("span", { text: meta.label }),
            editor,
          ]));
        }
        card.append(grid);
        list.append(card);
      });
      container.append(
        list,
        el("button", {
          class: "btn", type: "button", text: t("settings.addItem"),
          onclick: () => {
            const item = {};
            for (const meta of field.item_fields || []) {
              item[meta.key] = meta.default !== undefined && meta.default !== null
                ? JSON.parse(JSON.stringify(meta.default))
                : meta.type === "bool" ? false
                  : meta.type === "list" ? [] : "";
            }
            items.push(item);
            commit(); paint();
          },
        }),
      );
    }

    paint();
    return container;
  }

  function paintPanel() {
    clear(panel);
    const fields = payload.fields.filter((field) => field.category === current);
    const category = used.find((item) => item.key === current);
    panel.append(
      el("header", {}, [
        el("h2", { text: category?.label || "" }),
      ]),
    );
    if (category?.description) {
      panel.append(el("div", { class: "settings-note" }, [
        el("p", { text: category.description }),
        category.action_href
          ? el("a", { class: "btn", href: category.action_href, text: category.action_label })
          : null,
      ]));
    }

    let activeSection = null;
    for (const field of fields) {
      if (field.section && field.section !== activeSection) {
        activeSection = field.section;
        panel.append(el("h3", { class: "settings-section", text: field.section }));
      }
      const node = control(field);
      // A label that only sits near a control is not a label on it: to
      // anything reading the accessibility tree that is an unnamed edit box
      // beside some unrelated text, and the field holding the Ollama URL is
      // announced exactly like the one holding a timeout. A switch already
      // wraps its own label, and an object list is many controls rather than
      // one, so both are left as they are.
      const single = ["INPUT", "SELECT", "TEXTAREA"].includes(node.tagName);
      const controlId = `setting-${field.key}`;
      if (single) node.id = controlId;
      panel.append(
        el("div", { class: `field${changes.has(field.key) ? " changed" : ""}` }, [
          el("div", { class: "field-head" }, [
            el("label", { text: field.label, for: single ? controlId : null }),
            field.restart_required ? chip(t("settings.restart")) : null,
            field.is_secret ? chip(field.is_set ? t("settings.secretSet") : t("settings.secretUnset")) : null,
          ]),
          el("div", { class: "field-help", text: field.description }),
          el("div", {
            class: `field-control${field.type === "object_list" ? " wide" : ""}`,
          }, [node]),
        ]),
      );
    }
  }

  async function save() {
    if (!changes.size) return toast(t("settings.nothing"));
    saveButton.disabled = true;
    try {
      const result = await api.saveSettings(Object.fromEntries(changes));
      toast(
        result.restart_required.length
          ? t("settings.saved", { n: result.restart_required.length })
          : t("settings.savedClean"),
      );
      // Re-read so the page shows what the file now holds, masks included.
      const fresh = await api.settings();
      payload.fields = fresh.fields;
      changes.clear();
      markChanged();
      paintPanel();
    } catch (error) {
      toast(error.message, "bad");
      saveButton.disabled = false;
    }
  }

  async function restart() {
    if (payload.daemon_running === false) return;
    if (!window.confirm(t("settings.restartConfirm"))) return;
    restartButton.disabled = true;
    saveButton.disabled = true;
    toast(t("settings.restarting"));
    try {
      await api.restart();
    } catch {
      // The connection can drop before the response arrives once the
      // generation actually tears down; that is the expected shape of
      // a restart, not a failure worth surfacing.
    }
    await waitForRestart();
  }

  async function waitForRestart() {
    // Give the daemon a moment to actually start tearing down before
    // polling, so the first health check does not just hit the
    // still-running old generation and report "back" immediately.
    await new Promise((resolve) => setTimeout(resolve, 1500));
    const deadline = Date.now() + 60_000;
    while (Date.now() < deadline) {
      try {
        await api.health();
        toast(t("settings.restartBack"));
        location.reload();
        return;
      } catch {
        await new Promise((resolve) => setTimeout(resolve, 1000));
      }
    }
    toast(t("settings.restartFailed"), "bad");
    restartButton.disabled = false;
    saveButton.disabled = changes.size === 0;
  }

  paintNav();
  paintPanel();
  markChanged();

  // A field typed back to what it already was is not a change: `changes`
  // holds only what differs from the stored value, so the shell stays quiet
  // for an edit that was undone.
  return holdingUnsaved(() => changes.size > 0);
}
