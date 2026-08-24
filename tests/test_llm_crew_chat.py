"""Behaviour tests for :class:`CrewChatBackend`.

This backend relays Tier.CHAT turns to the Hermes crew's own chat engine on
Felix's NAS, reusing the exact wire shape ``webui/api/crew.py``'s
``crew_chat()`` already uses. The load-bearing assertions here pin the exact
HTTP request made (mirroring how ``test_open_on_computer.py`` asserts the
exact ``Popen`` vector rather than "it succeeded"), the exact typed failure
raised for every HTTP failure shape, and the fail-closed behaviour when the
endpoint or agent is unconfigured.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from jarvis.llm import (
    AuthError,
    ModelUnavailableError,
    ProviderError,
    RateLimitedError,
    ToolsNotSupportedError,
)
from jarvis.llm.crew_chat import CrewChatBackend


def _response(status_code=200, json_body=None, headers=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.json.return_value = json_body if json_body is not None else {}
    if status_code >= 400:
        error = requests.exceptions.HTTPError(response=resp)
        resp.raise_for_status.side_effect = error
    else:
        resp.raise_for_status.return_value = None
    return resp


def _backend(base_url="http://192.168.178.113:8643", api_key="secret-key", agent="dev"):
    return CrewChatBackend(base_url, api_key=api_key, agent=agent)


class TestRequestShape:
    def test_direct_posts_the_exact_wire_shape(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "pong"})
            result = backend.direct("unused-model", "be terse", "ping", timeout_sec=12.0)

        assert result == "pong"
        mock_post.assert_called_once_with(
            "http://192.168.178.113:8643/chat",
            headers={"X-Crew-Key": "secret-key"},
            json={"agent": "dev", "message": "be terse\n\nping"},
            timeout=12.0,
        )

    def test_no_api_key_sends_no_header(self):
        backend = _backend(api_key="")
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "pong"})
            backend.direct("unused-model", "", "ping", timeout_sec=5.0)

        assert mock_post.call_args.kwargs["headers"] == {}

    def test_agent_name_is_lowercased(self):
        backend = _backend(agent="DEV")
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "pong"})
            backend.direct("unused-model", "", "ping", timeout_sec=5.0)

        assert mock_post.call_args.kwargs["json"]["agent"] == "dev"

    def test_chat_flattens_messages_into_one_field(self):
        backend = _backend()
        messages = [
            {"role": "system", "content": "sys prompt"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "follow-up"},
        ]
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "done"})
            backend.chat("unused-model", messages, timeout_sec=20.0)

        sent = mock_post.call_args.kwargs["json"]
        assert sent["agent"] == "dev"
        assert "sys prompt" in sent["message"]
        assert "first question" in sent["message"]
        assert "first answer" in sent["message"]
        assert sent["message"].strip().endswith("follow-up")

    def test_chat_returns_openai_shaped_message(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "hello"})
            result = backend.chat("unused-model", [{"role": "user", "content": "hi"}], timeout_sec=5.0)

        assert result["message"] == {"role": "assistant", "content": "hello"}
        assert result["choices"][0]["message"]["content"] == "hello"

    def test_streaming_forwards_the_full_text_once(self):
        backend = _backend()
        chunks = []
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "the whole reply"})
            result = backend.streaming("unused-model", "sys", "ping", on_token=chunks.append, timeout_sec=5.0)

        assert result == "the whole reply"
        assert chunks == ["the whole reply"]


class TestToolsNotSupported:
    def test_chat_with_tools_raises_without_making_a_request(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            with pytest.raises(ToolsNotSupportedError):
                backend.chat(
                    "unused-model",
                    [{"role": "user", "content": "hi"}],
                    tools=[{"type": "function", "function": {"name": "ping"}}],
                    timeout_sec=5.0,
                )
        mock_post.assert_not_called()


class TestEmptyResponse:
    def test_missing_reply_field_returns_none(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={})
            assert backend.chat("unused-model", [{"role": "user", "content": "hi"}], timeout_sec=5.0) is None

    def test_blank_reply_returns_none(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(json_body={"reply": "   "})
            assert backend.direct("unused-model", "sys", "hi", timeout_sec=5.0) is None


class TestFailClosedWhenUnconfigured:
    def test_missing_base_url_raises_provider_error(self):
        backend = _backend(base_url="", agent="dev")
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            with pytest.raises(ProviderError):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)
        mock_post.assert_not_called()

    def test_missing_agent_raises_provider_error_not_a_guess(self):
        backend = _backend(base_url="http://nas:8643", agent="")
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            with pytest.raises(ProviderError):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)
        mock_post.assert_not_called()


class TestTypedFailures:
    @pytest.mark.parametrize("status,expected", [
        (401, AuthError),
        (403, AuthError),
        (404, ModelUnavailableError),
    ])
    def test_http_status_maps_to_typed_failure(self, status, expected):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(status_code=status)
            with pytest.raises(expected):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)

    def test_429_with_retry_after_maps_to_rate_limited(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(status_code=429, headers={"Retry-After": "30"})
            with pytest.raises(RateLimitedError):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)

    def test_unrecognised_status_maps_to_provider_error(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(status_code=500)
            with pytest.raises(ProviderError):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)

    def test_timeout_raises_provider_error(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post", side_effect=requests.exceptions.Timeout()):
            with pytest.raises(ProviderError):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)

    def test_connection_error_raises_provider_error(self):
        backend = _backend()
        with patch("jarvis.llm.crew_chat.requests.post", side_effect=requests.exceptions.ConnectionError()):
            with pytest.raises(ProviderError):
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)

    def test_no_endpoint_url_key_or_body_in_exception_text(self):
        backend = CrewChatBackend("http://192.168.178.113:8643", api_key="top-secret", agent="dev")
        with patch("jarvis.llm.crew_chat.requests.post") as mock_post:
            mock_post.return_value = _response(status_code=500, json_body={"reply": "leaked-body-text"})
            try:
                backend.direct("unused-model", "sys", "hi", timeout_sec=5.0)
                assert False, "expected ProviderError"
            except ProviderError as error:
                text = str(error)
                assert "192.168.178.113" not in text
                assert "top-secret" not in text
                assert "leaked-body-text" not in text


class TestUnsupportedMethods:
    def test_embed_returns_none(self):
        backend = _backend()
        assert backend.embed("text", "model") is None

    def test_list_models_returns_empty(self):
        backend = _backend()
        assert backend.list_models() == []

    def test_warm_up_is_a_no_op_true(self):
        backend = _backend()
        assert backend.warm_up("model") is True


class TestNeverThePrivateLane:
    def test_crew_chat_route_is_excluded_from_private_tier(self):
        from types import SimpleNamespace
        from jarvis.llm import Tier, get_llm_backend

        cfg = SimpleNamespace(
            llm_routes=[{
                "name": "crew-chat",
                "provider": "crew_chat",
                "base_url": "crew-chat",
                "api_key": "",
                "model": "crew-chat",
                "tier": "chat",
                "timeout_sec": 30.0,
                "enabled": True,
            }, {
                "name": "crew-chat-private-attempt",
                "provider": "crew_chat",
                "base_url": "crew-chat",
                "api_key": "",
                "model": "crew-chat",
                "tier": "private",
                "timeout_sec": 30.0,
                "enabled": True,
            }],
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
            crew_api_url="http://192.168.178.113:8643",
            crew_api_key="key",
            crew_chat_agent="dev",
        )
        backend = get_llm_backend(cfg)

        private_providers = {route.provider for route in backend.routes_for(Tier.PRIVATE)}
        assert private_providers == {"ollama"}

    def test_crew_chat_is_not_selectable_as_the_single_endpoint_provider(self):
        from types import SimpleNamespace
        from jarvis.llm import Tier, get_llm_backend

        cfg = SimpleNamespace(
            llm_routes=[],
            llm_provider="crew_chat",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
        )
        backend = get_llm_backend(cfg)

        chat_route = backend.routes_for(Tier.CHAT)[0]
        assert chat_route.provider == "ollama"

    def test_crew_chat_is_not_selectable_as_the_embedding_provider(self):
        from types import SimpleNamespace
        from jarvis.llm import get_embedding_backend
        from jarvis.llm.ollama import OllamaBackend

        cfg = SimpleNamespace(
            llm_routes=[],
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
            embedding_provider="crew_chat",
        )
        backend = get_embedding_backend(cfg)
        assert isinstance(backend, OllamaBackend)


class TestFactoryWiresSettingsIntoTheRoute:
    def test_crew_chat_backend_reads_cfg_not_the_route_placeholder(self):
        """The route entry's own base_url/api_key/model are placeholders;
        the built backend must actually target cfg.crew_api_url et al."""
        from types import SimpleNamespace
        from jarvis.llm import Tier, get_llm_backend

        cfg = SimpleNamespace(
            llm_routes=[{
                "name": "crew-chat",
                "provider": "crew_chat",
                "base_url": "crew-chat",
                "api_key": "",
                "model": "crew-chat",
                "tier": "chat",
                "timeout_sec": 30.0,
                "enabled": True,
            }],
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-model",
            fast_model="local-fast",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-model",
            ollama_embed_model="nomic-embed-text",
            crew_api_url="http://192.168.178.113:8643",
            crew_api_key="nas-key",
            crew_chat_agent="research",
        )
        backend = get_llm_backend(cfg)
        route = next(r for r in backend.routes_for(Tier.CHAT) if r.provider == "crew_chat")
        built = backend._backend(route)

        assert built._base_url == "http://192.168.178.113:8643"
        assert built._api_key == "nas-key"
        assert built._agent == "research"
