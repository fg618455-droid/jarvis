/* Mission Control: the agent crew running on the NAS.

   The crew lives outside this daemon, on a machine that is not always on.
   Every state the reading can be in — no endpoint configured, endpoint
   configured but not answering, or live data — gets its own plain message
   rather than a view that quietly shows nothing.

   What the crew logs is episodic: a line is written when an agent finishes
   something, and nothing at all says what an agent is doing right now. So
   this view does not pretend to watch work in progress. What it can show
   honestly is when each reading was taken, how long ago each thing
   happened, and which lines arrived while you were looking at the page.
   Those are the parts that are actually live, and they are the parts that
   move. */

import { api } from "../api.js";
import * as fmt from "../fmt.js";
import { t } from "../i18n.js";
import { live } from "../sse.js";
import { chip, clear, el, empty, toast } from "../ui.js";

/* Relative times are recomputed rather than repainted: rebuilding the feed
   every second would restart the arrival glow and throw away the scroll
   position for no gain. */
const TICK_MS = 1000;

const STATUS_TONE = { success: "ok", failure: "bad", partial: "warn" };
const STACK_ORDER = ["success", "partial", "failure"];

// Must match AGENT_THREADS in ask_crew.py — the roster the Telegram bridge
// and this chat panel both delegate to.
const CREW_AGENTS = ["jarvis", "dev", "research", "assistant", "schule", "scribe", "reach"];

const state = {
  payload: null,
  agent: "",
  failuresOnly: false,
  /* Entry ids that have been on screen at least once. A line only glows on
     the reading that first brought it in, never again on the next one. */
  seen: new Set(),
  rerender: () => {},
};

/* Nodes showing a relative time, the moment each one is relative to, and
   how that moment should read. */
let ticking = [];

function tick(node, epochSeconds, format) {
  const render = format || ((seconds) => fmt.ago(seconds, t));
  node.textContent = render(epochSeconds);
  ticking.push([node, epochSeconds, render]);
  return node;
}

function secondsOf(isoTimestamp) {
  const parsed = Date.parse(isoTimestamp || "");
  return Number.isNaN(parsed) ? 0 : parsed / 1000;
}

export async function mount(root) {
  state.payload = null;
  state.agent = "";
  state.failuresOnly = false;
  state.seen = new Set();

  const pill = el("span", { class: "state-pill" });
  // The chat panel is built once and left alone by re-renders below —
  // rebuilding it on every reload would drop whatever the user was
  // mid-typing and wipe the conversation so far.
  const chatCard = el("section", { class: "card" });
  const body = el("div", { class: "crew" });

  root.append(
    el("div", { class: "view-head" }, [
      el("h1", { text: t("crew.title") }),
      el("p", { text: t("crew.lead") }),
      el("div", { class: "actions" }, [
        pill,
        el("button", {
          class: "btn",
          type: "button",
          text: t("crew.refresh"),
          onclick: () => reload(),
        }),
      ]),
    ]),
    chatCard,
    body,
  );

  const chat = buildChatPanel(chatCard);

  function render() {
    ticking = [];
    paintState(pill, state.payload);
    chat.setAvailable(Boolean(state.payload?.configured && state.payload?.reachable));
    paint(body, state);
  }

  state.rerender = render;

  async function reload() {
    try {
      state.payload = await api.crew();
    } catch {
      // The daemon, not the NAS, is the one not answering. Either way the
      // view has nothing to show and says so rather than keeping a stale
      // reading on screen without saying how stale it is.
      state.payload = {
        configured: false, reachable: false, checked_at: Date.now() / 1000,
        entries: [], agents: [], daily: [],
      };
    }
    render();
  }

  // The daemon takes the reading for every open page and pushes it here, so
  // this view has no timer of its own beyond the one that ages what it has.
  const off = live.on("crew", (payload) => {
    state.payload = payload;
    render();
  });

  await reload();

  const timer = setInterval(() => {
    for (const [node, at, render] of ticking) node.textContent = render(at);
  }, TICK_MS);

  return () => {
    off();
    clearInterval(timer);
  };
}

/* ── The chat panel ─────────────────────────────────────────────────── */

function buildChatPanel(container) {
  container.append(el("header", {}, [el("h2", { text: t("crew.chatTitle") })]));

  const agentSelect = el(
    "select",
    {},
    CREW_AGENTS.map((agent) => el("option", { value: agent, text: agent })),
  );
  const input = el("input", { type: "text", placeholder: t("crew.chatPlaceholder") });
  const send = el("button", { class: "btn primary", type: "button", text: t("crew.chatSend") });
  const log = el("div", { class: "rows crew-chat-log" });

  function appendLine(agent, speaker, text) {
    log.append(
      el("div", { class: "row" }, [
        el("span", { class: "key", text: `${agent} · ${speaker}` }),
        el("span", { class: "val", text }),
      ]),
    );
    log.scrollTop = log.scrollHeight;
  }

  async function submit() {
    const message = input.value.trim();
    if (!message || send.disabled) return;
    const agent = agentSelect.value;

    appendLine(agent, t("crew.chatYou"), message);
    input.value = "";
    send.disabled = true;
    const previous = send.textContent;
    send.textContent = t("crew.chatSending");
    try {
      const result = await api.crewChat(agent, message);
      appendLine(
        agent, agent,
        result.reachable === false ? (result.error || t("crew.unreachable")) : (result.reply || "—"),
      );
    } catch (error) {
      toast(error.message, "bad");
    } finally {
      send.disabled = false;
      send.textContent = previous;
      input.focus();
    }
  }

  send.addEventListener("click", submit);
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  });

  container.append(el("div", { class: "composer-row" }, [agentSelect, input, send]), log);

  return {
    setAvailable(available) {
      agentSelect.disabled = !available;
      input.disabled = !available;
      send.disabled = !available;
    },
  };
}

/* ── The reading itself ─────────────────────────────────────────────── */

/* Freshness is reported beside the state, never folded into it: a tally is
   only as true as the moment it was read, and a view that shows a status
   without its age invites trust it has not earned. */
function paintState(pill, payload) {
  clear(pill);
  if (!payload) return;

  const live_ = payload.configured && payload.reachable;
  const label = !payload.configured
    ? t("crew.state.unconfigured")
    : payload.reachable
      ? t("crew.state.live")
      : t("crew.state.offline");

  pill.className = `state-pill ${live_ ? "live" : "down"}`;
  pill.append(
    el("span", { class: "state-pill-dot" }),
    el("span", { text: label }),
  );

  if (payload.checked_at) {
    const when = el("span", { class: "state-pill-age" });
    tick(when, payload.checked_at, (seconds) =>
      t("crew.checked", { when: fmt.ago(seconds, t) }),
    );
    pill.append(el("span", { class: "state-pill-note" }, [when]));
  }
}

function paint(container, current) {
  clear(container);
  const payload = current.payload;
  if (!payload) return;

  if (!payload.configured) {
    container.append(el("section", { class: "card" }, [empty(t("crew.notConfigured"))]));
    return;
  }
  if (!payload.reachable) {
    container.append(el("section", { class: "card" }, [empty(t("crew.unreachable"))]));
    return;
  }

  container.append(
    summaryCard(payload),
    rosterCard(payload, current),
    activityCard(payload, current),
  );
}

/* ── Summary and the activity ribbon ────────────────────────────────── */

function totals(daily) {
  const sum = (key) => daily.reduce((running, day) => running + (day[key] || 0), 0);
  return {
    count: sum("count"),
    success: sum("success"),
    partial: sum("partial"),
    failure: sum("failure"),
  };
}

function summaryCard(payload) {
  const daily = payload.daily || [];
  const tally = totals(daily);
  const rated = tally.success + tally.partial + tally.failure;
  const newest = (payload.entries || [])[0];

  const last = el("span", { class: "stat-value small" });
  if (newest) tick(last, secondsOf(newest.created_at));
  else last.textContent = "—";

  const agents = payload.agents || [];
  const working = agents.filter((agent) => agent.total > 0).length;

  return el("section", { class: "card" }, [
    el("header", {}, [el("h2", { text: t("crew.dailyActivity") })]),
    el("div", { class: "crew-summary" }, [
      // How much of the crew is actually working, not just how big it is.
      readout(t("crew.agents"), `${working} / ${agents.length}`),
      readout(t("crew.summary.tasks"), fmt.number(tally.count)),
      readout(
        t("crew.summary.success"),
        rated ? fmt.percent((tally.success / rated) * 100) : "—",
      ),
      el("div", { class: "stat" }, [
        el("span", { class: "stat-label", text: t("crew.summary.last") }),
        last,
      ]),
    ]),
    ribbon(daily),
  ]);
}

function readout(label, value) {
  return el("div", { class: "stat" }, [
    el("span", { class: "stat-label", text: label }),
    el("span", { class: "stat-value small", text: value }),
  ]);
}

/* Fourteen days as stacked bars rather than one shade per day: a busy day
   and a day of failures are not the same reading, and a single tone cannot
   tell them apart. */
function ribbon(daily) {
  if (!daily.length) return empty(t("crew.empty"));

  const tallest = Math.max(...daily.map((day) => day.count || 0), 1);

  return el(
    "div",
    { class: "ribbon" },
    daily.map((day, index) => {
      const track = el("div", { class: "ribbon-track" });
      if (day.count) {
        for (const status of STACK_ORDER) {
          const held = day[status] || 0;
          if (!held) continue;
          track.append(
            el("span", {
              class: `ribbon-seg ${STATUS_TONE[status]}`,
              style: `height: ${((held / tallest) * 100).toFixed(1)}%`,
            }),
          );
        }
      } else {
        track.append(el("span", { class: "ribbon-seg none" }));
      }

      return el(
        "div",
        {
          class: `ribbon-day${index === daily.length - 1 ? " today" : ""}`,
          title: t("crew.dayBreakdown", {
            date: fmt.date(day.date),
            count: day.count || 0,
            success: day.success || 0,
            partial: day.partial || 0,
            failure: day.failure || 0,
          }),
        },
        [track, el("span", { class: "ribbon-label", text: dayNumber(day.date) })],
      );
    }),
  );
}

/* The day of the month, which reads the same in every language. */
function dayNumber(isoDate) {
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  return Number.isNaN(parsed.getTime()) ? "" : String(parsed.getUTCDate());
}

/* ── The roster ─────────────────────────────────────────────────────── */

function rosterCard(payload, current) {
  const agents = payload.agents || [];
  return el("section", { class: "card" }, [
    el("header", {}, [
      el("h2", { text: t("crew.agents") }),
      current.agent
        ? el("button", {
            class: "btn crew-clear",
            type: "button",
            text: t("crew.filterAll"),
            onclick: () => setAgent(""),
          })
        : null,
    ]),
    agents.length
      ? el("div", { class: "crew-roster" }, agents.map((agent) => agentCard(agent, current)))
      : empty(t("crew.empty")),
  ]);
}

/* An agent with nothing in the window is shown as quiet, not dropped. The
   log is written on completion, so silence is a reading in its own right
   and hiding it would make a crew of seven look like a crew of one. */
function agentCard(agent, current) {
  const worked = agent.total > 0;
  const tone = worked ? STATUS_TONE[agent.last_status] || "" : "";
  const selected = current.agent === agent.name;

  const when = el("span", { class: "agent-when" });
  if (worked && agent.last_at) tick(when, secondsOf(agent.last_at));
  else when.textContent = t("crew.quiet");

  const rated = agent.success + agent.partial + agent.failure;

  return el(
    "button",
    {
      class: `agent${worked ? "" : " quiet"}`,
      type: "button",
      "aria-pressed": selected ? "true" : "false",
      "aria-label": t("crew.filterOne", { name: agent.name }),
      onclick: () => setAgent(selected ? "" : agent.name),
    },
    [
      el("span", { class: "agent-head" }, [
        el("span", { class: `agent-dot ${tone || "quiet"}` }),
        el("span", { class: "agent-name", text: agent.name }),
        when,
      ]),
      agentSpark(agent.daily || []),
      worked
        ? el("span", { class: "agent-foot" }, [
            el("span", {
              class: "agent-count",
              text: `${t("crew.tasks", { n: agent.total })} · ${t("crew.okShare", {
                percent: rated ? fmt.percent((agent.success / rated) * 100) : "—",
              })}`,
            }),
            successBar(agent, rated),
          ])
        : el("span", { class: "agent-foot" }, [
            el("span", { class: "agent-count muted", text: t("crew.noActivity") }),
          ]),
    ],
  );
}

/* The share that succeeded. Built from spans rather than the shared meter
   because this one lives inside a button, whose content model has no room
   for a division. */
function successBar(agent, rated) {
  const share = rated ? agent.success / rated : 0;
  return el("span", { class: "agent-bar" }, [
    el("span", {
      class: agent.failure > agent.success ? "bad" : "",
      style: `width: ${(share * 100).toFixed(1)}%`,
    }),
  ]);
}

/* The same fourteen days as the ribbon above, at a glance and per agent, so
   a rhythm is visible without reading three counters. */
function agentSpark(counts) {
  const tallest = Math.max(...counts, 1);
  return el(
    "span",
    { class: "agent-spark" },
    counts.map((count) =>
      el("span", {
        class: `agent-spark-bar${count ? "" : " none"}`,
        style: count ? `height: ${Math.max(12, (count / tallest) * 100).toFixed(1)}%` : null,
      }),
    ),
  );
}

function setAgent(name) {
  state.agent = name;
  state.rerender();
}

/* ── The activity feed ──────────────────────────────────────────────── */

function visible(entries, current) {
  return entries.filter((entry) => {
    if (current.agent && entry.agent_name !== current.agent) return false;
    if (current.failuresOnly && entry.status !== "failure") return false;
    return true;
  });
}

function activityCard(payload, current) {
  const entries = payload.entries || [];
  const shown = visible(entries, current);

  const card = el("section", { class: "card" }, [
    el("header", {}, [
      el("h2", { text: t("crew.activity") }),
      el("span", {
        class: "aside num",
        text: t("crew.shown", { shown: shown.length, total: entries.length }),
      }),
    ]),
    el("div", { class: "crew-filters" }, [
      filterChip(t("crew.filterAll"), !current.agent && !current.failuresOnly, () => {
        state.failuresOnly = false;
        setAgent("");
      }),
      current.agent ? filterChip(current.agent, true, () => setAgent("")) : null,
      filterChip(t("crew.filterFailures"), current.failuresOnly, () => {
        state.failuresOnly = !state.failuresOnly;
        setAgent(state.agent);
      }),
    ]),
  ]);

  if (!entries.length) card.append(empty(t("crew.empty")));
  else if (!shown.length) card.append(empty(t("crew.noMatch")));
  else card.append(feed(shown));

  // Every id is marked seen once painted, including the ones a filter is
  // hiding: an entry that arrived while filtered out is not news later.
  for (const entry of entries) state.seen.add(entry.id);
  return card;
}

function filterChip(label, active, onclick) {
  return el("button", {
    class: `chip crew-filter${active ? " accent" : ""}`,
    type: "button",
    "aria-pressed": active ? "true" : "false",
    text: label,
    onclick,
  });
}

function feed(entries) {
  // A first load is not an arrival: everything would glow at once and the
  // signal would mean nothing.
  const firstLoad = state.seen.size === 0;
  const list = el("div", { class: "feed well scroll" });

  let currentDay = null;
  for (const entry of entries) {
    const at = secondsOf(entry.created_at);
    const day = (entry.created_at || "").slice(0, 10);
    if (day !== currentDay) {
      currentDay = day;
      list.append(el("div", { class: "feed-day", text: fmt.date(day) }));
    }

    const isNew = !firstLoad && !state.seen.has(entry.id);
    list.append(
      el("div", { class: `feed-row${isNew ? " arrived" : ""}` }, [
        el("span", { class: `feed-dot ${STATUS_TONE[entry.status] || ""}` }),
        el("div", { class: "feed-body" }, [
          el("div", { class: "feed-head" }, [
            el("span", { class: "feed-when num", text: fmt.time(at) }),
            el("span", { class: "feed-agent", text: entry.agent_name || "?" }),
            chip(t(`crew.status.${entry.status}`) || entry.status, STATUS_TONE[entry.status]),
            el("span", { class: "feed-model", text: entry.model_used || "—" }),
          ]),
          el("p", { class: "feed-task", text: entry.task_description || "" }),
        ]),
      ]),
    );
  }
  return list;
}
