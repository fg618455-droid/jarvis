"""Behaviour tests for the opt-in cloud TTS fallback chain."""

from __future__ import annotations

import inspect
import io
import json
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from src.jarvis.output.tts import PiperTTS, Utterance


class _Provider:
    def __init__(self, outcome):
        self.outcome = outcome

    def synthesise(self, text, voice_id, model, timeout_sec, cancelled):
        outcome = self.outcome
        if isinstance(outcome, BaseException):
            raise outcome
        yield from outcome


class _ForbiddenProvider:
    def synthesise(self, text, voice_id, model, timeout_sec, cancelled):
        raise AssertionError("a later or blocked provider was contacted")
        yield


class _LocalEngine:
    enabled = True

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.spoken = []
        self.interrupted = False

    def _speak_once(self, utterance):
        if self.fail:
            raise RuntimeError("local synthesis failed")
        self.spoken.append(utterance.text)
        if utterance.completion_callback:
            utterance.completion_callback()

    def interrupt(self):
        self.interrupted = True

    def stop(self):
        pass


def _config(name: str, **overrides):
    from src.jarvis.output.cloud_tts import CloudProviderConfig

    values = {
        "name": name,
        "provider": f"fake-{name}",
        "api_key_env": f"KEY_{name.upper()}",
        "voice_id": f"voice-{name}",
        "model": f"model-{name}",
        "enabled": True,
        "timeout_sec": 2.0,
    }
    values.update(overrides)
    return CloudProviderConfig(**values)


def _engine(tmp_path: Path, providers, clients, local=None, **kwargs):
    from src.jarvis.output.cloud_tts import CloudTTS, TTSProviderStateStore

    state = kwargs.pop("state_store", TTSProviderStateStore(tmp_path / "state.json"))
    engine = CloudTTS(
        providers=providers,
        local_engine=local or _LocalEngine(),
        state_store=state,
        provider_factory=lambda config, api_key: clients[config.name],
        **kwargs,
    )
    played = []
    engine._play_pcm = lambda pcm, sample_rate, utterance: played.append(
        (bytes(pcm), sample_rate, utterance.text)
    )
    return engine, played


def test_no_cloud_providers_uses_the_local_engine(tmp_path):
    local = _LocalEngine()
    engine, played = _engine(tmp_path, [], {}, local=local)

    engine._speak_once(Utterance("Local words"))

    assert local.spoken == ["Local words"]
    assert played == []


def test_first_healthy_provider_supplies_the_audio(tmp_path):
    from src.jarvis.output.cloud_tts import TTSAudioChunk

    providers = [_config("first"), _config("later")]
    clients = {
        "first": _Provider([TTSAudioChunk.pcm16(b"\x01\x00\x02\x00", 24_000)]),
        "later": _ForbiddenProvider(),
    }
    local = _LocalEngine()
    engine, played = _engine(tmp_path, providers, clients, local=local)

    engine._speak_once(Utterance("Cloud words"))

    assert played == [(b"\x01\x00\x02\x00", 24_000, "Cloud words")]
    assert local.spoken == []


def test_disabled_provider_is_skipped(tmp_path):
    from src.jarvis.output.cloud_tts import TTSAudioChunk

    providers = [_config("disabled", enabled=False), _config("healthy")]
    clients = {
        "disabled": _ForbiddenProvider(),
        "healthy": _Provider([TTSAudioChunk.pcm16(b"\x08\x00", 24_000)]),
    }
    engine, played = _engine(tmp_path, providers, clients)

    engine._speak_once(Utterance("Enabled only"))

    assert played == [(b"\x08\x00", 24_000, "Enabled only")]


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param("quota", id="quota"),
        pytest.param("rate", id="rate-limit"),
        pytest.param("auth", id="authentication"),
        pytest.param("timeout", id="timeout"),
        pytest.param("network", id="network"),
        pytest.param("unavailable", id="unavailable-audio"),
    ],
)
def test_typed_provider_failures_fall_through(tmp_path, failure):
    from src.jarvis.output.cloud_tts import (
        TTSAudioChunk,
        TTSAuthenticationError,
        TTSNetworkError,
        TTSProviderTimeout,
        TTSProviderUnavailable,
        TTSQuotaExhausted,
        TTSRateLimited,
    )

    errors = {
        "quota": TTSQuotaExhausted(),
        "rate": TTSRateLimited(),
        "auth": TTSAuthenticationError(),
        "timeout": TTSProviderTimeout(),
        "network": TTSNetworkError(),
        "unavailable": TTSProviderUnavailable(),
    }
    providers = [_config("broken"), _config("healthy")]
    clients = {
        "broken": _Provider(errors[failure]),
        "healthy": _Provider([TTSAudioChunk.pcm16(b"\x03\x00", 22_050)]),
    }
    engine, played = _engine(tmp_path, providers, clients)

    engine._speak_once(Utterance("Fallback words"))

    assert played == [(b"\x03\x00", 22_050, "Fallback words")]


def test_empty_audio_falls_through(tmp_path):
    from src.jarvis.output.cloud_tts import TTSAudioChunk

    providers = [_config("empty"), _config("healthy")]
    clients = {
        "empty": _Provider([]),
        "healthy": _Provider([TTSAudioChunk.pcm16(b"\x04\x00", 16_000)]),
    }
    engine, played = _engine(tmp_path, providers, clients)

    engine._speak_once(Utterance("Still audible"))

    assert played == [(b"\x04\x00", 16_000, "Still audible")]


def test_all_cloud_failures_land_on_local_engine(tmp_path):
    from src.jarvis.output.cloud_tts import TTSNetworkError, TTSProviderTimeout

    providers = [_config("one"), _config("two")]
    clients = {
        "one": _Provider(TTSNetworkError()),
        "two": _Provider(TTSProviderTimeout()),
    }
    local = _LocalEngine()
    engine, played = _engine(tmp_path, providers, clients, local=local)

    engine._speak_once(Utterance("Local fallback"))

    assert played == []
    assert local.spoken == ["Local fallback"]


def test_local_failure_skips_without_raising(tmp_path, capsys):
    local = _LocalEngine(fail=True)
    engine, _ = _engine(tmp_path, [], {}, local=local)

    engine._speak_once(Utterance("The answer survives"))

    assert "⚠️" in capsys.readouterr().out


def test_rate_limit_block_survives_a_fresh_chain(tmp_path):
    from src.jarvis.output.cloud_tts import (
        TTSProviderStateStore,
        TTSRateLimited,
    )

    path = tmp_path / "state.json"
    provider = _config("limited")
    first_state = TTSProviderStateStore(path, now=lambda: 1_000.0)
    first, _ = _engine(
        tmp_path,
        [provider],
        {"limited": _Provider(TTSRateLimited(retry_after=120))},
        state_store=first_state,
    )
    first._speak_once(Utterance("First"))

    local = _LocalEngine()
    fresh_state = TTSProviderStateStore(path, now=lambda: 1_001.0)
    fresh, _ = _engine(
        tmp_path,
        [provider],
        {"limited": _ForbiddenProvider()},
        local=local,
        state_store=fresh_state,
    )
    fresh._speak_once(Utterance("Second"))

    assert local.spoken == ["Second"]


def test_authentication_failure_is_not_written_as_a_persisted_block(tmp_path):
    from src.jarvis.output.cloud_tts import (
        TTSAudioChunk,
        TTSAuthenticationError,
        TTSProviderStateStore,
    )

    path = tmp_path / "state.json"
    provider = _config("auth")
    first, _ = _engine(
        tmp_path,
        [provider],
        {"auth": _Provider(TTSAuthenticationError())},
        state_store=TTSProviderStateStore(path, run_invalid=set()),
    )
    first._speak_once(Utterance("Old credential"))

    restarted, played = _engine(
        tmp_path,
        [provider],
        {"auth": _Provider([TTSAudioChunk.pcm16(b"\x05\x00", 24_000)])},
        state_store=TTSProviderStateStore(path, run_invalid=set()),
    )
    restarted._speak_once(Utterance("Corrected credential"))

    assert played == [(b"\x05\x00", 24_000, "Corrected credential")]


def test_interrupt_stops_cloud_playback_promptly(tmp_path):
    from src.jarvis.output.cloud_tts import TTSAudioChunk

    provider = _config("slow-playback")
    engine, _ = _engine(
        tmp_path,
        [provider],
        {"slow-playback": _Provider([TTSAudioChunk.pcm16(b"\x01\x00", 24_000)])},
    )
    playback_started = threading.Event()

    def play(pcm, sample_rate, utterance):
        playback_started.set()
        while not engine._should_interrupt.wait(0.01):
            pass

    engine._play_pcm = play
    worker = threading.Thread(target=engine._speak_once, args=(Utterance("Stop me"),))
    worker.start()
    assert playback_started.wait(1.0)

    started = time.monotonic()
    engine.interrupt()
    worker.join(0.5)

    assert not worker.is_alive()
    assert time.monotonic() - started < 0.5


def test_interrupt_during_failure_prevents_the_next_provider(tmp_path):
    from src.jarvis.output.cloud_tts import TTSNetworkError

    entered = threading.Event()
    release = threading.Event()

    class FailingAfterRelease:
        def synthesise(self, text, voice_id, model, timeout_sec, cancelled):
            entered.set()
            release.wait(1.0)
            raise TTSNetworkError()
            yield

    providers = [_config("first"), _config("second")]
    engine, _ = _engine(
        tmp_path,
        providers,
        {"first": FailingAfterRelease(), "second": _ForbiddenProvider()},
    )
    worker = threading.Thread(target=engine._speak_once, args=(Utterance("Stop chain"),))
    worker.start()
    assert entered.wait(1.0)

    engine.interrupt()
    release.set()
    worker.join(1.0)

    assert not worker.is_alive()


def test_state_file_contains_only_hashes_and_health_data(tmp_path, monkeypatch):
    from src.jarvis.output.cloud_tts import TTSProviderStateStore, TTSRateLimited

    path = tmp_path / "state.json"
    provider = _config(
        "https://secret.example",
        api_key_env="VERY_SECRET_KEY_ENV",
        voice_id="felix-private-voice",
        model="identifying-model",
    )
    monkeypatch.setenv("VERY_SECRET_KEY_ENV", "credential-value")
    engine, _ = _engine(
        tmp_path,
        [provider],
        {provider.name: _Provider(TTSRateLimited(retry_after=60))},
        state_store=TTSProviderStateStore(path),
    )
    engine._speak_once(Utterance("private spoken words"))

    raw = path.read_text(encoding="utf-8")
    parsed = json.loads(raw)
    assert set(parsed) == {"version", "providers"}
    for private_value in (
        "secret.example",
        "VERY_SECRET_KEY_ENV",
        "credential-value",
        "felix-private-voice",
        "identifying-model",
        "private spoken words",
    ):
        assert private_value not in raw


def test_cloud_engine_matches_the_piper_public_interface(tmp_path):
    engine, _ = _engine(tmp_path, [], {})
    interface = {
        "start",
        "stop",
        "speak",
        "end_of_reply",
        "interrupt",
        "is_speaking",
        "get_last_spoken_text",
    }

    assert all(callable(getattr(engine, method, None)) for method in interface)
    assert {
        name for name, member in inspect.getmembers(PiperTTS, inspect.isfunction)
        if name in interface
    } == interface


def test_factory_selects_cloud_and_always_builds_a_local_fallback():
    from src.jarvis.output.cloud_tts import CloudTTS
    from src.jarvis.output.tts import create_tts_engine

    engine = create_tts_engine(
        engine="cloud",
        enabled=False,
        cloud_providers=[],
        local_fallback_engine="piper",
    )

    assert isinstance(engine, CloudTTS)
    assert isinstance(engine.local_engine, PiperTTS)


def test_defaults_keep_cloud_speech_off():
    from src.jarvis.config import get_default_config

    defaults = get_default_config()

    assert defaults["tts_engine"] == "piper"
    assert [entry["provider"] for entry in defaults["tts_cloud_providers"]] == [
        "fish_audio",
        "elevenlabs",
    ]
    assert defaults["tts_local_fallback_engine"] == "piper"


def test_cloud_provider_config_ignores_malformed_entries():
    from unittest.mock import patch

    from src.jarvis.config import load_settings

    configured = {
        "_config_version": 999,
        "tts_engine": "cloud",
        "tts_cloud_providers": [
            {
                "name": "primary",
                "provider": "fake",
                "api_key_env": "TTS_API_KEY",
                "voice_id": "voice-1",
                "model": "model-1",
                "enabled": True,
                "timeout_sec": 3,
                "api_key": "must-not-be-copied",
            },
            {"name": "missing required fields"},
            "not-an-object",
        ],
        "tts_local_fallback_engine": "kokoro",
    }
    with patch("src.jarvis.config._load_json", return_value=configured):
        settings = load_settings()

    assert settings.tts_engine == "cloud"
    assert settings.tts_local_fallback_engine == "kokoro"
    assert settings.tts_cloud_providers == [{
        "name": "primary",
        "provider": "fake",
        "api_key_env": "TTS_API_KEY",
        "voice_id": "voice-1",
        "model": "model-1",
        "enabled": True,
        "timeout_sec": 3.0,
    }]


def test_credential_is_resolved_only_when_client_is_built(tmp_path, monkeypatch):
    from src.jarvis.output.cloud_tts import TTSAudioChunk

    monkeypatch.setenv("KEY_LAZY", "credential-value")
    observed = []
    provider = _config("lazy", api_key_env="KEY_LAZY")

    def build(config, api_key):
        observed.append(api_key)
        return _Provider([TTSAudioChunk.pcm16(b"\x01\x00", 24_000)])

    from src.jarvis.output.cloud_tts import CloudTTS, TTSProviderStateStore

    engine = CloudTTS(
        providers=[provider],
        local_engine=_LocalEngine(),
        state_store=TTSProviderStateStore(tmp_path / "state.json"),
        provider_factory=build,
    )
    engine._play_pcm = lambda *args: None
    assert observed == []

    engine._speak_once(Utterance("Build lazily"))

    assert observed == ["credential-value"]
    assert "credential-value" not in repr(provider)
    assert "credential-value" not in repr(engine)


def test_complete_pcm_wav_response_is_accepted(tmp_path):
    from src.jarvis.output.cloud_tts import TTSAudioChunk

    buffer = io.BytesIO()
    with __import__("wave").open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(16_000)
        target.writeframes(b"\x06\x00\x07\x00")
    provider = _config("wav")
    engine, played = _engine(
        tmp_path,
        [provider],
        {"wav": _Provider([TTSAudioChunk.wav(buffer.getvalue())])},
    )

    engine._speak_once(Utterance("WAV words"))

    assert played == [(b"\x06\x00\x07\x00", 16_000, "WAV words")]


def test_progressive_rate_limit_and_quota_reset_rules(tmp_path):
    from src.jarvis.output.cloud_tts import (
        TTSProviderStateStore,
        TTSQuotaExhausted,
        TTSRateLimited,
    )

    provider = _config("cooldown")
    path = tmp_path / "state.json"
    clock = [1_700_000_000.0]
    state = TTSProviderStateStore(path, now=lambda: clock[0], run_invalid=set())

    for delay in (60.0, 300.0, 900.0):
        state.record_failure(provider, TTSRateLimited())
        persisted = next(iter(json.loads(path.read_text())["providers"].values()))
        assert persisted["blocked_until"] == clock[0] + delay
        clock[0] = persisted["blocked_until"] + 1

    stated_reset = clock[0] + 12_345
    state.record_failure(provider, TTSQuotaExhausted(reset_at=stated_reset))
    persisted = next(iter(json.loads(path.read_text())["providers"].values()))
    assert persisted["blocked_until"] == stated_reset

    clock[0] = stated_reset + 1
    state.record_failure(provider, TTSQuotaExhausted())
    persisted = next(iter(json.loads(path.read_text())["providers"].values()))
    reset = datetime.fromtimestamp(persisted["blocked_until"], tz=timezone.utc)
    assert (reset.hour, reset.minute, reset.second) == (0, 0, 0)
