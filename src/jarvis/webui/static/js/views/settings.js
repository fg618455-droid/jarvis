/* Settings: the config file, rendered from the same registry the Qt window uses.

   Only what actually changed is sent, so a save never rewrites a value the
   page merely displayed, and a masked credential returned unchanged leaves
   the stored one alone. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { chip, clear, el, toast } from "../ui.js";

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
  const changeCount = el("span", { class: "muted" });
  const bar = el("div", { class: "settings-bar" }, [
    el("span", { class: "muted", text: `${t("settings.file")}: ${payload.path}` }),
    el("span", { class: "spacer" }),
    changeCount,
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

  function paintPanel() {
    clear(panel);
    const fields = payload.fields.filter((field) => field.category === current);
    panel.append(
      el("header", {}, [
        el("h2", { text: used.find((category) => category.key === current)?.label || "" }),
      ]),
    );

    for (const field of fields) {
      const node = control(field);
      panel.append(
        el("div", { class: `field${changes.has(field.key) ? " changed" : ""}` }, [
          el("div", { class: "field-head" }, [
            el("label", { text: field.label }),
            field.restart_required ? chip(t("settings.restart")) : null,
            field.is_secret ? chip(field.is_set ? t("settings.secretSet") : t("settings.secretUnset")) : null,
          ]),
          el("div", { class: "field-help", text: field.description }),
          el("div", { class: "field-control" }, [node]),
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

  paintNav();
  paintPanel();
  markChanged();

  return () => {};
}
