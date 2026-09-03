/* Talking to the control centre.

   Every write carries the X-Jarvis-UI header the server demands, and the
   access token rides along when the page was opened with one. */

import { t } from "./i18n.js";

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";

/* How long a request waits before it stops waiting.

   A connection that is refused fails at once and a server that answers with
   an error fails with a reason, and the page already has somewhere to put
   both. A socket that is accepted and then never answered does neither: the
   promise simply never settles, so a view awaiting one never finishes
   mounting and the panel it was opened for keeps announcing itself as busy
   for as long as the tab is left open.

   One bound for all of it would have to be long enough for the longest
   honest wait, which would make it useless for the rest. Which bound a call
   gets therefore follows from whether anything will ask again. A reading is
   retaken, by the deck's snapshot or by opening the panel again, so giving
   up on one costs nothing and an answer that arrives long after it was asked
   for is not worth having. Work the user asked for once — a turn, a probe, a
   briefing, a save, a restart — is asked for by nobody else, and cutting it
   short would report a failure for something that was still running, or, for
   a write, for something that has already landed on disk. So it waits far
   longer, and stops only because a wedged connection must not be permanent. */
export const DEADLINES = {
  reading: 10000,
  work: 180000,
};

/* A fetch that cannot outlive its deadline.

   `AbortSignal.timeout` says this in one line but is counted by the browser
   rather than by `setTimeout`, which puts it out of reach of anything that
   controls time on the page — including a test, which would then have to
   spend the deadline in real seconds to see it fire. */
function guardedBy(ms) {
  const controller = new AbortController();
  let timer = setTimeout(() => controller.abort(), ms);
  return {
    signal: controller.signal,
    /* Start the count again. Used by a stream, which is bounded by silence
       rather than by duration: one that is still arriving has not gone
       quiet, however long it has been running. */
    heard() {
      clearTimeout(timer);
      timer = setTimeout(() => controller.abort(), ms);
    },
    done() {
      clearTimeout(timer);
    },
  };
}

/* Why the wait ended, in words, because this is what the panel shows in place
   of the view and what the dock shows in place of a reply. "Aborted" is the
   mechanism rather than the fact, and the fact is that nothing came back. */
function silence(ms) {
  const error = new Error(t("api.noAnswer", { n: Math.round(ms / 1000) }));
  error.timedOut = true;
  return error;
}

function url(path) {
  if (!TOKEN) return path;
  return path + (path.includes("?") ? "&" : "?") + `t=${encodeURIComponent(TOKEN)}`;
}

export function eventsUrl() {
  return url("/api/events");
}

export function token() {
  return TOKEN;
}

async function request(path, { deadline = DEADLINES.reading, ...options } = {}) {
  const headers = { "X-Jarvis-UI": "1", ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (TOKEN) headers["X-Jarvis-Token"] = TOKEN;

  const guard = guardedBy(deadline);
  let response;
  let text;
  try {
    response = await fetch(url(path), {
      ...options,
      headers,
      signal: guard.signal,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    // The deadline covers the body as well as the headers: a server that
    // sends a status and then stops writing has stopped answering just as
    // completely as one that never sent anything.
    text = await response.text();
  } catch (error) {
    throw guard.signal.aborted ? silence(deadline) : error;
  } finally {
    guard.done();
  }

  let payload = null;
  try {
    payload = text ? JSON.parse(text) : null;
  } catch {
    payload = { raw: text };
  }

  if (!response.ok) {
    const error = new Error(payload?.error || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return payload;
}

async function streamRequest(path, onEvent) {
  const headers = { "X-Jarvis-UI": "1" };
  if (TOKEN) headers["X-Jarvis-Token"] = TOKEN;

  /* Importing a whole vault legitimately runs for minutes and says so line by
     line, so a stream is bounded by how long it has been quiet rather than by
     how long it has been going. Every line restarts the count; the bound is
     only reached by a stream that has stopped saying anything at all. */
  const guard = guardedBy(DEADLINES.work);
  try {
    return await consumeStream(path, headers, guard, onEvent);
  } catch (error) {
    throw guard.signal.aborted ? silence(DEADLINES.work) : error;
  } finally {
    guard.done();
  }
}

async function consumeStream(path, headers, guard, onEvent) {
  const response = await fetch(url(path), {
    method: "POST",
    headers,
    signal: guard.signal,
  });
  if (!response.ok) {
    const text = await response.text();
    let payload = null;
    try {
      payload = text ? JSON.parse(text) : null;
    } catch {
      payload = null;
    }
    const error = new Error(payload?.error || `${response.status} ${response.statusText}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = "";
  let finalEvent = null;

  function consume(line) {
    if (!line.trim()) return;
    const event = JSON.parse(line);
    finalEvent = event;
    if (onEvent) onEvent(event);
  }

  while (true) {
    const { value, done } = await reader.read();
    guard.heard();
    buffered += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffered.split("\n");
    buffered = lines.pop() || "";
    for (const line of lines) consume(line);
    if (done) break;
  }
  consume(buffered);
  return finalEvent;
}

/* A call that does something rather than reports something, and so waits on
   the longer of the two bounds. Every write is one: cutting a write short
   would report a failure for a change that may already be on disk, and the
   reader would go and make it again. */
function work(path, options = {}) {
  return request(path, { ...options, deadline: DEADLINES.work });
}

export const api = {
  status: () => request("/api/status"),
  turns: (limit = 50) => request(`/api/turns?limit=${limit}`),
  conversation: (limit = 30) => request(`/api/conversation?limit=${limit}`),
  voiceStatus: () => request("/api/voice/status"),
  setConversationMode: (enabled) =>
    work("/api/conversation/mode", { method: "POST", body: { enabled } }),
  chat: (text, speak) => work("/api/chat", { method: "POST", body: { text, speak } }),
  passive: (date = "", limit = 500) =>
    request(
      `/api/passive?limit=${limit}${date ? `&date=${encodeURIComponent(date)}` : ""}`,
    ),
  setPassiveEnabled: (enabled) =>
    work("/api/passive/enabled", { method: "POST", body: { enabled } }),
  deletePassiveLine: (id) =>
    work(`/api/passive/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deletePassiveDay: (date) =>
    work(`/api/passive?date=${encodeURIComponent(date)}`, { method: "DELETE" }),
  deletePassiveAll: () => work("/api/passive?all=1", { method: "DELETE" }),

  tools: () => request("/api/tools"),
  refreshTools: () => work("/api/tools/refresh", { method: "POST" }),

  visualizerState: () => request("/api/visualizer/state"),

  mcpServers: () => request("/api/mcp/servers"),
  saveMcpServers: (servers) =>
    work("/api/mcp/servers", { method: "PUT", body: { servers } }),

  // The prose is generated only when it is asked for, and kept for the day:
  // reading it back is a reading, and asking for it is the work.
  briefing: () => request("/api/briefing"),
  refreshBriefing: () => work("/api/briefing/refresh", { method: "POST" }),

  security: () => request("/api/security"),
  decide: (requestId, approved) =>
    work("/api/security/decide", {
      method: "POST",
      body: { request_id: requestId, approved },
    }),

  system: () => request("/api/system"),
  restart: () => work("/api/system/restart", { method: "POST" }),
  health: () => request("/api/health"),
  logs: (limit = 200) => request(`/api/logs?limit=${limit}`),

  crew: (limit = 200) => request(`/api/crew?limit=${limit}`),
  crewChat: (agent, message) => work("/api/crew/chat", { method: "POST", body: { agent, message } }),

  llmRoutes: () => request("/api/llm/routes"),
  saveLlmRoutes: (routes) => work("/api/llm/routes", { method: "PUT", body: { routes } }),
  probeLlmRoutes: () => work("/api/llm/routes/probe", { method: "POST" }),
  resetLlmRoutes: () => work("/api/llm/routes/reset", { method: "POST" }),
  setChatBackendOverride: (chatBackendOverride) =>
    work("/api/llm/routes/chat-backend-override", { method: "PUT", body: { chat_backend_override: chatBackendOverride } }),
  setCrewChatAgent: (crewChatAgent) =>
    work("/api/llm/routes/crew-chat-agent", { method: "PUT", body: { crew_chat_agent: crewChatAgent } }),

  settings: () => request("/api/settings"),
  saveSettings: (changes) => work("/api/settings", { method: "PUT", body: { changes } }),

  graphTree: () => request("/api/graph/tree"),
  graphStats: () => request("/api/graph/stats"),
  graphNode: (id) => request(`/api/graph/node/${encodeURIComponent(id)}`),
  graphRecent: () => request("/api/graph/recent"),
  graphTop: () => request("/api/graph/top"),
  graphPresets: () => request("/api/graph/presets"),
  createNode: (node) => work("/api/graph/node", { method: "POST", body: node }),
  updateNode: (id, node) =>
    work(`/api/graph/node/${encodeURIComponent(id)}`, { method: "PUT", body: node }),
  deleteNode: (id) =>
    work(`/api/graph/node/${encodeURIComponent(id)}`, { method: "DELETE" }),

  memories: (query) => request(`/api/memories${query ? `?search=${encodeURIComponent(query)}` : ""}`),
  memoryStats: () => request("/api/stats"),
  meals: () => request("/api/meals"),
  topics: () => request("/api/topics"),
  importDiary: (onEvent) => streamRequest("/api/graph/import-diary", onEvent),
  consolidateAll: (onEvent) => streamRequest("/api/graph/consolidate-all", onEvent),
  scrubDeflections: (onEvent) => streamRequest("/api/diary/scrub-deflections", onEvent),
  optimiseTopics: (onEvent) => streamRequest("/api/diary/optimise-topics", onEvent),

  exportUrl: () => url("/api/turns/export.csv"),
};
