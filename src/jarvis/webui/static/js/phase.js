/* What the assistant is doing, said in the reader's terms.

   The runtime publishes one word per phase, and two of those words cover
   situations a reader would never call the same thing. `idle` while a
   conversation is running is not waiting for the wake word: nothing needs
   the wake word then. `capturing` outside a conversation is not listening
   to you: voice activity opened the microphone for whoever is in the room,
   and what it heard is on its way to a wake-word check, or to the passive
   record, and usually neither is a question for Jarvis.

   The phase stays one word because the runtime measures with it. The
   sentence built here is the reading, and it is built in one place so the
   header, the face, and the conversation band cannot disagree about what
   the same moment means. */

import { t } from "./i18n.js";

/* `phase` is the runtime's own word. `reading` is what the page knows
   around it: whether it is still connected, whether a conversation is open,
   and whether the room is being written down. */
export function phaseLabel(phase, reading = {}) {
  const { connected = true, conversation = false, passive = false } = reading;

  // A page on its own is showing the last thing it heard, which ages badly
  // in exactly the seconds a reader is watching it.
  if (connected === false) return t("common.reconnecting");

  const known = phase && t(`phase.${phase}`) !== `phase.${phase}`;
  if (!known) return t("phase.offline");

  if (conversation) {
    if (phase === "idle") return t("phase.idle.conversation");
    return t(`phase.${phase}`);
  }

  if (phase === "capturing") {
    return passive ? t("phase.capturing.record") : t("phase.capturing.wake");
  }

  return t(`phase.${phase}`);
}
