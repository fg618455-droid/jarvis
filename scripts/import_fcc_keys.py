"""Import available FCC credentials into Jarvis' generic LLM route list."""

from __future__ import annotations

from collections.abc import Mapping

from jarvis.config import _load_json, _save_json, resolve_config_path
from jarvis.llm.probe import configure_cli_output, load_fcc_values, probe_all
from jarvis.llm.route_catalogue import CHAT_ORDER, ENDPOINTS, FAST_ORDER


def _selected_model(endpoint, values: Mapping[str, str], models: list[str]) -> str:
    configured = str(values.get(endpoint.model_env, "") or "").strip()
    if configured and configured in models:
        return configured
    if endpoint.default_model and endpoint.default_model in models:
        return endpoint.default_model
    return models[0] if models else ""


def build_routes(values: Mapping[str, str], probe_results: list[dict]) -> list[dict]:
    endpoints = {endpoint.name: endpoint for endpoint in ENDPOINTS}
    results = {result["name"]: result for result in probe_results}
    routes: list[dict] = []
    for tier, order in (("fast", FAST_ORDER), ("chat", CHAT_ORDER)):
        for name in order:
            endpoint = endpoints[name]
            key = str(values.get(endpoint.key_env, "") or "").strip()
            probe = results.get(name, {})
            if not probe.get("ok") or not probe.get("import_candidate"):
                continue
            models = list(probe.get("models", []))
            model = _selected_model(endpoint, values, models)
            if not key or not model:
                continue
            routes.append({
                "name": name,
                "provider": "openai_compatible",
                "base_url": endpoint.base_url,
                "api_key": "",
                "api_key_env": endpoint.key_env,
                "model": model,
                "tier": tier,
                "timeout_sec": 8.0 if name == "groq" else 4.0,
                "enabled": True,
                "capabilities": ["chat", "stream", "tools"],
            })
    return routes


def main() -> int:
    configure_cli_output()
    values = load_fcc_values()
    if not values:
        print("⚠️ FCC environment not found", flush=True)
        return 1
    print("🔎 Checking configured FCC credentials", flush=True)
    results = probe_all(values)
    routes = build_routes(values, results)
    if not routes:
        print("⚠️ No working route with an advertised model was found", flush=True)
        return 1

    path = resolve_config_path()
    config = _load_json(path) or {}
    config["_config_version"] = max(7, int(config.get("_config_version", 0) or 0))
    config["llm_routes"] = routes
    if not _save_json(path, config):
        print("❌ Could not write Jarvis configuration", flush=True)
        return 1

    print("✅ FCC routes imported", flush=True)
    print(f"   Routes: {len(routes)}", flush=True)
    print("   Keys: ••••••••", flush=True)
    print(f"💾 Configuration: {path}", flush=True)
    print("   The running backend activates them on its next configuration reload", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
