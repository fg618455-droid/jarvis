from __future__ import annotations

import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from jarvis.providers.auth import (
    AuthenticationManager,
    parse_claude_auth_status,
    parse_codex_auth_status,
    parse_hermes_auth_status,
    scrub_provider_environment,
)
from jarvis.providers.base import AuthStatus


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            '{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty",'
            '"email":"person@example.com","orgId":"org-1","subscriptionType":"pro"}',
            (True, "claude.ai", "person@example.com", "pro"),
        ),
        (
            '{"loggedIn":false,"authMethod":null,"apiProvider":null,"email":null,'
            '"orgId":null,"subscriptionType":null}',
            (False, None, None, None),
        ),
    ],
)
def test_claude_auth_parser_handles_documented_states(payload, expected):
    status = parse_claude_auth_status(payload)

    assert (status.logged_in, status.method, status.account, status.plan) == expected


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "{}",
        '{"loggedIn":"yes","authMethod":"claude.ai"}',
        '{"loggedIn":true,"authMethod":"api_key"}',
    ],
)
def test_claude_auth_parser_fails_closed_for_changed_or_non_subscription_output(payload):
    assert parse_claude_auth_status(payload).logged_in is False


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "logged_in", "method"),
    [
        ("Logged in using ChatGPT\n", True, "chatgpt"),
        ("Not logged in\n", False, None),
        ("Logged in using API key\n", False, "api_key"),
        ("Authentication state: fine\n", False, None),
    ],
)
def test_codex_auth_parser_accepts_only_the_chatgpt_subscription(payload, logged_in, method):
    status = parse_codex_auth_status(payload)

    assert status.logged_in is logged_in
    assert status.method == method


@pytest.mark.unit
@pytest.mark.parametrize(
    ("payload", "logged_in", "method"),
    [
        ("openai-codex: logged in\n", True, "chatgpt"),
        ("openai-codex: not logged in\n", False, None),
        ("anthropic: logged in\n", False, None),
        ("Credentials ready\n", False, None),
    ],
)
def test_hermes_auth_parser_accepts_only_openai_codex_subscription(payload, logged_in, method):
    status = parse_hermes_auth_status(payload, "openai-codex")

    assert status.logged_in is logged_in
    assert status.method == method


@pytest.mark.unit
def test_hermes_authentication_reads_the_provider_from_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="openai-codex: logged in\n", stderr="")

    status = AuthenticationManager(
        runner=runner,
        hermes_command="hermes",
        hermes_config_path=config_path,
    ).status("hermes")

    assert status.logged_in is True
    assert status.method == "chatgpt"
    assert status.plan == "openai-codex"
    assert calls == [["hermes", "auth", "status", "openai-codex"]]


@pytest.mark.unit
def test_hermes_authentication_fails_closed_when_config_is_missing(tmp_path):
    def runner(command, **kwargs):
        raise AssertionError("runner must not be called")

    status = AuthenticationManager(
        runner=runner,
        hermes_config_path=tmp_path / "missing.yaml",
    ).status("hermes")

    assert status.logged_in is False
    assert status.method is None
    assert status.detail == "hermes provider unknown"


@pytest.mark.unit
def test_hermes_authentication_fails_closed_when_config_is_unreadable(tmp_path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("model:\n  provider: openai-codex\n", encoding="utf-8")
    monkeypatch.setattr(
        type(config_path),
        "read_text",
        lambda self, **kwargs: (_ for _ in ()).throw(PermissionError("denied")),
    )

    status = AuthenticationManager(hermes_config_path=config_path).status("hermes")

    assert status == AuthStatus(detail="hermes provider unknown")


@pytest.mark.unit
@pytest.mark.parametrize("contents", ["", "model:\n  provider: ''\n", "provider: openai-codex\n"])
def test_hermes_authentication_fails_closed_when_model_provider_is_unavailable(
    tmp_path, contents
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(contents, encoding="utf-8")

    status = AuthenticationManager(hermes_config_path=config_path).status("hermes")

    assert status == AuthStatus(detail="hermes provider unknown")


@pytest.mark.unit
def test_provider_environment_removes_billing_keys_and_keeps_unrelated_values():
    environment = scrub_provider_environment(
        {
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "OPENAI_API_KEY": "openai-secret",
            "PATH": "provider-tools",
        }
    )

    assert "ANTHROPIC_API_KEY" not in environment
    assert "OPENAI_API_KEY" not in environment
    assert environment["PATH"] == "provider-tools"


@pytest.mark.unit
def test_scrubbed_environment_is_absent_inside_a_real_child_process():
    parent = os.environ.copy()
    parent["ANTHROPIC_API_KEY"] = "anthropic-secret"
    parent["OPENAI_API_KEY"] = "openai-secret"
    environment = scrub_provider_environment(parent)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import os; print('ANTHROPIC_API_KEY' in os.environ, 'OPENAI_API_KEY' in os.environ)",
        ],
        capture_output=True,
        check=True,
        env=environment,
        text=True,
    )

    assert result.stdout.strip() == "False False"


@pytest.mark.unit
def test_authentication_status_is_cached_for_sixty_seconds(monkeypatch):
    calls = []
    now = [100.0]

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")

    manager = AuthenticationManager(runner=runner, clock=lambda: now[0])

    assert manager.status("codex").logged_in is True
    now[0] = 159.999
    assert manager.status("codex").logged_in is True
    assert len(calls) == 1

    now[0] = 160.0
    assert manager.status("codex").logged_in is True
    assert len(calls) == 2
    assert "ANTHROPIC_API_KEY" not in calls[0][1]["env"]
    assert "OPENAI_API_KEY" not in calls[0][1]["env"]


@pytest.mark.unit
def test_authentication_cache_can_be_invalidated():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT\n", stderr="")

    manager = AuthenticationManager(runner=runner)
    manager.status("codex")
    manager.invalidate("codex")
    manager.status("codex")

    assert len(calls) == 2


@pytest.mark.unit
def test_authentication_manager_fails_closed_when_command_fails():
    def runner(command, **kwargs):
        return SimpleNamespace(returncode=1, stdout="Logged in using ChatGPT\n", stderr="failure")

    assert AuthenticationManager(runner=runner).status("codex").logged_in is False


@pytest.mark.unit
def test_authentication_manager_fails_closed_when_process_cannot_start():
    def runner(command, **kwargs):
        raise FileNotFoundError("provider executable missing")

    assert AuthenticationManager(runner=runner).status("claude").logged_in is False


@pytest.mark.unit
def test_authentication_manager_rejects_unknown_providers_without_starting_a_process():
    def runner(command, **kwargs):
        raise AssertionError("runner must not be called")

    with pytest.raises(ValueError, match="unknown"):
        AuthenticationManager(runner=runner).status("unknown")
