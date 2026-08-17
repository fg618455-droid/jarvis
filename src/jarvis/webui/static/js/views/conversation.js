/* Conversation: every turn with what it cost, and a way to type one. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { chip, clear, el, empty, stageBar, toast } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("conversation.title") }),
    el("p", { text: t("conversation.lead") }),
  ]);

  const modeCard = el("section", { class: "card" });
  const discardedCard = el("section", { class: "card" });
  const passiveCard = el("section", { class: "card" });
  const list = el("div", { class: "rows" });

  const input = el("textarea", {
    placeholder: t("conversation.placeholder"),
    rows: "2",
  });
  const speak = el("input", { type: "checkbox" });
  const send = el("button", { class: "btn primary", type: "button", text: t("conversation.send") });
  const composer = el("div", { class: "composer" }, [
    input,
    el("div", { class: "composer-row" }, [
      el("label", { class: "check" }, [speak, t("conversation.speak")]),
      el("span", { class: "muted", style: "margin-left:auto" }),
      send,
    ]),
  ]);

  root.append(head, modeCard, discardedCard, passiveCard, list, composer);

  async function refresh() {
    const [conversation, passive] = await Promise.all([
      api.conversation(50),
      api.passive("", 500),
    ]);
    paintMode(modeCard, Boolean(conversation.conversation_mode), refresh);
    paintDiscarded(discardedCard, conversation.discarded || {});
    paintTurns(list, conversation.turns || []);
    paintPassive(passiveCard, passive, refresh);
  }

  async function submit() {
    const text = input.value.trim();
    if (!text || send.disabled) return;

    send.disabled = true;
    const previous = send.textContent;
    send.textContent = t("conversation.thinking");
    try {
      await api.chat(text, speak.checked);
      input.value = "";
      await refresh();
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      send.disabled = false;
      send.textContent = previous;
      input.focus();
    }
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    // Enter sends, Shift+Enter breaks the line: this is a message box, not
    // a document.
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submit();
    }
  });

  await refresh();
  const off = live.on("turn", () => refresh().catch(() => {}));
  const offDiscard = live.on("discarded", () => refresh().catch(() => {}));
  const offPassive = live.on("passive", () => refresh().catch(() => {}));
  // The conversation also ends on its own, when the judge hears the user
  // ask Jarvis to stop, so the card follows the runtime rather than the
  // last thing this page clicked.
  const offMode = live.on("conversation", () => refresh().catch(() => {}));

  return () => {
    off();
    offDiscard();
    offPassive();
    offMode();
  };
}

function paintMode(container, active, refresh) {
  clear(container);
  const toggle = el("button", {
    class: active ? "btn" : "btn primary",
    type: "button",
    text: active ? t("conversation.modeTurnOff") : t("conversation.modeTurnOn"),
    onclick: async () => {
      toggle.disabled = true;
      try {
        await api.setConversationMode(!active);
        await refresh();
      } catch (error) {
        toast(t("conversation.modeNoListener"), "bad");
      } finally {
        toggle.disabled = false;
      }
    },
  });
  container.append(
    el("header", {}, [
      el("h2", { text: t("conversation.modeTitle") }),
      chip(active ? t("conversation.modeOn") : t("conversation.modeOff"),
           active ? "accent" : null),
      el("span", { class: "spacer" }),
      toggle,
    ]),
    el("p", { class: "passive-notice", text: t("conversation.modeLead") }),
  );
}


function paintPassive(container, payload, refresh) {
  clear(container);
  const lines = payload.lines || [];
  const deleteNotice = t("passive.deleteNotice");
  const deleteAll = el("button", {
    class: "btn danger",
    type: "button",
    text: t("passive.deleteAll"),
    disabled: !lines.length,
    onclick: async () => {
      if (!window.confirm(`${t("passive.confirmAll")}\n\n${deleteNotice}`)) return;
      try {
        await api.deletePassiveAll();
        await refresh();
      } catch (error) {
        toast(error.message, "bad");
      }
    },
  });
  container.append(
    el("header", {}, [
      el("h2", { text: t("passive.title") }),
      chip(
        payload.enabled ? t("passive.on") : t("passive.off"),
        payload.enabled ? "bad" : null,
      ),
      el("span", {
        class: "aside",
        text: t("passive.waiting", { n: payload.undigested_count || 0 }),
      }),
      deleteAll,
    ]),
    el("p", { class: "passive-notice", text: deleteNotice }),
  );

  if (!lines.length) {
    container.append(empty(t("passive.empty")));
    return;
  }

  const byDay = new Map();
  for (const line of lines) {
    if (!byDay.has(line.date_utc)) byDay.set(line.date_utc, []);
    byDay.get(line.date_utc).push(line);
  }

  const groups = el("div", { class: "passive-days" });
  for (const [date, dayLines] of byDay) {
    const dayDelete = el("button", {
      class: "btn danger",
      type: "button",
      text: t("passive.deleteDay"),
      onclick: async () => {
        if (!window.confirm(`${t("passive.confirmDay", { date })}\n\n${deleteNotice}`)) return;
        try {
          await api.deletePassiveDay(date);
          await refresh();
        } catch (error) {
          toast(error.message, "bad");
        }
      },
    });
    const group = el("section", { class: "passive-day" }, [
      el("header", {}, [el("h3", { text: fmt.date(date) }), dayDelete]),
    ]);

    for (const line of dayLines) {
      const epoch = new Date(line.ts_utc).getTime() / 1000;
      group.append(
        el("div", { class: "passive-line" }, [
          el("time", { class: "when", text: fmt.time(epoch), datetime: line.ts_utc }),
          el("span", { class: "passive-text", text: line.text }),
          line.addressed ? chip(t("passive.addressed"), "accent") : null,
          el("button", {
            class: "btn danger passive-line-delete",
            type: "button",
            text: t("common.delete"),
            onclick: async () => {
              if (!window.confirm(`${t("passive.confirmLine")}\n\n${deleteNotice}`)) return;
              try {
                await api.deletePassiveLine(line.id);
                await refresh();
              } catch (error) {
                toast(error.message, "bad");
              }
            },
          }),
        ]),
      );
    }
    groups.append(group);
  }
  container.append(groups);
}

function paintDiscarded(container, discarded) {
  clear(container);
  const total = Object.values(discarded).reduce((a, b) => a + b, 0);
  container.append(
    el("header", {}, [
      el("h2", { text: t("overview.discarded") }),
      el("span", { class: "aside", text: t("conversation.discardedNote") }),
    ]),
  );
  if (!total) {
    container.append(el("span", { class: "muted", text: t("common.none") }));
    return;
  }
  container.append(
    el(
      "div",
      { class: "turn-tools" },
      Object.entries(discarded).map(([reason, count]) => chip(`${reason} · ${count}`)),
    ),
  );
}

function paintTurns(container, turns) {
  clear(container);
  if (!turns.length) {
    container.append(empty(t("conversation.empty")));
    return;
  }

  for (const turn of [...turns].reverse()) {
    container.append(
      el("article", { class: "turn" }, [
        el("div", { class: "turn-head" }, [
          el("span", { class: "when", text: fmt.time(turn.started_at) }),
          chip(turn.source === "text" ? "text" : "voice"),
          turn.language
            ? chip(
                `${t("conversation.language")} ${turn.language}${
                  turn.language_probability
                    ? ` ${fmt.number(turn.language_probability, 2)}`
                    : ""
                }`,
              )
            : null,
          el("span", { class: "num", style: "margin-left:auto", text: fmt.ms(turn.total_ms) }),
        ]),
        el("div", { class: "turn-said", text: turn.transcript || "—" }),
        turn.tools?.length
          ? el(
              "div",
              { class: "turn-tools" },
              turn.tools.map((tool) =>
                chip(`${tool.name} ${fmt.ms(tool.duration_ms)}`, tool.ok ? null : "bad"),
              ),
            )
          : null,
        el("div", { class: "turn-reply", text: turn.reply || turn.error || "—" }),
        stageBar(turn),
      ]),
    );
  }
}
