/* Talking to the control centre.

   Every write carries the X-Jarvis-UI header the server demands, and the
   access token rides along when the page was opened with one. */

const params = new URLSearchParams(location.search);
const TOKEN = params.get("t") || "";

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

async function request(path, options = {}) {
  const headers = { "X-Jarvis-UI": "1", ...(options.headers || {}) };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  if (TOKEN) headers["X-Jarvis-Token"] = TOKEN;

  const response = await fetch(url(path), {
    ...options,
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
  });

  const text = await response.text();
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

  const response = await fetch(url(path), { method: "POST", headers });
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
    buffered += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffered.split("\n");
    buffered = lines.pop() || "";
    for (const line of lines) consume(line);
    if (done) break;
  }
  consume(buffered);
  return finalEvent;
}

export const api = {
  status: () => request("/api/status"),
  turns: (limit = 50) => request(`/api/turns?limit=${limit}`),
  conversation: (limit = 30) => request(`/api/conversation?limit=${limit}`),
  voiceStatus: () => request("/api/voice/status"),
  setConversationMode: (enabled) =>
    request("/api/conversation/mode", { method: "POST", body: { enabled } }),
  chat: (text, speak) => request("/api/chat", { method: "POST", body: { text, speak } }),
  passive: (date = "", limit = 500) =>
    request(
      `/api/passive?limit=${limit}${date ? `&date=${encodeURIComponent(date)}` : ""}`,
    ),
  setPassiveEnabled: (enabled) =>
    request("/api/passive/enabled", { method: "POST", body: { enabled } }),
  deletePassiveLine: (id) =>
    request(`/api/passive/${encodeURIComponent(id)}`, { method: "DELETE" }),
  deletePassiveDay: (date) =>
    request(`/api/passive?date=${encodeURIComponent(date)}`, { method: "DELETE" }),
  deletePassiveAll: () => request("/api/passive?all=1", { method: "DELETE" }),

  tools: () => request("/api/tools"),
  refreshTools: () => request("/api/tools/refresh", { method: "POST" }),

  security: () => request("/api/security"),
  decide: (requestId, approved) =>
    request("/api/security/decide", {
      method: "POST",
      body: { request_id: requestId, approved },
    }),

  system: () => request("/api/system"),
  restart: () => request("/api/system/restart", { method: "POST" }),
  health: () => request("/api/health"),
  logs: (limit = 200) => request(`/api/logs?limit=${limit}`),

  crew: (limit = 200) => request(`/api/crew?limit=${limit}`),
  crewChat: (agent, message) => request("/api/crew/chat", { method: "POST", body: { agent, message } }),

  llmRoutes: () => request("/api/llm/routes"),
  saveLlmRoutes: (routes) => request("/api/llm/routes", { method: "PUT", body: { routes } }),
  probeLlmRoutes: () => request("/api/llm/routes/probe", { method: "POST" }),
  resetLlmRoutes: () => request("/api/llm/routes/reset", { method: "POST" }),
  setChatBackendOverride: (chatBackendOverride) =>
    request("/api/llm/routes/chat-backend-override", { method: "PUT", body: { chat_backend_override: chatBackendOverride } }),
  setCrewChatAgent: (crewChatAgent) =>
    request("/api/llm/routes/crew-chat-agent", { method: "PUT", body: { crew_chat_agent: crewChatAgent } }),

  settings: () => request("/api/settings"),
  saveSettings: (changes) => request("/api/settings", { method: "PUT", body: { changes } }),

  graphTree: () => request("/api/graph/tree"),
  graphStats: () => request("/api/graph/stats"),
  graphNode: (id) => request(`/api/graph/node/${encodeURIComponent(id)}`),
  graphRecent: () => request("/api/graph/recent"),
  graphTop: () => request("/api/graph/top"),
  graphPresets: () => request("/api/graph/presets"),
  createNode: (node) => request("/api/graph/node", { method: "POST", body: node }),
  updateNode: (id, node) =>
    request(`/api/graph/node/${encodeURIComponent(id)}`, { method: "PUT", body: node }),
  deleteNode: (id) =>
    request(`/api/graph/node/${encodeURIComponent(id)}`, { method: "DELETE" }),

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
