/* Hash routing.

   Views are modules with a mount(container) function that may return a
   cleanup function. Leaving a view runs its cleanup, so a view can poll or
   subscribe without leaking into the next one. */

export class Router {
  constructor(container, routes, fallback) {
    this.container = container;
    this.routes = routes;
    this.fallback = fallback;
    this._cleanup = null;
    this._current = null;
    this._listeners = new Set();
  }

  onChange(handler) {
    this._listeners.add(handler);
  }

  get current() {
    return this._current;
  }

  start() {
    window.addEventListener("hashchange", () => this._render());
    this._render();
  }

  go(name) {
    if (location.hash === `#/${name}`) this._render();
    else location.hash = `#/${name}`;
  }

  async _render() {
    const name = (location.hash.replace(/^#\//, "") || this.fallback).split("?")[0];
    const view = this.routes[name] ? name : this.fallback;

    if (this._cleanup) {
      try {
        this._cleanup();
      } catch (error) {
        console.error("view cleanup failed", error);
      }
      this._cleanup = null;
    }

    this._current = view;
    for (const handler of this._listeners) handler(view);

    while (this.container.firstChild) this.container.removeChild(this.container.firstChild);

    const root = document.createElement("div");
    root.className = "view";
    this.container.append(root);

    try {
      const module = await this.routes[view]();
      this._cleanup = (await module.mount(root)) || null;
    } catch (error) {
      console.error(`view ${view} failed`, error);
      const failure = document.createElement("div");
      failure.className = "empty";
      failure.textContent = String(error.message || error);
      root.append(failure);
    }
  }
}
