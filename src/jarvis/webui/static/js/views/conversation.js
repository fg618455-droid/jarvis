/* Conversation: every turn with what it cost, and a way to type one. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { createMic } from "../mic.js";
import { live } from "../sse.js";
import { chip, clear, el, empty, stageBar, toast } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("conversation.title") }),
    el("p", { text: t("conversation.lead") }),
  ]);

  const modeCard = el("section", { class: "card" });
  const discardedCard = el("section", { class: "card" });
  const list = el("div", { class: "rows" });

  const input = el("textarea", {
    placeholder: t("conversation.placeholder"),
    rows: "2",
  });
  const speak = el("input", { type: "checkbox" });
  const send = el("button", { class: "btn primary", type: "button", text: t("conversation.send") });
  const mic = el("button", { class: "btn", type: "button", text: t("conversation.mic.start") });
  const micState = el("span", { class: "muted" });
  const composer = el("div", { class: "composer" }, [
    input,
    el("div", { class: "composer-row" }, [
      el("label", { class: "check" }, [speak, t("conversation.speak")]),
      mic,
      micState,
      el("span", { class: "muted", style: "margin-left:auto" }),
      send,
    ]),
  ]);

  root.append(head, modeCard, discardedCard, list, composer);

  async function refresh() {
    const conversation = await api.conversation(50);
    paintMode(modeCard, Boolean(conversation.conversation_mode), refresh);
    paintDiscarded(discardedCard, conversation.discarded || {});
    paintTurns(list, conversation.turns || []);
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

  const capture = createMic({
    onState: (state) => {
      mic.textContent = state === "idle" ? t("conversation.mic.start") : t("conversation.mic.stop");
      mic.classList.toggle("primary", state !== "idle");
      micState.textContent = state === "starting" ? t("conversation.mic.starting") : "";
    },
    onError: (err) => toast(`${t("conversation.mic.failed")}: ${err.message}`),
  });

  mic.addEventListener("click", async () => {
    // A daemon that is not listening has nowhere to put the audio, so say
    // that instead of opening the microphone and dropping every frame.
    if (!capture.running) {
      const status = await api.voiceStatus().catch(() => null);
      if (!status?.ingress) {
        toast(t("conversation.mic.noListener"));
        return;
      }
    }
    capture.toggle().catch(() => {});
  });

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
  // The conversation also ends on its own, when the judge hears the user
  // ask Jarvis to stop, so the card follows the runtime rather than the
  // last thing this page clicked.
  const offMode = live.on("conversation", () => refresh().catch(() => {}));

  return () => {
    off();
    offDiscard();
    offMode();
    // Leaving the view releases the microphone. A capture the user cannot
    // see is a capture they cannot stop.
    capture.stop().catch(() => {});
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
