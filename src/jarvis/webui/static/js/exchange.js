/* The exchange, on the face's own stage.

   What is being said sits between the face and the thing you type into,
   because that is where a conversation happens: you talk to the face, and
   what has just been said stands under it. It is a live thing rather than a
   record — the last few turns, newest against the dock, older ones rising
   away above them until they are gone. The whole history is the
   conversation panel's, and always was.

   A turn arrives here in three pieces, in the order they become true rather
   than all at once at the end. What was understood comes first, before any
   answer exists. The answer follows in the pieces it is written in. The
   finished turn comes last and is what everything on screen is reconciled
   against: a page that missed a piece of the stream still ends up showing
   exactly what was said.

   Typing is the one case where this page knows something before the daemon
   tells it: the message is on screen the moment it is sent, and the `heard`
   event that follows adopts that bubble rather than adding a second one. */

import { t } from "./i18n.js";
import { el } from "./ui.js";

/* How many exchanges stand under the face. Enough to read back through the
   last thing that happened without the face losing the room it needs to be
   the largest object on the page. */
const KEPT = 6;

/* A reader who has scrolled up is reading; anything further from the bottom
   than this is not "nearly at the newest" by accident. */
const AT_BOTTOM_PX = 32;

export function createExchange(root) {
  root.classList.add("face-exchange");
  root.setAttribute("role", "log");
  root.setAttribute("aria-live", "polite");
  root.setAttribute("aria-label", t("face.exchange"));

  /* Every turn on screen, oldest first, keyed by the turn it belongs to. A
     turn typed here has no id until the daemon answers with one, so it is
     held under a local key until `heard` says what the real one is. */
  const turns = [];
  let pendingCount = 0;

  function find(id) {
    return turns.find((turn) => turn.id === id) || null;
  }

  function atBottom() {
    return root.scrollHeight - root.scrollTop - root.clientHeight < AT_BOTTOM_PX;
  }

  /* Follow the newest line, unless the reader has gone looking at an older
     one: an answer arriving elsewhere must not drag them back down. */
  function follow(wasAtBottom) {
    if (wasAtBottom) root.scrollTop = root.scrollHeight;
  }

  function trim() {
    while (turns.length > KEPT) {
      const oldest = turns.shift();
      oldest.node.remove();
    }
  }

  /* One exchange: what you said, and what came back. The answer's line is
     built with it rather than when the first token lands, so the wait has
     somewhere to be shown and the block does not change height the moment
     the reply starts. */
  function open(id, said, { animate = true } = {}) {
    const wasAtBottom = atBottom();

    const yours = el("p", { class: "exchange-said", text: said });
    const theirs = el("p", { class: "exchange-reply" });
    const node = el("article", { class: `exchange-turn${animate ? " rising" : ""}` }, [
      el("div", { class: "exchange-line", dataset: { who: "you" } }, [
        el("span", { class: "exchange-who", text: t("deck.you") }),
        yours,
      ]),
      el("div", { class: "exchange-line", dataset: { who: "jarvis" } }, [
        el("span", { class: "exchange-who", text: t("deck.jarvis") }),
        theirs,
      ]),
    ]);

    const turn = { id, said, written: "", node, yours, theirs, settled: false };
    turns.push(turn);
    root.append(node);
    waiting(turn, true);
    trim();
    follow(wasAtBottom);
    return turn;
  }

  /* Whether Jarvis is still writing this one. The dots are drawn rather than
     written, so a reader who has asked for no motion gets a still mark in
     the same place instead of nothing at all; the state is also on the
     element, which is what a screen reader is given. */
  function waiting(turn, is) {
    turn.node.dataset.writing = is ? "true" : "false";
    if (!is) return;
    turn.theirs.replaceChildren(
      el("span", { class: "exchange-writing", "aria-label": t("face.writing") }, [
        el("span"), el("span"), el("span"),
      ]),
    );
  }

  function write(turn, text) {
    const wasAtBottom = atBottom();
    turn.node.dataset.writing = "false";
    turn.theirs.textContent = text;
    follow(wasAtBottom);
  }

  /* What was understood, before there is an answer to it. A message typed
     here is already on screen, so its own event adopts that bubble: the
     daemon is confirming the turn it was given, not starting a second one. */
  function heard(event) {
    if (!event || !event.text || find(event.turn_id)) return;
    const adoptable = turns.find(
      (turn) => turn.pending && !turn.settled && turn.said === event.text,
    );
    if (adoptable) {
      adoptable.id = event.turn_id;
      adoptable.pending = false;
      return;
    }
    open(event.turn_id, event.text);
  }

  /* The next piece of the answer. A stream whose opening was never announced
     still lands somewhere: a turn the page joined halfway through is better
     read without its question than not read at all. */
  function reply(event) {
    if (!event || !event.delta) return;
    const turn = find(event.turn_id) || open(event.turn_id, "");
    if (turn.settled) return;
    turn.written += event.delta;
    write(turn, turn.written);
  }

  /* The finished turn, which is the truth about it. Whatever the stream
     managed to show is replaced by what was actually said, so a piece the
     page never received leaves nothing behind. */
  function settle(record, { animate = true } = {}) {
    if (!record) return;
    const turn = find(record.turn_id)
      || open(record.turn_id, record.transcript || "", { animate });
    turn.settled = true;
    if (record.transcript && record.transcript !== turn.said) {
      turn.said = record.transcript;
      turn.yours.textContent = record.transcript;
    }
    turn.node.dataset.failed = record.error ? "true" : "false";
    const answer = record.reply || record.error || "";
    if (answer) {
      write(turn, answer);
    } else {
      turn.node.dataset.writing = "false";
      turn.theirs.textContent = "";
    }
  }

  /* Sent from here, and on screen before the daemon has said anything.
     Returns the handle the caller needs to tell it how that went. */
  function pending(text) {
    pendingCount += 1;
    const turn = open(`pending:${pendingCount}`, text);
    turn.pending = true;
    return {
      failed() {
        turn.node.dataset.failed = "true";
        turn.node.dataset.writing = "false";
        turn.theirs.textContent = t("face.notSent");
      },
    };
  }

  /* The turns that were already over when the page opened. Painted without
     the arrival animation: none of them just happened. */
  function seed(history) {
    for (const record of (history || []).slice(-KEPT)) {
      if (!record || (!record.transcript && !record.reply)) continue;
      settle(record, { animate: false });
    }
  }

  return { node: root, seed, pending, heard, reply, settle };
}

/* Wire an exchange to the daemon's live stream. Kept apart from the drawing
   above so the drawing can be built and driven on its own. */
export function followLive(exchange, live) {
  const off = [
    live.on("heard", (event) => exchange.heard(event)),
    live.on("reply", (event) => exchange.reply(event)),
    live.on("turn", (turn) => exchange.settle(turn)),
  ];
  return () => off.forEach((stop) => stop());
}
