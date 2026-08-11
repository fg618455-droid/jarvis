/* The live stream.

   EventSource reconnects on its own, but only for a clean drop. A daemon
   that stops answering leaves the page silently stale, so the connection
   is watched and rebuilt, and the interface is told when it is on its own. */

import { eventsUrl } from "./api.js";

const RETRY_MS = 2000;

export class LiveStream {
  constructor() {
    this._handlers = new Map();
    this._source = null;
    this._retry = null;
    this.connected = false;
  }

  on(kind, handler) {
    if (!this._handlers.has(kind)) this._handlers.set(kind, new Set());
    this._handlers.get(kind).add(handler);
    return () => this._handlers.get(kind).delete(handler);
  }

  _emit(kind, data) {
    for (const handler of this._handlers.get(kind) || []) {
      try {
        handler(data);
      } catch (error) {
        console.error(`live handler failed for ${kind}`, error);
      }
    }
  }

  start() {
    this.stop();
    const source = new EventSource(eventsUrl());
    this._source = source;

    source.onopen = () => {
      this.connected = true;
      this._emit("connection", { connected: true });
    };

    source.onerror = () => {
      if (!this.connected) return this._scheduleRetry();
      this.connected = false;
      this._emit("connection", { connected: false });
      this._scheduleRetry();
    };

    for (const kind of [
      "status",
      "phase",
      "stage",
      "turn",
      "discarded",
      "error",
      "confirmation",
      "confirmation_resolved",
    ]) {
      source.addEventListener(kind, (event) => {
        let data = {};
        try {
          data = JSON.parse(event.data);
        } catch {
          data = {};
        }
        this._emit(kind, data);
      });
    }
  }

  _scheduleRetry() {
    if (this._retry) return;
    this._retry = setTimeout(() => {
      this._retry = null;
      this.start();
    }, RETRY_MS);
  }

  stop() {
    if (this._retry) {
      clearTimeout(this._retry);
      this._retry = null;
    }
    if (this._source) {
      this._source.close();
      this._source = null;
    }
  }
}

export const live = new LiveStream();
