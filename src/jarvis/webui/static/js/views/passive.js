/* Passive record: everything heard near the microphone, written down.

   This is a privacy surface before it is a reading. The switch that starts
   it, the account of what it has kept, and the three ways to delete that
   account all belong together, and none of them belongs in the middle of
   the conversation.

   What it shows is text and only text. No audio is written anywhere, and a
   line that was addressed to Jarvis is marked rather than hidden, because
   the record is meant to read as an account of the room rather than of the
   half of it the assistant ignored. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { clear, el, empty, toast } from "../ui.js";

export async function mount(root) {
  const pill = el("span", { class: "state-pill" });
  const toggle = el("button", { class: "btn", type: "button" });
  const card = el("section", { class: "card" });

  root.append(
    el("div", { class: "view-head" }, [
      el("h1", { text: t("passive.title") }),
      el("p", { text: t("passive.lead") }),
      el("div", { class: "actions" }, [pill, toggle]),
    ]),
    card,
  );

  let payload = { lines: [], enabled: false, undigested_count: 0 };

  async function refresh() {
    payload = await api.passive("", 500);
    paintState(pill, payload);
    paintToggle(toggle, payload, refresh);
    paintRecord(card, payload, refresh);
  }

  await refresh();

  // The switch can also be flipped from Settings, and a line lands without
  // anyone clicking, so the view follows the runtime rather than its own
  // last action.
  const off = live.on("passive", () => refresh().catch(() => {}));
  return () => off();
}

/* The switch state, beside how much is still waiting to be read by the
   digest. Whether the record is being written is the fact people come here
   for, so it is said in the frame rather than inside a card. */
function paintState(pill, payload) {
  clear(pill);
  pill.className = `state-pill${payload.enabled ? " recording" : ""}`;
  pill.append(
    el("span", { class: "state-pill-dot" }),
    el("span", { text: payload.enabled ? t("passive.on") : t("passive.off") }),
    el("span", { class: "state-pill-note" }, [
      el("span", { text: t("passive.waiting", { n: payload.undigested_count || 0 }) }),
    ]),
  );
}

function paintToggle(toggle, payload, refresh) {
  const enabled = Boolean(payload.enabled);
  clear(toggle);
  toggle.className = enabled ? "btn" : "btn primary";
  toggle.textContent = enabled ? t("passive.turnOff") : t("passive.turnOn");
  toggle.onclick = async () => {
    // Turning it on is asked for; turning it off never is. Permission is
    // owed for starting to write the room down, not for stopping.
    if (!enabled && !window.confirm(t("passive.enableConfirm", {
      provider: payload.llm_provider || "",
    }))) return;

    toggle.disabled = true;
    try {
      await api.setPassiveEnabled(!enabled);
      await refresh();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      toggle.disabled = false;
    }
  };
}

function paintRecord(container, payload, refresh) {
  clear(container);
  const lines = payload.lines || [];
  const notice = t("passive.deleteNotice");

  container.append(
    el("header", {}, [
      el("h2", { text: t("passive.kept") }),
      el("span", { class: "aside num", text: t("passive.lines", { n: lines.length }) }),
      el("button", {
        class: "btn danger",
        type: "button",
        text: t("passive.deleteAll"),
        disabled: !lines.length,
        onclick: () => remove(
          `${t("passive.confirmAll")}\n\n${notice}`,
          () => api.deletePassiveAll(),
          refresh,
        ),
      }),
    ]),
    el("p", { class: "passive-notice", text: notice }),
  );

  if (!lines.length) {
    container.append(empty(payload.enabled ? t("passive.empty") : t("passive.notRunning")));
    return;
  }

  const byDay = new Map();
  for (const line of lines) {
    if (!byDay.has(line.date_utc)) byDay.set(line.date_utc, []);
    byDay.get(line.date_utc).push(line);
  }

  const record = el("div", { class: "record well scroll" });
  for (const [date, dayLines] of byDay) {
    record.append(
      el("div", { class: "passive-day" }, [
        el("span", { text: fmt.date(date) }),
        el("span", { class: "num", text: t("passive.lines", { n: dayLines.length }) }),
        el("button", {
          class: "btn danger passive-day-delete",
          type: "button",
          text: t("passive.deleteDay"),
          onclick: () => remove(
            `${t("passive.confirmDay", { date })}\n\n${notice}`,
            () => api.deletePassiveDay(date),
            refresh,
          ),
        }),
      ]),
    );

    for (const line of dayLines) {
      const at = Date.parse(line.ts_utc || "") / 1000;
      record.append(
        el("div", { class: `passive-line${line.addressed ? " addressed" : ""}` }, [
          el("span", { class: "passive-gutter" }, [
            // Most of a busy day is addressed to Jarvis, so the mark is a
            // gutter rule rather than a chip on every second line: repeated
            // often enough, a label stops being read at all. It carries its
            // own name so it is a marker and not just a colour.
            line.addressed
              ? el("span", {
                  class: "passive-mark",
                  role: "img",
                  "aria-label": t("passive.addressed"),
                  title: t("passive.addressed"),
                })
              : null,
            el("time", {
              class: "when num",
              text: fmt.time(at),
              datetime: line.ts_utc || "",
            }),
          ]),
          el("span", { class: "passive-text", text: line.text || "" }),
          el("button", {
            class: "btn danger passive-line-delete",
            type: "button",
            text: t("common.delete"),
            "aria-label": `${t("common.delete")} ${fmt.time(at)}`,
            onclick: () => remove(
              `${t("passive.confirmLine")}\n\n${notice}`,
              () => api.deletePassiveLine(line.id),
              refresh,
            ),
          }),
        ]),
      );
    }
  }
  container.append(record);
}

async function remove(question, run, refresh) {
  if (!window.confirm(question)) return;
  try {
    await run();
    await refresh();
  } catch (error) {
    toast(error.message, "bad");
  }
}
