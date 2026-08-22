/* Logs: the diagnostic entries this process has kept.

   An entry carries a time, a category, and a message. It carries no
   severity, so this view invents none: nothing here is coloured as though
   it were an error, because the ring has no way of knowing that it is.
   What it can offer is the two dimensions the entry actually has, which
   are the category it was logged under and the words in it.

   The ring holds the last 500 entries, so the log is read in full on every
   poll and narrowed on the page. Filtering never asks for anything. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { clear, el, empty } from "../ui.js";

const POLL_MS = 2000;
/* Long enough that a search reads as typing rather than as a filter firing
   on every keystroke, short enough that it never feels held back. */
const SETTLE_MS = 150;

export async function mount(root) {
  const state = {
    entries: [],
    category: "",
    search: "",
    /* Whether the newest entry is kept in view. It follows the reader
       rather than a button: scrolling back is how someone says they are
       reading rather than watching. */
    follow: true,
  };

  const shown = el("span", { class: "aside num log-shown" });
  const followButton = el("button", { class: "btn", type: "button" });
  const filters = el("div", { class: "log-filters" });
  const search = el("input", {
    type: "search",
    class: "log-search",
    placeholder: t("logs.search"),
    "aria-label": t("logs.search"),
  });
  const well = el("div", { class: "log-well well scroll" });

  const card = el("section", { class: "card logs" }, [
    el("header", {}, [
      el("h2", { text: t("logs.entries") }),
      shown,
      followButton,
    ]),
    el("div", { class: "log-controls" }, [filters, search]),
    well,
  ]);

  root.append(
    el("div", { class: "view-head" }, [
      el("h1", { text: t("logs.title") }),
      el("p", { text: t("logs.lead") }),
    ]),
    card,
  );

  function paintFollow() {
    followButton.className = state.follow ? "btn primary" : "btn";
    followButton.textContent = t("logs.follow");
    followButton.setAttribute("aria-pressed", state.follow ? "true" : "false");
  }

  followButton.addEventListener("click", () => {
    state.follow = !state.follow;
    paintFollow();
    if (state.follow) well.scrollTop = well.scrollHeight;
  });

  // Scrolling back stops the log moving under the reader; scrolling to the
  // end again resumes it. Neither needs the button.
  well.addEventListener("scroll", () => {
    const atEnd = well.scrollHeight - well.scrollTop - well.clientHeight < 40;
    if (atEnd === state.follow) return;
    state.follow = atEnd;
    paintFollow();
  });

  let settle = null;
  search.addEventListener("input", () => {
    clearTimeout(settle);
    settle = setTimeout(() => {
      state.search = search.value.trim().toLowerCase();
      paint();
    }, SETTLE_MS);
  });

  function choose(category) {
    state.category = category;
    paint();
  }

  function paint() {
    paintFilters(filters, state, choose);
    const visible = narrow(state);
    shown.textContent = t("logs.shown", {
      shown: visible.length,
      total: state.entries.length,
    });
    paintLines(well, visible, state);
  }

  async function refresh() {
    state.entries = (await api.logs(500)).entries || [];
    paint();
  }

  paintFollow();
  await refresh();

  const timer = setInterval(() => refresh().catch(() => {}), POLL_MS);
  return () => {
    clearInterval(timer);
    clearTimeout(settle);
  };
}

function narrow(state) {
  return state.entries.filter((entry) => {
    if (state.category && entry.category !== state.category) return false;
    if (state.search && !String(entry.message || "").toLowerCase().includes(state.search)) {
      return false;
    }
    return true;
  });
}

/* The categories offered are the ones the log actually contains, so the row
   never advertises a filter that would empty the view. */
function paintFilters(container, state, choose) {
  const categories = [...new Set(state.entries.map((entry) => entry.category || "debug"))].sort();
  clear(container);
  container.append(filterChip(t("logs.all"), !state.category, () => choose("")));
  for (const category of categories) {
    container.append(
      filterChip(category, state.category === category, () => choose(category)),
    );
  }
}

function filterChip(label, active, onclick) {
  return el("button", {
    class: `chip log-filter${active ? " accent" : ""}`,
    type: "button",
    "aria-pressed": active ? "true" : "false",
    text: label,
    onclick,
  });
}

function paintLines(container, entries, state) {
  clear(container);
  if (!entries.length) {
    container.append(empty(state.entries.length ? t("logs.noMatch") : t("logs.empty")));
    return;
  }

  for (const entry of entries) {
    container.append(
      el("div", { class: "log-line" }, [
        el("time", { class: "when num", text: fmt.time(entry.timestamp) }),
        el("span", { class: "log-category", text: entry.category || "debug" }),
        el("span", { class: "log-message", text: entry.message || "" }),
      ]),
    );
  }

  if (state.follow) container.scrollTop = container.scrollHeight;
}
