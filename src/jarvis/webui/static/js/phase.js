/* What the assistant is doing, said in the reader's terms.

   The runtime publishes one word per phase, and outside a conversation
   three of those words describe one wait. `idle`, `capturing` and
   `transcribing` all mean the wake word has not been said yet: voice
   activity opens the microphone for whoever is in the room, and the
   recogniser runs on what it caught so the name can be looked for in it. A
   room with people in it passes through all three several times a minute,
   and none of it is an exchange with Jarvis.

   Outside a conversation they are therefore one reading: the same sentence,
   the same dot, no motion. Inside a conversation every utterance is
   addressed to Jarvis, and the same phases are then worth showing, because
   they are the work the reader is waiting on.

   The phase stays one word because the runtime measures with it. The
   reading built here is what an interface shows, and it is built in one
   place so the header, the face, and the conversation band cannot disagree
   about what the same moment means. */

import { t } from "./i18n.js";

/* The phases that, on their own, only mean the wake word has not been said. */
const BEFORE_A_TURN = new Set(["capturing", "transcribing"]);

/* The phase an interface should paint: the runtime's word, unless it is a
   step towards finding out whether Jarvis was addressed at all. Everything
   painted from a phase (the dot, the pill, the wait's stopwatch) takes it
   from here, so nothing moves while the words hold still. */
export function displayPhase(phase, reading = {}) {
  const { conversation = false } = reading;
  if (!conversation && BEFORE_A_TURN.has(phase)) return "idle";
  return phase;
}

/* `phase` is the runtime's own word. `reading` is what the page knows around
   it: whether it is still connected, and whether a conversation is open. */
export function phaseLabel(phase, reading = {}) {
  const { connected = true, conversation = false } = reading;

  // A page on its own is showing the last thing it heard, which ages badly
  // in exactly the seconds a reader is watching it.
  if (connected === false) return t("common.reconnecting");

  const known = phase && t(`phase.${phase}`) !== `phase.${phase}`;
  if (!known) return t("phase.offline");

  const shown = displayPhase(phase, reading);
  if (conversation && shown === "idle") return t("phase.idle.conversation");
  return t(`phase.${shown}`);
}
