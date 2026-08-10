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

export const api = {
  status: () => request("/api/status"),
  turns: (limit = 50) => request(`/api/turns?limit=${limit}`),
  conversation: (limit = 30) => request(`/api/conversation?limit=${limit}`),
  chat: (text, speak) => request("/api/chat", { method: "POST", body: { text, speak } }),

  tools: () => request("/api/tools"),
  refreshTools: () => request("/api/tools/refresh", { method: "POST" }),

  security: () => request("/api/security"),
  decide: (requestId, approved) =>
    request("/api/security/decide", {
      method: "POST",
      body: { request_id: requestId, approved },
    }),

  system: () => request("/api/system"),

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

  exportUrl: () => url("/api/turns/export.csv"),
};
