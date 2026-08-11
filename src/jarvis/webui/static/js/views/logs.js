/* Logs: locally retained diagnostic events for the Control Centre. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { clear, el, empty } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("logs.title") }),
    el("p", { text: t("logs.lead") }),
  ]);
  const card = el("section", { class: "card" });
  root.append(head, card);

  async function refresh() {
    const payload = await api.logs();
    const entries = payload.entries || [];
    clear(card);
    if (!entries.length) {
      card.append(empty(t("logs.empty")));
      return;
    }
    const lines = entries.map((entry) => {
      const time = new Date((entry.timestamp || 0) * 1000).toLocaleTimeString();
      return `[${time}] [${entry.category || "debug"}] ${entry.message || ""}`;
    });
    card.append(el("pre", { class: "log-lines", text: lines.join("\n") }));
  }

  await refresh();
  const timer = setInterval(() => refresh().catch(() => {}), 2000);
  return () => clearInterval(timer);
}
