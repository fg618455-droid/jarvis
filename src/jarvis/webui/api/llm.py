"""Inspect, probe, reset, and replace generic LLM route chains."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

import requests
from flask import Blueprint, Response, jsonify, request

from jarvis.config import _load_json, _save_json, load_settings, resolve_config_path
from jarvis.debug import debug_log
from jarvis.llm import ProviderError, RoutedBackend, Tier, get_llm_backend
from jarvis.llm.route_state import RouteStateStore

from .settings import MASK, _mask

bp = Blueprint("llm_routes", __name__, url_prefix="/api/llm/routes")


def _display_url(value: str) -> str:
    """Return an endpoint URL without user-info, query values, or fragments."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if parsed.port is not None:
            host = f"{host}:{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except (TypeError, ValueError):
        return ""


def _payload() -> dict[str, Any]:
    settings = load_settings()
    backend = get_llm_backend(settings)
    if not isinstance(backend, RoutedBackend):
        return {"chains": {tier.value: [] for tier in Tier}}
    chains = backend.route_status()
    for tier in Tier:
        for item, route in zip(chains[tier.value], backend.routes_for(tier)):
            item["base_url"] = _display_url(route.base_url)
            item["masked_key"] = _mask(route.api_key)
    return {"chains": chains}


@bp.route("")
def routes() -> Response:
    """Return route health without contacting any configured endpoint."""
    return jsonify(_payload())


def _normalise_routes(raw_routes: Any, existing: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(raw_routes, list):
        raise TypeError("routes must be a list")
    existing_by_identity = {
        (str(route.get("name", "")), str(route.get("tier", ""))): route
        for route in existing if isinstance(route, dict)
    }
    clean: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_routes):
        if not isinstance(raw, dict):
            raise TypeError(f"route {index + 1} must be an object")
        name = str(raw.get("name", "") or "").strip()
        provider = str(raw.get("provider", "") or "").strip().lower()
        base_url = str(raw.get("base_url", "") or "").strip().rstrip("/")
        model = str(raw.get("model", "") or "").strip()
        tier = str(raw.get("tier", "") or "").strip().lower()
        if not name or not base_url or not model:
            raise ValueError(f"route {index + 1} needs name, base_url, and model")
        if provider not in ("ollama", "openai_compatible"):
            raise ValueError(f"route {index + 1} has an unsupported protocol")
        if tier not in ("fast", "chat"):
            raise ValueError(f"route {index + 1} has an unsupported tier")
        try:
            timeout_sec = float(raw.get("timeout_sec", 4.0))
        except (TypeError, ValueError) as error:
            raise ValueError(f"route {index + 1} has an invalid timeout") from error
        if timeout_sec <= 0:
            raise ValueError(f"route {index + 1} has an invalid timeout")
        api_key = str(raw.get("api_key", "") or "")
        if api_key.startswith(MASK):
            api_key = str(existing_by_identity.get((name, tier), {}).get("api_key", "") or "")
        clean.append({
            "name": name,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "model": model,
            "tier": tier,
            "timeout_sec": timeout_sec,
        })
    return clean


@bp.route("", methods=["PUT"])
def replace_routes() -> Response:
    """Replace configured chains while preserving unchanged masked keys."""
    body = request.get_json(silent=True) or {}
    path = resolve_config_path()
    config = _load_json(path) or {}
    existing = config.get("llm_routes", [])
    try:
        clean = _normalise_routes(body.get("routes"), existing if isinstance(existing, list) else [])
    except (TypeError, ValueError) as error:
        return jsonify(error=str(error)), 400
    config["llm_routes"] = clean
    if not _save_json(path, config):
        return jsonify(error="could not write route configuration"), 500
    debug_log(f"LLM route configuration written ({len(clean)} routes)", "webui")
    return jsonify({"written": len(clean), **_payload()})


@bp.route("/reset", methods=["POST"])
def reset_routes() -> Response:
    """Clear persisted cooldowns for all configured routes."""
    RouteStateStore().reset()
    debug_log("LLM route cooldowns reset", "webui")
    return jsonify({"reset": True, **_payload()})


@bp.route("/probe", methods=["POST"])
def probe_routes() -> Response:
    """Contact configured endpoints only after the user requests a probe."""
    settings = load_settings()
    backend = get_llm_backend(settings)
    results = []
    if isinstance(backend, RoutedBackend):
        for route in backend.routes:
            if route.provider == "ollama" and route.name.startswith("local-"):
                continue
            try:
                models = backend._backend(route).list_models(timeout_sec=route.timeout_sec)
                results.append({
                    "name": route.name,
                    "tier": route.tier.value,
                    "ok": bool(models),
                    "models": models,
                })
            except (ProviderError, requests.exceptions.RequestException) as error:
                results.append({
                    "name": route.name,
                    "tier": route.tier.value,
                    "ok": False,
                    "models": [],
                    "error": type(error).__name__,
                })
    debug_log(f"LLM route probe completed ({len(results)} routes)", "webui")
    return jsonify({"results": results})
