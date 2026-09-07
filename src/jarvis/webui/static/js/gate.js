/* The gate, standing where you are already looking.

   A confirmation is a question the assistant is asking mid-turn, and the
   answer is the only thing standing between it and running a tool on this
   machine. Behind a widget it is a question in a drawer: the reader is
   watching the face, the request expires on its own, and a refusal that was
   never made reads afterwards exactly like one that was.

   So it asks on the stage, immediately over the thing you type into, which
   is the last place the eye rests. What was said and what is being asked are
   the same conversation and are read as one column.

   It follows the daemon's own events and keeps a clock of its own only while
   something is waiting: a request runs out on a timer nobody pushes, so the
   count down has to be counted here. With nothing pending it asks nothing at
   all, which is the state it is in almost all of the time. */

import { api } from "./api.js";
import { t } from "./i18n.js";
import { el, toast } from "./ui.js";

/* Fast enough to read as a countdown on a wait measured in seconds, and it
   runs only while something is actually waiting. */
const TICK_MS = 1000;

export function createGate(root, { live } = {}) {
  root.classList.add("stage-gate");
  root.setAttribute("role", "group");
  root.setAttribute("aria-label", t("security.pending"));
  root.hidden = true;

  /* What was asked, keyed by request. A card is kept rather than rebuilt so
     the countdown ticks in place: replacing the node every second would move
     the buttons out from under the pointer once a second. */
  const cards = new Map();
  let ticking = null;
  let stopped = false;

  function tick() {
    let anyLeft = false;
    for (const card of cards.values()) {
      card.left = Math.max(0, card.left - TICK_MS / 1000);
      card.countdown.textContent = t("gate.secondsLeft", { n: Math.ceil(card.left) });
      if (card.left > 0) anyLeft = true;
    }
    // Everything queued has run out. The daemon decides what that means, and
    // says so on the event stream; this only stops counting.
    if (!anyLeft) pace();
  }

  function pace() {
    const wanted = cards.size > 0;
    if (wanted && !ticking) ticking = setInterval(tick, TICK_MS);
    else if (!wanted && ticking) {
      clearInterval(ticking);
      ticking = null;
    }
  }

  async function decide(requestId, approved) {
    const card = cards.get(requestId);
    if (card) card.busy(true);
    try {
      await api.decide(requestId, approved);
      // Gone the moment it is answered rather than at the next reading: the
      // question has been settled and a card still offering both answers
      // invites the reader to answer it twice.
      drop(requestId);
    } catch (error) {
      toast(error.message, "bad");
      if (card) card.busy(false);
    }
  }

  function drop(requestId) {
    const card = cards.get(requestId);
    if (!card) return;
    card.node.remove();
    cards.delete(requestId);
    root.hidden = cards.size === 0;
    pace();
  }

  function build(request) {
    const countdown = el("span", { class: "gate-left num" });
    const args = el("pre", { class: "gate-args" });
    // Whatever a tool was asked to do arrives from the model and is shown as
    // text, never as markup.
    args.textContent = JSON.stringify(request.action_args ?? {}, null, 2);

    const approve = el("button", {
      class: "btn primary gate-approve",
      type: "button",
      text: t("security.approve"),
      onclick: () => decide(request.request_id, true),
    });
    const deny = el("button", {
      class: "btn danger gate-deny",
      type: "button",
      text: t("security.deny"),
      onclick: () => decide(request.request_id, false),
    });

    const node = el("article", { class: "gate-request" }, [
      el("div", { class: "gate-head" }, [
        el("span", { class: "gate-asks", text: t("gate.asks") }),
        el("strong", { class: "gate-action", text: request.action_name || "" }),
        countdown,
      ]),
      args,
      el("div", { class: "gate-actions" }, [deny, approve]),
    ]);

    return {
      node,
      countdown,
      left: Number(request.seconds_left) || 0,
      busy(is) {
        approve.disabled = is;
        deny.disabled = is;
      },
    };
  }

  /* What is waiting, as the daemon has it. A request already on screen keeps
     its card and takes the fresh countdown; one that has gone is removed,
     however it was settled — from here, from the desktop, from Telegram, or
     by running out. */
  function paint(pending) {
    const seen = new Set();
    for (const request of pending || []) {
      if (!request || !request.request_id) continue;
      seen.add(request.request_id);
      const existing = cards.get(request.request_id);
      if (existing) {
        existing.left = Number(request.seconds_left) || 0;
        existing.countdown.textContent = t("gate.secondsLeft", {
          n: Math.ceil(existing.left),
        });
        continue;
      }
      const card = build(request);
      card.countdown.textContent = t("gate.secondsLeft", { n: Math.ceil(card.left) });
      cards.set(request.request_id, card);
      root.append(card.node);
    }
    for (const requestId of [...cards.keys()]) {
      if (!seen.has(requestId)) drop(requestId);
    }
    root.hidden = cards.size === 0;
    pace();
  }

  async function refresh() {
    try {
      const payload = await api.security();
      if (!stopped) paint(payload.pending || []);
    } catch {
      /* A reading that failed says nothing about what is waiting, so what is
         on screen stands rather than being cleared by a lost connection. */
    }
  }

  const off = live
    ? [
        live.on("confirmation", () => refresh()),
        live.on("confirmation_resolved", () => refresh()),
      ]
    : [];

  refresh();

  return {
    node: root,
    refresh,
    destroy() {
      stopped = true;
      if (ticking) clearInterval(ticking);
      ticking = null;
      off.forEach((stop) => stop());
    },
  };
}
