"""Status polling must never spawn the Ollama CLI or follow redirects."""
from types import SimpleNamespace
from unittest.mock import MagicMock
import pytest
import requests
from jarvis.webui.api import system


@pytest.fixture
def client(monkeypatch):
    session = MagicMock()
    session.__enter__.return_value = session
    response = session.get.return_value.__enter__.return_value
    response.status_code = 200
    monkeypatch.setattr(system.requests, 'Session', lambda: session)
    monkeypatch.setattr(system, 'load_settings', lambda: SimpleNamespace(ollama_base_url='http://127.0.0.1:11435'))
    monkeypatch.setattr(system, '_run', lambda *_: pytest.fail('No CLI permitted'))
    return session, response


def test_residency_uses_bounded_loopback_http_and_existing_fields(client):
    session, response = client
    response.json.return_value = {'models': [{'name': 'synthetic', 'size': 4_000_000_000,
        'size_vram': 2_000_000_000, 'context_length': 4096, 'expires_at': 'synthetic-expiry'}]}
    assert system.read_loaded_models() == [{'name': 'synthetic', 'size': '4.0 GB',
        'processor': '50% CPU/50% GPU', 'context': '4096', 'until': 'synthetic-expiry'}]
    session.get.assert_called_once_with('http://127.0.0.1:11435/api/ps', timeout=4.0, allow_redirects=False)
    assert session.trust_env is False


@pytest.mark.parametrize('status', [302, 503])
def test_unavailable_or_redirect_has_no_resident_models(client, status):
    _, response = client
    response.status_code = status
    assert system.read_loaded_models() == []
    response.json.assert_not_called()


def test_timeout_returns_without_cli_fallback(client):
    session, _ = client
    session.get.side_effect = requests.Timeout()
    assert system.read_loaded_models() == []


@pytest.mark.parametrize('payload', [None, {'models': None}, {'models': [None, {'name':'bad','size':'x'}]}])
def test_malformed_payload_has_no_resident_models(client, payload):
    _, response = client
    response.json.return_value = payload
    assert system.read_loaded_models() == []
