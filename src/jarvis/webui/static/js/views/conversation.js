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

  const discardedCard = el("section", { class: "card" });
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

  root.append(head, discardedCard, list, composer);

  async function refresh() {
    const payload = await api.conversation(50);
    paintDiscarded(discardedCard, payload.discarded || {});
    paintTurns(list, payload.turns || []);
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

  return () => {
    off();
    offDiscard();
  };
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
