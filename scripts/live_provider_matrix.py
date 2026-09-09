"""Opt-in synthetic provider probes; emits only capability metadata."""
from __future__ import annotations
import argparse
import json
import os
from jarvis.llm.probe import load_fcc_values, probe_route, classify_probe_error, _has_tool_call, _PROBE_TOOL
from jarvis.llm.route import Route, _build_backend
from jarvis.llm.route_catalogue import ENDPOINTS
from jarvis.llm.tiers import Tier
from types import SimpleNamespace
from jarvis.tools.registry import (generate_tools_json_schema, configure_vault_search_tool,
    configure_computer_interaction_tools, configure_system_management_tool)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('providers', nargs='+', choices=['mistral', 'groq', 'openrouter'])
    args = parser.parse_args()
    if os.environ.get('JARVIS_LIVE_ACCEPTANCE') != '1':
        parser.error('JARVIS_LIVE_ACCEPTANCE=1 is required')
    values = load_fcc_values()
    cfg = SimpleNamespace(obsidian_vault_path="synthetic-schema-only", computer_interaction_enabled=True,
                          system_management_enabled=True)
    configure_vault_search_tool(cfg)
    configure_computer_interaction_tools(cfg)
    configure_system_management_tool(cfg)
    schemas = generate_tools_json_schema()
    passed = True
    for name in args.providers:
        endpoint = next(e for e in ENDPOINTS if e.name == name)
        key = values.get(endpoint.key_env) or os.environ.get(endpoint.key_env, '')
        route = Route(name, 'openai_compatible', endpoint.base_url, key,
                      endpoint.default_model, Tier.FAST if name != 'openrouter' else Tier.CHAT, 30.0)
        backend = _build_backend(route)
        result = probe_route(route, backend=backend)
        result.pop('models', None)
        result['schema_acceptance'] = {'status': 'unavailable', 'count': len(schemas)}
        if result['ok'] and result['tools']['native']:
            try:
                response = backend.chat(route.model,
                    [{'role': 'user', 'content': 'Call probe_echo with value ok. This is a synthetic schema check.'}],
                    tools=[*schemas, _PROBE_TOOL], extra_options={'tool_choice': {'type': 'function', 'function': {'name': 'probe_echo'}}, 'max_tokens': 256}, timeout_sec=30.0)
                result['schema_acceptance']['status'] = 'passed' if _has_tool_call(response) else 'failed'
            except Exception as exc:
                result['schema_acceptance'].update(status='failed', error=classify_probe_error(exc))
        passed = passed and result['ok'] and result['schema_acceptance']['status'] == 'passed'
        print(json.dumps(result, sort_keys=True), flush=True)
    return 0 if passed else 1

if __name__ == '__main__':
    raise SystemExit(main())
