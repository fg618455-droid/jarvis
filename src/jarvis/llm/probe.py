"""Probe configured generic endpoints for their current model catalogues."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

import requests
from dotenv import dotenv_values

from .backend import ProviderError
from .openai_compatible import OpenAICompatibleBackend
from .route_catalogue import ENDPOINTS, EndpointTemplate


def configure_cli_output() -> None:
    """Make emoji CLI output reliable on legacy Windows code pages."""
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")


def fcc_env_path() -> Path:
    return Path.home() / ".fcc" / ".env"


def load_fcc_values(path: Path | None = None) -> dict[str, str]:
    source = path or fcc_env_path()
    if not source.exists():
        return {}
    return {
        str(key): str(value)
        for key, value in dotenv_values(source).items()
        if key and value is not None
    }


def probe_endpoint(endpoint: EndpointTemplate, values: Mapping[str, str]) -> dict:
    key = str(values.get(endpoint.key_env, "") or "").strip()
    if not key:
        return {"name": endpoint.name, "configured": False, "ok": False, "models": []}
    backend = OpenAICompatibleBackend(endpoint.base_url, api_key=key)
    try:
        models = backend.list_models(timeout_sec=8.0)
        return {
            "name": endpoint.name,
            "configured": True,
            "ok": bool(models),
            "models": models,
        }
    except (ProviderError, requests.exceptions.RequestException) as error:
        return {
            "name": endpoint.name,
            "configured": True,
            "ok": False,
            "models": [],
            "error": type(error).__name__,
        }


def probe_all(values: Mapping[str, str]) -> list[dict]:
    return [probe_endpoint(endpoint, values) for endpoint in ENDPOINTS]


def _save_catalogue(results: list[dict]) -> Path:
    path = Path.home() / ".jarvis" / "llm_probe.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".llm_probe.", suffix=".tmp")
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"providers": results}, handle, indent=2)
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except Exception:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise
    return path


def main() -> int:
    configure_cli_output()
    values = load_fcc_values()
    if not values:
        print("⚠️ FCC environment not found", flush=True)
        print(f"   Expected: {fcc_env_path()}", flush=True)
        return 1
    print("🔎 Probing configured OpenAI-compatible endpoints", flush=True)
    results = probe_all(values)
    for result in results:
        if not result["configured"]:
            print(f"⚪ {result['name']}", flush=True)
            print("   No key configured", flush=True)
        elif result["ok"]:
            print(f"✅ {result['name']}", flush=True)
            for model in result["models"]:
                print(f"   {model}", flush=True)
        else:
            print(f"❌ {result['name']}", flush=True)
            print(f"   {result.get('error', 'No models returned')}", flush=True)
    saved = _save_catalogue(results)
    print("💾 Probe catalogue saved", flush=True)
    print(f"   {saved}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
