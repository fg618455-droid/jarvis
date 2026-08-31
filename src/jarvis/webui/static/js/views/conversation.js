/* Conversation: what was said, what came back, and what is happening now.

   The daemon publishes its phase and the stage a turn has reached, and the
   browser can hand it the microphone. Between them there is enough to show
   a conversation as it happens rather than a table of finished ones, so
   this view is built around the exchange and puts the readings beside it
   instead of on top of it.

   Two live facts sit in the band and are never conflated. One is what
   Jarvis is doing, which is the daemon's own phase and would be true with
   this page closed. The other is whether *this browser* is feeding it
   audio, which is true only here. A page that mixed them would say the
   assistant is listening when nothing had opened a microphone. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { createMic } from "../mic.js";
import { live } from "../sse.js";
import { ICONS, chip, clear, el, empty, icon, motionAllowed, stageBar, toast } from "../ui.js";

/* Phases a turn passes through. While the assistant is in one of them
   something is happening and the wait is worth counting; at idle nothing
   is, and the reading holds still. */
const IN_FLIGHT = new Set([
  "capturing", "transcribing", "thinking", "tool", "speaking", "dictating",
]);

/* Fast enough that the count reads as a stopwatch on a wait measured in
   seconds, and it runs only while a turn is in flight. */
const TICK_MS = 200;
const LEVEL_MS = 60;

export async function mount(root) {
  const state = {
    phase: null,
    phaseSince: 0,
    stage: "",
    mode: false,
    lastTotalMs: null,
    /* Turn ids that have been on screen. A turn glows on the reading that
       first brought it in and never again. */
    seen: new Set(),
    refresh: async () => {},
  };

  // The view fills the window rather than growing past it, so the exchange
  // is the only thing that scrolls and the composer stays where it is.
  root.classList.add("view-conversation");

  const band = el("section", { class: "card voice" });
  const dialogue = el("div", { class: "dialogue well scroll" });
  const composer = el("div", { class: "composer" });

  root.append(
    el("div", { class: "view-head" }, [
      el("h1", { text: t("conversation.title") }),
      el("p", { text: t("conversation.lead") }),
    ]),
    band,
    dialogue,
    composer,
  );

  const capture = buildCapture(band, state);
  const typing = buildComposer(composer, () => refresh());
  buildBand(band, capture);

  async function refresh() {
    const payload = await api.conversation(50);
    state.mode = Boolean(payload.conversation_mode);
    capture.paintMode();
    paintDiscarded(band, payload.discarded || {});
    paintDialogue(dialogue, payload.turns || [], state);
  }

  state.refresh = refresh;
  await refresh();
  capture.paintReading();

  // The band is correct from the moment it appears rather than from the
  // next phase change, which on an idle assistant may never come.
  const status = await api.status().catch(() => null);
  if (status) {
    state.phase = status.phase || state.phase;
    state.phaseSince = status.phase_since || Date.now() / 1000;
    state.lastTotalMs = status.last_turn?.total_ms ?? null;
    capture.paintReading();
  }

  const offTurn = live.on("turn", (turn) => {
    state.lastTotalMs = turn?.total_ms ?? state.lastTotalMs;
    state.stage = "";
    refresh().catch(() => {});
  });
  const offDiscard = live.on("discarded", () => refresh().catch(() => {}));
  // The conversation also ends on its own, when the judge hears the user
  // ask Jarvis to stop, so the switch follows the runtime rather than the
  // last thing this page clicked.
  const offMode = live.on("conversation", () => refresh().catch(() => {}));
  const offPhase = live.on("phase", (phase) => {
    state.phase = phase.phase;
    state.phaseSince = phase.phase_since || Date.now() / 1000;
    if (!IN_FLIGHT.has(state.phase)) state.stage = "";
    capture.paintReading();
  });
  // A stage says which part of the turn the wait is in, which the phase
  // alone cannot: "running a tool" is true of every tool there is.
  const offStage = live.on("stage", (event) => {
    state.stage = event.stage || "";
    capture.paintReading();
  });

  const ticking = setInterval(() => {
    if (IN_FLIGHT.has(state.phase)) capture.paintReading();
  }, TICK_MS);

  return () => {
    offTurn();
    offDiscard();
    offMode();
    offPhase();
    offStage();
    clearInterval(ticking);
    typing.stop();
    capture.release();
  };
}

/* ── The live band ──────────────────────────────────────────────────── */

function buildCapture(band, state) {
  const button = el("button", {
    class: "voice-mic",
    type: "button",
    "aria-pressed": "false",
    "aria-label": t("conversation.mic.start"),
  }, [icon(ICONS.microphone)]);

  // The meter is painted from JavaScript, so the stylesheet's reduced-motion
  // rule cannot reach it. It asks for itself, and what it shows is written
  // beside it in words either way.
  const meter = motionAllowed()
    ? el("span", { class: "voice-level", "aria-hidden": "true" }, [el("span")])
    : null;

  const micState = el("span", { class: "voice-mic-state" });
  const phaseDot = el("span", { class: "voice-phase-dot dot", "data-phase": "idle" });
  const phaseName = el("span", { class: "voice-phase-name" });
  const stageName = el("span", { class: "voice-stage num" });
  const elapsed = el("span", { class: "voice-elapsed num" });
  const modeChip = el("span");
  const modeButton = el("button", { class: "btn", type: "button" });

  let mic = null;
  let levelTimer = null;

  function paintReading() {
    const phase = state.phase || "offline";
    phaseDot.dataset.phase = phase;
    phaseName.textContent = t(`phase.${phase}`) || phase;
    stageName.textContent = state.stage;

    // While something is happening, how long it has been happening. When
    // nothing is, the last wait that finished, held still.
    if (IN_FLIGHT.has(phase) && state.phaseSince) {
      elapsed.textContent = fmt.ms((Date.now() / 1000 - state.phaseSince) * 1000);
      elapsed.classList.add("running");
    } else {
      elapsed.textContent = state.lastTotalMs === null ? "" : fmt.ms(state.lastTotalMs);
      elapsed.classList.remove("running");
    }
  }

  function paintMic(micState_) {
    const open = micState_ !== "idle";
    button.classList.toggle("open", open);
    button.setAttribute("aria-pressed", open ? "true" : "false");
    button.setAttribute(
      "aria-label",
      open ? t("conversation.mic.stop") : t("conversation.mic.start"),
    );
    micState.textContent = micState_ === "starting"
      ? t("conversation.mic.starting")
      : open ? t("conversation.mic.open") : t("conversation.mic.closed");
    micState.classList.toggle("open", open);

    if (meter) {
      if (open && !levelTimer) {
        levelTimer = setInterval(() => {
          meter.firstChild.style.height = `${(mic.level * 100).toFixed(1)}%`;
        }, LEVEL_MS);
      } else if (!open && levelTimer) {
        clearInterval(levelTimer);
        levelTimer = null;
        meter.firstChild.style.height = "0%";
      }
    }
  }

  mic = createMic({
    onState: paintMic,
    onError: (error) => toast(`${t("conversation.mic.failed")}: ${error.message}`),
  });

  button.addEventListener("click", async () => {
    // A daemon that is not listening has nowhere to put the audio, so say
    // that instead of opening the microphone and dropping every frame.
    if (!mic.running) {
      const reachable = await api.voiceStatus().catch(() => null);
      if (!reachable?.ingress) {
        toast(t("conversation.mic.noListener"));
        return;
      }
    }
    mic.toggle().catch(() => {});
  });

  modeButton.addEventListener("click", async () => {
    modeButton.disabled = true;
    try {
      await api.setConversationMode(!state.mode);
      await state.refresh();
    } catch {
      toast(t("conversation.modeNoListener"), "bad");
    } finally {
      modeButton.disabled = false;
    }
  });

  paintMic("idle");

  return {
    nodes: { button, meter, micState, phaseDot, phaseName, stageName, elapsed, modeChip, modeButton },
    paintReading,
    paintMode() {
      clear(modeChip);
      modeChip.append(
        chip(
          state.mode ? t("conversation.modeOn") : t("conversation.modeOff"),
          state.mode ? "accent" : null,
        ),
      );
      modeButton.className = state.mode ? "btn" : "btn primary";
      modeButton.textContent = state.mode
        ? t("conversation.modeTurnOff")
        : t("conversation.modeTurnOn");
    },
    release() {
      if (levelTimer) clearInterval(levelTimer);
      // Leaving the view releases the microphone. A capture the user cannot
      // see is a capture they cannot stop.
      mic.stop().catch(() => {});
    },
  };
}

function buildBand(band, capture) {
  const { button, meter, micState, phaseDot, phaseName, stageName, elapsed, modeChip, modeButton } =
    capture.nodes;

  band.append(
    el("div", { class: "voice-capture" }, [
      button,
      meter,
      el("span", { class: "voice-capture-read" }, [
        el("span", { class: "voice-label", text: t("conversation.mic.title") }),
        micState,
      ]),
    ]),
    el("div", { class: "voice-reading" }, [
      el("span", { class: "voice-label", text: t("conversation.doingNow") }),
      el("div", { class: "voice-phase" }, [phaseDot, phaseName, stageName, elapsed]),
    ]),
    el("div", { class: "voice-mode" }, [
      el("span", { class: "voice-label" }, [
        el("span", { text: t("conversation.modeTitle") }),
        modeChip,
      ]),
      modeButton,
      el("span", { class: "voice-mode-lead", text: t("conversation.modeLead") }),
    ]),
  );
}

/* Utterances thrown away never became a turn, so they belong beside the
   conversation rather than in it, and only when there are any: a counter
   permanently reading zero teaches a reader to stop looking at it. */
function paintDiscarded(band, discarded) {
  band.querySelector(".voice-discarded")?.remove();
  const entries = Object.entries(discarded).filter(([, count]) => count);
  if (!entries.length) return;

  band.append(
    el("div", { class: "voice-discarded" }, [
      el("span", { class: "voice-label", text: t("deck.discarded") }),
      el(
        "span",
        { class: "turn-tools" },
        entries.map(([reason, count]) => chip(`${reason} · ${count}`)),
      ),
      el("span", { class: "aside", text: t("conversation.discardedNote") }),
    ]),
  );
}

/* ── The exchange ───────────────────────────────────────────────────── */

function paintDialogue(container, turns, state) {
  // Whether the reader was at the newest turn before this repaint. Someone
  // reading back through the history is not dragged to the bottom because
  // a turn finished elsewhere.
  const atBottom =
    container.scrollHeight - container.scrollTop - container.clientHeight < 40;
  const firstLoad = state.seen.size === 0;

  clear(container);
  if (!turns.length) {
    container.append(empty(t("conversation.empty")));
    return;
  }

  let currentDay = null;
  // Oldest first, so the newest turn ends up beside the composer and the
  // page reads downwards the way the conversation happened.
  for (const turn of turns) {
    const day = new Date((turn.started_at || 0) * 1000).toDateString();
    if (day !== currentDay) {
      currentDay = day;
      container.append(
        el("div", { class: "feed-day", text: fmt.date(new Date((turn.started_at || 0) * 1000).toISOString()) }),
      );
    }
    container.append(turnCard(turn, firstLoad, state));
  }

  for (const turn of turns) state.seen.add(turn.turn_id);
  if (atBottom || firstLoad) container.scrollTop = container.scrollHeight;
}

function turnCard(turn, firstLoad, state) {
  const isNew = !firstLoad && !state.seen.has(turn.turn_id);
  const failed = Boolean(turn.error);

  return el("article", { class: `turn${isNew ? " arrived" : ""}` }, [
    el("div", { class: "turn-meta" }, [
      el("span", { class: "when num", text: fmt.time(turn.started_at) }),
      chip(turn.source === "text" ? t("conversation.typed") : t("conversation.spoken")),
      turn.language
        ? el("span", {
            text: turn.language_probability
              ? `${turn.language} ${fmt.number(turn.language_probability, 2)}`
              : turn.language,
          })
        : null,
      el("span", { class: "num turn-cost", text: fmt.ms(turn.total_ms) }),
    ]),
    el("div", { class: "turn-line" }, [
      el("span", { class: "who", text: t("deck.you") }),
      el("p", { class: "turn-said", text: turn.transcript || "—" }),
    ]),
    el("div", { class: "turn-line" }, [
      el("span", { class: "who", text: t("deck.jarvis") }),
      el("p", {
        class: `turn-reply${failed ? " failed" : ""}`,
        text: turn.reply || turn.error || "—",
      }),
    ]),
    el("details", { class: "turn-trace" }, [
      el("summary", {}, [
        el("span", { text: t("conversation.trace") }),
        turn.tools?.length
          ? el("span", { class: "turn-trace-count num", text: String(turn.tools.length) })
          : null,
      ]),
      turn.tools?.length
        ? el(
            "div",
            { class: "turn-tools" },
            turn.tools.map((tool) =>
              chip(`${tool.name} ${fmt.ms(tool.duration_ms)}`, tool.ok ? null : "bad"),
            ),
          )
        : null,
      stageBar(turn),
    ]),
  ]);
}

/* ── Typing one ─────────────────────────────────────────────────────── */

function buildComposer(container, refresh) {
  const input = el("textarea", {
    placeholder: t("conversation.placeholder"),
    rows: "2",
    "aria-label": t("conversation.placeholder"),
  });
  const speak = el("input", { type: "checkbox" });
  const send = el("button", {
    class: "btn primary",
    type: "button",
    text: t("conversation.send"),
  });

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

  container.append(
    input,
    el("div", { class: "composer-row" }, [
      el("label", { class: "check" }, [speak, t("conversation.speak")]),
      el("span", { class: "spacer" }),
      send,
    ]),
  );

  return { stop() { send.disabled = true; } };
}
