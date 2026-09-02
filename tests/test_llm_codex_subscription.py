"""Behaviour tests for the subscription-backed Codex CLI LLM backend."""

from __future__ import annotations

import io
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest


def _event(text: str) -> str:
    return json.dumps({
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }) + "\n"


class FakeProcess:
    """Small observable stand-in for the Codex child process."""

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0,
                 hang: bool = False) -> None:
        class CapturingStdin(io.StringIO):
            def close(self):
                self.was_closed = True

        self.stdin = CapturingStdin()
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.returncode = None if hang else returncode
        self._final_returncode = returncode
        self.hang = hang
        self.terminated = False
        self.reaped = False
        self.pid = 4242

    def wait(self, timeout=None):
        if self.hang and not self.terminated:
            raise subprocess.TimeoutExpired("codex", timeout)
        self.reaped = True
        if self.returncode is None:
            self.returncode = self._final_returncode
        return self.returncode

    def poll(self):
        return self.returncode

    def send_signal(self, _signal):
        self.terminated = True
        self.returncode = -1

    def terminate(self):
        self.terminated = True
        self.returncode = -1

    def kill(self):
        self.terminated = True
        self.returncode = -9


def _run_with(process: FakeProcess, method="direct", *, model="gpt-5.6-sol",
              system="system instruction", user="private user prompt", **kwargs):
    from jarvis.llm.codex_subscription import CodexSubscriptionBackend

    observed = {}

    def start(args, **popen_kwargs):
        observed["args"] = list(args)
        observed["kwargs"] = popen_kwargs
        return process

    backend = CodexSubscriptionBackend()
    with patch(
        "jarvis.llm.codex_subscription._resolve_codex_launcher",
        return_value=["codex"],
    ), patch("jarvis.llm.codex_subscription.subprocess.Popen", side_effect=start):
        result = getattr(backend, method)(
            model, system, user, timeout_sec=kwargs.pop("timeout_sec", 5.0), **kwargs
        )
    observed["prompt"] = process.stdin.getvalue()
    return result, observed


class TestSafeInvocation:
    def test_direct_uses_restrictive_noninteractive_flags_and_stdin(self):
        result, observed = _run_with(FakeProcess(stdout=_event("safe answer")))

        assert result == "safe answer"
        args = observed["args"]
        assert args[:2] == ["codex", "exec"]
        assert "--sandbox" in args
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert "--ignore-user-config" in args
        assert "--ephemeral" in args
        assert "-c" in args
        assert 'approval_policy="never"' in args
        assert 'forced_login_method="chatgpt"' in args
        assert 'web_search="disabled"' in args
        assert "features.shell_tool=false" in args
        assert 'model_reasoning_effort="low"' in args
        assert args[args.index("--model") + 1] == "gpt-5.6-sol"
        assert args[-1] == "-"
        assert "private user prompt" not in args
        assert observed["prompt"] == "system instruction\n\nprivate user prompt"

        cwd = observed["kwargs"]["cwd"]
        assert cwd
        assert args[args.index("--cd") + 1] == cwd

    def test_child_environment_cannot_select_a_metered_api_key(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "metered-secret")
        monkeypatch.setenv("ANOTHER_API_KEY", "another-secret")
        monkeypatch.setenv("CODEX_ACCESS_TOKEN", "direct-token")
        monkeypatch.setenv("CODEX_HOME", r"C:\subscription-login")

        _result, observed = _run_with(FakeProcess(stdout=_event("answer")))

        child_environment = observed["kwargs"]["env"]
        assert "OPENAI_API_KEY" not in child_environment
        assert "ANOTHER_API_KEY" not in child_environment
        assert "CODEX_ACCESS_TOKEN" not in child_environment
        assert child_environment["CODEX_HOME"] == r"C:\subscription-login"

    def test_windows_cmd_shim_resolves_to_node_without_a_command_shell(self):
        from pathlib import Path
        from jarvis.llm.codex_subscription import _resolve_codex_launcher

        def which(name):
            return {
                "codex": r"C:\npm\codex.cmd",
                "node": r"C:\Program Files\nodejs\node.exe",
            }[name]

        with patch("jarvis.llm.codex_subscription.os.name", "nt"), patch(
            "jarvis.llm.codex_subscription.shutil.which", side_effect=which
        ), patch.object(Path, "is_file", return_value=True):
            launcher = _resolve_codex_launcher()

        assert launcher[0] == r"C:\Program Files\nodejs\node.exe"
        assert launcher[1].replace("\\", "/").endswith(
            "/node_modules/@openai/codex/bin/codex.js"
        )

    def test_streaming_forwards_the_cli_whole_reply_shape_once(self):
        chunks = []
        result, _ = _run_with(
            FakeProcess(stdout=_event("whole reply")),
            method="streaming",
            on_token=chunks.append,
        )

        assert result == "whole reply"
        assert chunks == ["whole reply"]


class TestTextGenerationOnly:
    def test_tools_are_rejected_before_a_process_starts(self):
        from jarvis.llm import ToolsNotSupportedError
        from jarvis.llm.codex_subscription import CodexSubscriptionBackend

        started = False

        def start(*_args, **_kwargs):
            nonlocal started
            started = True
            return FakeProcess()

        with patch("jarvis.llm.codex_subscription.subprocess.Popen", side_effect=start):
            with pytest.raises(ToolsNotSupportedError):
                CodexSubscriptionBackend().chat(
                    "gpt-5.6-sol",
                    [{"role": "user", "content": "hello"}],
                    tools=[{"type": "function", "function": {"name": "ping"}}],
                )

        assert started is False

    def test_chat_uses_the_claude_subscription_flattening_shape(self):
        from jarvis.llm.claude_subscription import _flatten_messages
        from jarvis.llm.codex_subscription import CodexSubscriptionBackend

        messages = [
            {"role": "system", "content": "system one"},
            {"role": "system", "content": "system two"},
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "second"},
            {"role": "tool", "content": "third"},
        ]
        process = FakeProcess(stdout=_event("done"))
        with patch(
            "jarvis.llm.codex_subscription.subprocess.Popen", return_value=process
        ):
            response = CodexSubscriptionBackend().chat("gpt-5.6-sol", messages)

        system, transcript = _flatten_messages(messages)
        assert process.stdin.getvalue() == f"{system}\n\n{transcript}"
        assert response["message"] == {"role": "assistant", "content": "done"}

    def test_unsupported_methods_have_no_external_side_effect(self):
        from jarvis.llm.codex_subscription import CodexSubscriptionBackend

        backend = CodexSubscriptionBackend()
        assert backend.embed("text", "model") is None
        assert backend.list_models() == []
        assert backend.warm_up("model") is True


class TestDeadlineAndCleanup:
    def test_timeout_terminates_and_reaps_the_process(self):
        from jarvis.llm import ProviderError

        process = FakeProcess(hang=True)
        with pytest.raises(ProviderError, match="timed out"):
            _run_with(process, timeout_sec=0.01)

        assert process.terminated is True
        assert process.reaped is True
        assert process.poll() is not None

    def test_routing_passes_the_smaller_route_and_remaining_caller_budget(self, tmp_path):
        from jarvis.llm import RequestDeadline, Route, RoutedBackend, Tier
        from jarvis.llm.route_state import RouteStateStore

        now = [10.0]

        class RecordingBackend:
            def __init__(self, *, elapsed=0.0, result=None):
                self.elapsed = elapsed
                self.result = result
                self.timeouts = []

            def chat(self, _model, _messages, *, timeout_sec, **_kwargs):
                self.timeouts.append(timeout_sec)
                now[0] += self.elapsed
                return self.result

        first = Route(
            "first", "codex_subscription", "codex-cli", "", "gpt-5.6-sol",
            Tier.CHAT, 2.0,
        )
        second = Route(
            "second", "codex_subscription", "codex-cli", "", "gpt-5.6-sol",
            Tier.CHAT, 10.0,
        )
        first_backend = RecordingBackend(elapsed=1.5)
        second_backend = RecordingBackend(result={"message": {"content": "ok"}})
        router = RoutedBackend(
            [first, second],
            state_store=RouteStateStore(tmp_path / "state.json"),
            backend_factory={first: first_backend, second: second_backend}.__getitem__,
            clock=lambda: now[0],
        )
        deadline = RequestDeadline.after(3.0, clock=lambda: now[0])

        result = router.chat(
            "chat", [{"role": "user", "content": "hello"}],
            timeout_sec=20.0, deadline=deadline,
        )

        assert result["message"]["content"] == "ok"
        assert first_backend.timeouts == [2.0]
        assert second_backend.timeouts == [pytest.approx(1.5)]


class TestTypedFailures:
    @pytest.mark.parametrize(
        "stderr, expected_name",
        [
            ("401 Unauthorized: login expired", "AuthError"),
            ("model gpt-test was not found", "ModelUnavailableError"),
            ("429 Too Many Requests: rate limit exceeded", "RateLimitedError"),
            ("insufficient_quota: usage quota exhausted", "QuotaExhaustedError"),
            ("unexpected transport failure", "ProviderError"),
        ],
    )
    def test_cli_failure_maps_to_existing_typed_error(self, stderr, expected_name):
        import jarvis.llm as llm

        process = FakeProcess(stderr=stderr, returncode=1)
        with pytest.raises(getattr(llm, expected_name)):
            _run_with(process)

    def test_missing_codex_binary_maps_to_provider_error(self):
        from jarvis.llm import ProviderError
        from jarvis.llm.codex_subscription import CodexSubscriptionBackend

        with patch(
            "jarvis.llm.codex_subscription.subprocess.Popen",
            side_effect=FileNotFoundError("private executable path"),
        ):
            with pytest.raises(ProviderError):
                CodexSubscriptionBackend().direct(
                    "gpt-5.6-sol", "system", "private prompt"
                )

    def test_blank_success_is_an_empty_response(self):
        assert _run_with(FakeProcess(stdout=_event("   ")))[0] is None

    def test_prompt_stderr_response_and_paths_never_reach_logs_or_errors(self):
        from jarvis.llm import ProviderError

        private_values = {
            "secret spoken prompt",
            "C:\\private\\codex.exe",
            "secret stderr body",
            "secret response body",
        }
        process = FakeProcess(
            stdout=_event("secret response body"),
            stderr="secret stderr body C:\\private\\codex.exe",
            returncode=1,
        )
        logged = []
        with patch(
            "jarvis.llm.codex_subscription.debug_log",
            side_effect=lambda message, _area: logged.append(str(message)),
        ):
            with pytest.raises(ProviderError) as raised:
                _run_with(process, user="secret spoken prompt")

        emitted = "\n".join(logged + [str(raised.value)])
        assert all(value not in emitted for value in private_values)


class TestFactoryRouting:
    @staticmethod
    def _settings(routes, **overrides):
        values = dict(
            llm_routes=routes,
            llm_provider="ollama",
            llm_base_url="",
            llm_api_key="",
            llm_chat_model="local-chat-model",
            fast_model="local-fast-model",
            ollama_base_url="http://127.0.0.1:11434",
            ollama_chat_model="local-chat-model",
            ollama_embed_model="nomic-embed-text",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    @staticmethod
    def _route(name, tier="chat", provider="codex_subscription"):
        return {
            "name": name,
            "provider": provider,
            "base_url": "codex-cli",
            "api_key": "",
            "model": "gpt-5.6-sol",
            "tier": tier,
            "timeout_sec": 30.0,
            "enabled": True,
        }

    def test_codex_route_builds_the_exported_backend(self):
        from jarvis.llm import CodexSubscriptionBackend, Tier, get_llm_backend

        router = get_llm_backend(self._settings([self._route("codex")]))
        route = next(r for r in router.routes_for(Tier.CHAT) if r.name == "codex")

        assert isinstance(router._backend(route), CodexSubscriptionBackend)

    def test_codex_routes_are_chat_only_and_follow_faster_candidates(self):
        from jarvis.llm import Tier, get_llm_backend

        routes = [
            self._route("codex-first-in-config"),
            self._route("fast-cloud", provider="openai_compatible"),
            self._route("codex-fast-attempt", tier="fast"),
            self._route("codex-private-attempt", tier="private"),
        ]
        router = get_llm_backend(self._settings(routes))

        assert [r.name for r in router.routes_for(Tier.CHAT)] == [
            "fast-cloud", "codex-first-in-config", "local-chat"
        ]
        assert all(
            r.provider != "codex_subscription"
            for tier in (Tier.FAST, Tier.PRIVATE)
            for r in router.routes_for(tier)
        )

    def test_codex_is_not_a_single_endpoint_or_embedding_provider(self):
        from jarvis.llm import Tier, get_embedding_backend, get_llm_backend
        from jarvis.llm.ollama import OllamaBackend

        cfg = self._settings(
            [],
            llm_provider="codex_subscription",
            embedding_provider="codex_subscription",
        )

        assert get_llm_backend(cfg).routes_for(Tier.CHAT)[0].provider == "ollama"
        assert isinstance(get_embedding_backend(cfg), OllamaBackend)

    def test_route_metadata_offers_codex_without_a_settings_duplicate(self):
        from jarvis.config_metadata import FIELD_METADATA, LLM_ROUTE_FIELD_METADATA

        provider = next(
            item for item in LLM_ROUTE_FIELD_METADATA if item.key == "provider"
        )
        assert "codex_subscription" in {value for value, _label in provider.choices}
        assert "chat_backend_override" not in {item.key for item in FIELD_METADATA}
