/* Face/visualizer: the vendored ai-visualizer face gallery, framed inside
   the control centre.

   The face is a self-contained third-party page (its own <html>, canvas
   rendering, and animation loop), not something built from this app's own
   `el()` helpers, so it is framed rather than reimplemented: an iframe
   pointed at the gallery the daemon serves at /visualizer/. The gallery
   itself is how a face gets picked; nothing here duplicates that switch. */

import { t } from "../i18n.js";
import { el } from "../ui.js";

export async function mount(root) {
  root.classList.add("view-visualizer");

  root.append(
    el("div", { class: "view-head" }, [
      el("h1", { text: t("visualizer.title") }),
      el("p", { text: t("visualizer.lead") }),
    ]),
    el("iframe", {
      class: "visualizer-frame",
      src: "/visualizer/",
      title: t("visualizer.title"),
    }),
  );

  return () => {};
}
