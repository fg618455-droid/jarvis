"""Inspect, probe, reset, and replace generic LLM route chains."""

from __future__ import annotations

import ipaddress
import threading
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from flask import Blueprint, Response, jsonify, request

from jarvis.config import _load_json, _save_json, load_settings, resolve_config_path
from jarvis.config_metadata import (
    LLM_ROUTE_FIELD_METADATA,
    LLM_ROUTE_PROVIDER_PLACEHOLDERS,
)
from jarvis.debug import debug_log
from jarvis.llm import Route, RoutedBackend, Tier, get_llm_runtime
from jarvis.llm.probe import probe_route
from jarvis.llm.route import _build_backend
from jarvis.llm.route_state import route_state_key
from jarvis.tools.builtin.ask_crew import AGENT_THREADS

from .settings import MASK, _mask

bp = Blueprint("llm_routes", __name__, url_prefix="/api/llm/routes")
_PROBE_LOCK = threading.Lock()
_ALLOWED_CHAT_OVERRIDES = {
    "auto", "openai_compatible", "claude_subscription",
    "codex_subscription", "crew_chat",
}


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
    generation = get_llm_runtime().install(settings)
    settings = generation.settings
    backend = generation.backend
    override = str(getattr(settings, "chat_backend_override", "auto") or "auto")
    crew_chat_agent = str(getattr(settings, "crew_chat_agent", "") or "")
    chains = (
        backend.route_status()
        if isinstance(backend, RoutedBackend)
        else {tier.value: [] for tier in Tier}
    )
    if isinstance(backend, RoutedBackend):
        for tier in Tier:
            for item, route in zip(chains[tier.value], backend.routes_for(tier)):
                item["base_url"] = _display_url(route.base_url)
                item["masked_key"] = _mask(route.api_key)
                item["local"] = RoutedBackend._is_local(route)
    configured_routes = []
    for index, route in enumerate(getattr(settings, "llm_routes", []) or []):
        if not isinstance(route, dict):
            continue
        exact_url = str(route.get("base_url", "") or "")
        display_url = _display_url(exact_url)
        key = str(route.get("api_key", "") or "")
        configured_routes.append({
            "_index": index,
            "name": str(route.get("name", "") or ""),
            "provider": str(route.get("provider", "") or ""),
            "base_url": display_url,
            "base_url_redacted": display_url != exact_url,
            "api_key": _mask(key),
            "api_key_env": str(route.get("api_key_env", "") or ""),
            "model": str(route.get("model", "") or ""),
            "tier": str(route.get("tier", "") or ""),
            "timeout_sec": float(route.get("timeout_sec", 4.0) or 4.0),
            "enabled": bool(route.get("enabled", True)),
            "capabilities": list(route.get("capabilities", [])),
        })
    runtime = get_llm_runtime().status()
    last_responding = next((
        item
        for tier in ("chat", "fast", "private")
        for item in chains.get(tier, [])
        if item.get("last_responded")
    ), None)
    return {
        "configured_routes": configured_routes,
        "effective_chains": chains,
        # Compatibility for older clients; the editor never reconstructs
        # configuration from this runtime-expanded shape.
        "chains": chains,
        "route_fields": [_route_field_payload(field) for field in LLM_ROUTE_FIELD_METADATA],
        "provider_placeholders": LLM_ROUTE_PROVIDER_PLACEHOLDERS,
        "chat_backend_override": override,
        "crew_chat_agent": crew_chat_agent,
        "runtime": runtime,
        "runtime_generation": generation.number,
        "last_responding_route": last_responding,
        "health": runtime.get("health", {}),
    }


def _route_field_payload(meta) -> dict[str, Any]:
    return {
        "key": meta.key,
        "label": meta.label,
        "description": meta.description,
        "type": meta.field_type,
        "choices": [
            {"value": value, "label": label}
            for value, label in (meta.choices or [])
        ] or None,
        "min": meta.min_val,
        "max": meta.max_val,
        "step": meta.step,
        "suffix": meta.suffix,
        "nullable": meta.nullable,
        "is_secret": meta.field_type == "password",
        "default": meta.default_value,
    }


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
        if provider not in (
            "openai_compatible", "claude_subscription",
            "codex_subscription", "crew_chat",
        ):
            raise ValueError(f"route {index + 1} has an unsupported protocol")
        existing_route = None
        try:
            source_index = int(raw.get("_index"))
        except (TypeError, ValueError):
            source_index = -1
        if 0 <= source_index < len(existing) and isinstance(existing[source_index], dict):
            existing_route = existing[source_index]
        if existing_route is None:
            existing_route = existing_by_identity.get((name, tier), {})
        if raw.get("base_url_redacted") and existing_route:
            prior_url = str(existing_route.get("base_url", "") or "")
            if base_url == _display_url(prior_url):
                base_url = prior_url
        inert = LLM_ROUTE_PROVIDER_PLACEHOLDERS.get(provider, {})
        if provider in {"claude_subscription", "codex_subscription", "crew_chat"}:
            base_url = base_url or str(inert.get("base_url", ""))
            model = model or str(inert.get("model", ""))
        if not name or not base_url or not model:
            raise ValueError(f"route {index + 1} needs name, base_url, and model")
        if tier not in ("fast", "chat"):
            raise ValueError(f"route {index + 1} has an unsupported tier")
        if provider in {"claude_subscription", "codex_subscription", "crew_chat"} and tier != "chat":
            raise ValueError(f"route {index + 1} provider is only supported for chat")
        if provider == "openai_compatible":
            try:
                parsed = urlsplit(base_url)
                host = (parsed.hostname or "").strip().lower()
            except ValueError as error:
                raise ValueError(
                    f"route {index + 1} has an invalid endpoint"
                ) from error
            try:
                address = ipaddress.ip_address(host) if host else None
            except ValueError:
                address = None
            if parsed.scheme != "https" or not host:
                raise ValueError(f"route {index + 1} needs a cloud HTTPS endpoint")
            if host == "localhost" or host.endswith(".local") or (
                address is not None and not address.is_global
            ):
                raise ValueError(f"route {index + 1} cannot use a local endpoint")
        try:
            timeout_sec = float(raw.get("timeout_sec", 4.0))
        except (TypeError, ValueError) as error:
            raise ValueError(f"route {index + 1} has an invalid timeout") from error
        if timeout_sec <= 0:
            raise ValueError(f"route {index + 1} has an invalid timeout")
        api_key = str(raw.get("api_key", raw.get("masked_key", "")) or "")
        if api_key.startswith(MASK):
            api_key = str((existing_route or {}).get("api_key", "") or "")
        api_key_env = str(raw.get("api_key_env", "") or "").strip()
        raw_capabilities = raw.get("capabilities", ["chat", "stream", "tools"])
        if not isinstance(raw_capabilities, list):
            raise ValueError(f"route {index + 1} has invalid capabilities")
        capabilities = list(dict.fromkeys(
            str(value).strip().lower() for value in raw_capabilities
            if str(value).strip().lower() in ("chat", "stream", "tools")
        ))
        clean.append({
            "name": name,
            "provider": provider,
            "base_url": base_url,
            "api_key": api_key,
            "api_key_env": api_key_env,
            "model": model,
            "tier": tier,
            "timeout_sec": timeout_sec,
            "enabled": bool(raw.get("enabled", True)),
            "capabilities": capabilities,
        })
    return clean


def _write_and_swap(path, previous: dict[str, Any], candidate: dict[str, Any]):
    """Persist, build and publish a generation, rolling the file back on failure."""
    candidate["_config_version"] = max(7, int(candidate.get("_config_version", 0) or 0))

    def prepare():
        if not _save_json(path, candidate):
            raise OSError("configuration write failed")
        return load_settings()

    def rollback() -> None:
        _save_json(path, previous)

    return get_llm_runtime().reconfigure(prepare, rollback)


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
    candidate = dict(config)
    candidate["llm_routes"] = clean
    try:
        generation = _write_and_swap(path, config, candidate)
    except Exception:
        return jsonify(error="could not activate route configuration"), 500
    debug_log(f"LLM route configuration written ({len(clean)} routes)", "webui")
    return jsonify({"written": len(clean), "generation": generation.number, **_payload()})


@bp.route("/chat-backend-override", methods=["PUT"])
def set_chat_backend_override() -> Response:
    """Force a specific Tier.CHAT route provider for every reply, or reset
    to "auto" (the default) so automatic per-turn classification and the
    configured chain order decide instead. Not validated against currently
    configured routes: a provider named here with no matching route is the
    same ordinary "unavailable, fall through to the normal chain" case
    RoutedBackend already handles at call time, not a config error."""
    body = request.get_json(silent=True) or {}
    value = str(body.get("chat_backend_override", "") or "").strip().lower() or "auto"
    if value not in _ALLOWED_CHAT_OVERRIDES:
        return jsonify(error="unsupported chat backend override"), 400
    path = resolve_config_path()
    config = _load_json(path) or {}
    candidate = dict(config)
    candidate["chat_backend_override"] = value
    try:
        _write_and_swap(path, config, candidate)
    except Exception:
        return jsonify(error="could not activate chat backend override"), 500
    debug_log(f"chat backend override set to {value!r}", "webui")
    return jsonify({"chat_backend_override": value, **_payload()})


@bp.route("/crew-chat-agent", methods=["PUT"])
def set_crew_chat_agent() -> Response:
    """Set which crew specialist answers a turn routed to the crew_chat
    backend, or clear it back to empty (the route then fails closed rather
    than guessing an agent, see llm.spec.md). Validated against the same
    fixed roster askCrew delegates to, since a name outside it can never
    answer either."""
    body = request.get_json(silent=True) or {}
    value = str(body.get("crew_chat_agent", "") or "").strip().lower()
    if value and value not in AGENT_THREADS:
        return jsonify(error=(
            f"Unknown crew agent '{value}'. Choose one of: "
            f"{', '.join(sorted(AGENT_THREADS))}."
        )), 400
    path = resolve_config_path()
    config = _load_json(path) or {}
    candidate = dict(config)
    candidate["crew_chat_agent"] = value
    try:
        _write_and_swap(path, config, candidate)
    except Exception:
        return jsonify(error="could not activate crew chat agent"), 500
    debug_log(f"crew chat agent set to {value!r}", "webui")
    return jsonify({"crew_chat_agent": value, **_payload()})


@bp.route("/reset", methods=["POST"])
def reset_routes() -> Response:
    """Clear all health state or the exact stable route identifier supplied."""
    body = request.get_json(silent=True) or {}
    route_id = str(body.get("route_id", "") or "").strip()
    generation = get_llm_runtime().install(load_settings())
    backend = generation.backend
    if not isinstance(backend, RoutedBackend):
        return jsonify(error="routing backend is unavailable"), 409
    selected = None
    if route_id:
        selected = next((route for route in backend.routes if route_state_key(route) == route_id), None)
        if selected is None:
            return jsonify(error="unknown route identifier"), 404
    backend.reset(selected)
    debug_log("LLM route health reset", "webui")
    return jsonify({"reset": True, "route_id": route_id or None, **_payload()})


@bp.route("/probe", methods=["POST"])
def probe_routes() -> Response:
    """Run secret-free capability probes after an explicit user request."""
    if not _PROBE_LOCK.acquire(blocking=False):
        return jsonify(error="a route probe is already running"), 409
    try:
        settings = load_settings()
        generation = get_llm_runtime().install(settings)
        backend = generation.backend
        results = []
        effective = {
            (route.name, route.tier.value): route
            for route in (backend.routes if isinstance(backend, RoutedBackend) else ())
            if route.tier is not Tier.PRIVATE
        }
        for index, raw in enumerate(getattr(settings, "llm_routes", []) or []):
            if not isinstance(raw, dict):
                continue
            try:
                tier = Tier(str(raw.get("tier", "")).strip().lower())
            except ValueError:
                continue
            if tier is Tier.PRIVATE:
                continue
            name = str(raw.get("name", "") or f"route-{index + 1}").strip()
            provider = str(raw.get("provider", "") or "").strip().lower()
            try:
                route = Route(
                    name=name,
                    provider=provider,
                    base_url=str(raw.get("base_url", "") or "").strip().rstrip("/"),
                    api_key=str(raw.get("api_key", "") or ""),
                    api_key_env=str(raw.get("api_key_env", "") or "").strip(),
                    model=str(raw.get("model", "") or "").strip(),
                    tier=tier,
                    timeout_sec=max(0.1, float(raw.get("timeout_sec", 4.0) or 4.0)),
                    enabled=bool(raw.get("enabled", True)),
                    capabilities=frozenset(
                        str(value).strip().lower()
                        for value in raw.get("capabilities", ["chat", "stream", "tools"])
                        if str(value).strip().lower() in {"chat", "stream", "tools"}
                    ),
                )
            except (TypeError, ValueError):
                continue
            effective_route = effective.get((name, tier.value))
            concrete = None
            if effective_route is not None and isinstance(backend, RoutedBackend):
                concrete = backend._backend(effective_route)
            # Missing credentials are detected before a backend is used. This
            # keeps inactive configured candidates visible in the probe result
            # without accidentally contacting them.
            results.append(probe_route(
                route,
                backend=concrete or _build_backend(route, settings),
            ))
        debug_log(f"LLM route probe completed ({len(results)} routes)", "webui")
        return jsonify({"results": results, "generation": generation.number})
    finally:
        _PROBE_LOCK.release()
