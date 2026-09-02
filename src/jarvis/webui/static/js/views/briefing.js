/* Today: what is on, and the prose only if you ask for it.

   The panel shows the same two readings the endpoint separates, and keeps
   them separated. The items are extracted from the School branch and cost
   nothing, so they are simply there. The summary is a CHAT-tier call, so it
   is behind a button and says when it was made.

   The spoken morning briefing is reported here rather than hidden in
   Settings, because "did it already tell me this today" is the first thing
   you want to know when you are reading the same briefing on a screen. */

import { api } from "../api.js";
import { t } from "../i18n.js";
import { chip, clear, el, empty, toast } from "../ui.js";

export async function mount(root) {
  const head = el("div", { class: "view-head" }, [
    el("h1", { text: t("briefing.title") }),
    el("p", { text: t("briefing.lead") }),
  ]);

  const summaryCard = el("section", { class: "card" });
  const itemsCard = el("section", { class: "card" });
  const spokenCard = el("section", { class: "card" });

  root.append(head, summaryCard, itemsCard, spokenCard);

  let payload = await api.briefing();
  paint();

  async function generate(button) {
    button.disabled = true;
    try {
      payload = await api.refreshBriefing();
      paint();
    } catch (error) {
      // 409 is "there is nothing to summarise", which is a fact about the
      // School branch rather than a failure of the request.
      toast(error.message, "bad");
    } finally {
      button.disabled = false;
    }
  }

  function paint() {
    paintSummary();
    paintItems();
    paintSpoken();
  }

  function paintSummary() {
    clear(summaryCard);
    const button = el("button", {
      class: "btn primary",
      type: "button",
      text: payload.summary ? t("briefing.regenerate") : t("briefing.generate"),
      disabled: !payload.available,
      title: payload.available ? "" : t("briefing.nothingToSummarise"),
      onclick: (event) => generate(event.target),
    });

    summaryCard.append(
      el("header", {}, [
        el("h2", { text: t("briefing.summary") }),
        el("span", { class: "aside" }, [button]),
      ]),
      payload.summary
        ? el("p", { class: "briefing-summary", text: payload.summary })
        : empty(t("briefing.noSummary")),
    );
  }

  function paintItems() {
    clear(itemsCard);
    itemsCard.append(
      el("header", {}, [
        el("h2", { text: t("briefing.items") }),
        el("span", { class: "aside", text: String((payload.items || []).length) }),
      ]),
    );

    if (!payload.items?.length) {
      itemsCard.append(empty(t("briefing.nothingOn")));
      return;
    }

    itemsCard.append(
      el("div", { class: "scroll" }, [
        el(
          "div",
          { class: "briefing-items" },
          // Everything below is stored text the assistant learned, so it is
          // set as text nodes and never as markup.
          payload.items.map((item) =>
            el("div", { class: "briefing-item" }, [
              el("span", { class: "briefing-item-title", text: item.title }),
              item.note && el("span", { class: "briefing-item-note", text: item.note }),
            ]),
          ),
        ),
      ]),
    );
  }

  function paintSpoken() {
    clear(spokenCard);
    const spoken = payload.spoken || {};
    spokenCard.append(
      el("header", {}, [
        el("h2", { text: t("briefing.spoken") }),
        el("span", { class: "aside" }, [
          chip(
            spoken.enabled ? t("briefing.spokenOn") : t("briefing.spokenOff"),
            spoken.enabled ? "ok" : null,
          ),
        ]),
      ]),
      el("div", { class: "rows" }, [
        el("div", { class: "row" }, [
          el("span", { class: "key", text: t("briefing.spokenTime") }),
          el("span", { class: "val num", text: spoken.time || "—" }),
        ]),
        el("div", { class: "row" }, [
          el("span", { class: "key", text: t("briefing.lastDelivered") }),
          el("span", { class: "val num", text: spoken.last_delivered || t("common.never") }),
        ]),
      ]),
      el("p", { class: "muted", text: t("briefing.spokenNote") }),
    );
  }

  return () => {};
}
