/* What the assistant is doing, said in the reader's terms.

   The runtime publishes one word per phase, and outside a conversation two
   of those words describe one wait. `idle` and `capturing` both mean the
   wake word has not been said: voice activity opens the microphone for
   whoever is in the room, and what it heard still has to be checked for the
   name. Said as two sentences they turn the header into a flicker between
   them every time anyone speaks nearby, and the second sentence carries
   nothing the reader can act on.

   Inside a conversation the same two words mean something else, because
   nothing needs the wake word then, and that difference is worth the words.

   The phase stays one word because the runtime measures with it. The
   sentence built here is the reading, and it is built in one place so the
   header, the face, and the conversation band cannot disagree about what
   the same moment means. */

import { t } from "./i18n.js";

/* `phase` is the runtime's own word. `reading` is what the page knows around
   it: whether it is still connected, and whether a conversation is open. */
export function phaseLabel(phase, reading = {}) {
  const { connected = true, conversation = false } = reading;

  // A page on its own is showing the last thing it heard, which ages badly
  // in exactly the seconds a reader is watching it.
  if (connected === false) return t("common.reconnecting");

  const known = phase && t(`phase.${phase}`) !== `phase.${phase}`;
  if (!known) return t("phase.offline");

  if (conversation) {
    return phase === "idle" ? t("phase.idle.conversation") : t(`phase.${phase}`);
  }

  // Overheard speech is still the wait for the wake word, not an exchange.
  if (phase === "capturing") return t("phase.idle");

  return t(`phase.${phase}`);
}
