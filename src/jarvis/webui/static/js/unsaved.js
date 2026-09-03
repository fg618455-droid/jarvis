/* What the page is holding that is not on disk yet.

   Three views edit a file rather than press a button: Settings, the MCP
   editor and the LLM route editor. All three collect a whole form and write
   it in one go, because the thing being written is only coherent once its
   parts agree, and until Save is pressed everything typed lives in the page
   and nowhere else.

   The MCP editor is why this exists. Its fields are credentials, and a saved
   credential is read back masked, so what is discarded there is not an edit
   to make again: it is a secret to go and find again.

   A view says whether it is holding anything by leaving a check here, and
   takes the check back when it is torn down. Nothing here decides what to do
   about it — the shell asks, because the shell is what knows the page is
   about to become a different one. */

const holders = new Set();

/* Register a check and get back the way to withdraw it. A view that
   registers without withdrawing would go on answering for a page that no
   longer exists, so the return value belongs in the view's own cleanup. */
export function holdingUnsaved(check) {
  holders.add(check);
  return () => holders.delete(check);
}

export function anythingUnsaved() {
  for (const check of holders) {
    try {
      if (check()) return true;
    } catch (error) {
      // A view too broken to answer is not a reason to throw away whatever
      // it was holding. The expensive answer is the safe one here.
      console.error("a view could not say whether it holds unsaved changes", error);
      return true;
    }
  }
  return false;
}
