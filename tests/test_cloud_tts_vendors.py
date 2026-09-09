"""Behaviour tests for the plain-HTTP cloud TTS vendor clients."""

from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest
import requests

from src.jarvis.output.tts import Utterance


class _Response:
    def __init__(
        self,
        status_code: int,
        *,
        chunks=(),
        body=None,
        headers=None,
    ) -> None:
        self.status_code = status_code
        self._chunks = tuple(chunks)
        self._body = body
        self.headers = dict(headers or {})
        self.closed = False

    def iter_content(self, chunk_size=8192):
        del chunk_size
        yield from self._chunks

    def json(self):
        if isinstance(self._body, BaseException):
            raise self._body
        return self._body

    def close(self):
        self.closed = True


class _Transport:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.requests = []

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _audio(client):
    return list(client.synthesise(
        "Words sent to speech",
        "opaque-voice",
        "configured-model",
        4.5,
        threading.Event(),
    ))


def test_fish_audio_streams_documented_pcm_request_shape():
    from src.jarvis.output.cloud_tts_vendors import FishAudioTTSProvider

    response = _Response(200, chunks=(b"\x01", b"\x00\x02", b"\x00"))
    transport = _Transport(response)
    chunks = _audio(FishAudioTTSProvider("fish-secret", transport=transport))

    assert [(chunk.data, chunk.format, chunk.sample_rate) for chunk in chunks] == [
        (b"\x01\x00", "pcm", 24_000),
        (b"\x02\x00", "pcm", 24_000),
    ]
    url, request = transport.requests[0]
    assert url == "https://api.fish.audio/v1/tts"
    assert request["headers"] == {
        "Authorization": "Bearer fish-secret",
        "Content-Type": "application/json",
        "model": "configured-model",
    }
    assert request["json"] == {
        "text": "Words sent to speech",
        "reference_id": "opaque-voice",
        "format": "pcm",
        "sample_rate": 24_000,
    }
    assert request["stream"] is True
    assert request["timeout"] == 4.5
    assert response.closed


def test_elevenlabs_streams_documented_pcm_request_shape():
    from src.jarvis.output.cloud_tts_vendors import ElevenLabsTTSProvider

    response = _Response(200, chunks=(b"\x03\x00", b"\x04\x00"))
    transport = _Transport(response)
    chunks = _audio(ElevenLabsTTSProvider("eleven-secret", transport=transport))

    assert [(chunk.data, chunk.format, chunk.sample_rate) for chunk in chunks] == [
        (b"\x03\x00", "pcm", 24_000),
        (b"\x04\x00", "pcm", 24_000),
    ]
    url, request = transport.requests[0]
    assert url == "https://api.elevenlabs.io/v1/text-to-speech/opaque-voice/stream"
    assert request["headers"] == {
        "xi-api-key": "eleven-secret",
        "Content-Type": "application/json",
    }
    assert request["params"] == {"output_format": "pcm_24000"}
    assert request["json"] == {
        "text": "Words sent to speech",
        "model_id": "configured-model",
    }
    assert request["stream"] is True
    assert request["timeout"] == 4.5
    assert response.closed


@pytest.mark.parametrize(
    ("provider_name", "response", "failure_type", "timing_attribute", "timing_value"),
    [
        pytest.param("fish", _Response(401, body={"status": 401, "message": "unauthorised"}),
                     "TTSAuthenticationError", None, None, id="fish-auth"),
        pytest.param("fish", _Response(402, body={"status": 402, "message": "credits exhausted"}),
                     "TTSQuotaExhausted", "reset_at", None, id="fish-quota"),
        pytest.param("fish", _Response(429, body={"status": 429, "message": "busy"},
                                       headers={"Retry-After": "17"}),
                     "TTSRateLimited", "retry_after", 17.0, id="fish-rate-limit"),
        pytest.param("fish", _Response(404, body={"status": 404, "message": "reference not found"}),
                     "TTSProviderError", None, None, id="fish-unknown-voice"),
        pytest.param("fish", _Response(422, body={"status": 422, "message": "model invalid"}),
                     "TTSProviderError", None, None, id="fish-unknown-model"),
        pytest.param("elevenlabs", _Response(401, body={"detail": {
            "type": "authentication_error", "code": "invalid_api_key"}}),
                     "TTSAuthenticationError", None, None, id="elevenlabs-auth"),
        pytest.param("elevenlabs", _Response(402, body={"detail": {
            "type": "payment_required", "code": "insufficient_credits"}}),
                     "TTSQuotaExhausted", "reset_at", None, id="elevenlabs-quota"),
        pytest.param("elevenlabs", _Response(401, body={"detail": {
            "status": "quota_exceeded"}}),
                     "TTSQuotaExhausted", "reset_at", None, id="elevenlabs-legacy-quota-status"),
        pytest.param("elevenlabs", _Response(429, body={"detail": {
            "type": "rate_limit_error", "code": "rate_limit_exceeded"}},
            headers={"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"}),
                     "TTSRateLimited", "retry_after", "http-date", id="elevenlabs-rate-limit"),
        pytest.param("elevenlabs", _Response(404, body={"detail": {
            "type": "not_found", "code": "voice_not_found"}}),
                     "TTSProviderError", None, None, id="elevenlabs-unknown-voice"),
        pytest.param("elevenlabs", _Response(404, body={"detail": {
            "type": "not_found", "code": "model_not_found"}}),
                     "TTSProviderError", None, None, id="elevenlabs-unknown-model"),
        pytest.param("elevenlabs", _Response(403, body={"detail": {
            "type": "authorization_error", "code": "feature_not_available"}}),
                     "TTSProviderUnavailable", None, None, id="elevenlabs-plan-gate"),
    ],
)
def test_recorded_vendor_failures_map_to_safe_types(
    provider_name, response, failure_type, timing_attribute, timing_value, monkeypatch
):
    import src.jarvis.output.cloud_tts as cloud
    from src.jarvis.output.cloud_tts_vendors import (
        ElevenLabsTTSProvider,
        FishAudioTTSProvider,
    )

    transport = _Transport(response)
    client_type = FishAudioTTSProvider if provider_name == "fish" else ElevenLabsTTSProvider
    if timing_value == "http-date":
        monkeypatch.setattr("src.jarvis.output.cloud_tts_vendors.time.time", lambda: 2_000_000_000.0)

    with pytest.raises(getattr(cloud, failure_type)) as caught:
        _audio(client_type("safe-secret", transport=transport))

    if timing_attribute:
        actual = getattr(caught.value, timing_attribute)
        if timing_value == "http-date":
            expected = datetime(2037, 10, 21, 7, 28, tzinfo=timezone.utc).timestamp() - 2_000_000_000.0
            assert actual == expected
        else:
            assert actual == timing_value
    assert response.closed


@pytest.mark.parametrize("provider_name", ["fish", "elevenlabs"])
def test_missing_credential_is_unavailable_without_a_request(provider_name):
    from src.jarvis.output.cloud_tts import TTSProviderUnavailable
    from src.jarvis.output.cloud_tts_vendors import (
        ElevenLabsTTSProvider,
        FishAudioTTSProvider,
    )

    transport = _Transport(_Response(200, chunks=(b"\x01\x00",)))
    client_type = FishAudioTTSProvider if provider_name == "fish" else ElevenLabsTTSProvider

    with pytest.raises(TTSProviderUnavailable):
        _audio(client_type(None, transport=transport))

    assert transport.requests == []


@pytest.mark.parametrize("provider_name", ["fish", "elevenlabs"])
def test_timeout_is_a_generic_provider_failure(provider_name):
    from src.jarvis.output.cloud_tts import TTSProviderError
    from src.jarvis.output.cloud_tts_vendors import (
        ElevenLabsTTSProvider,
        FishAudioTTSProvider,
    )

    transport = _Transport(requests.Timeout("secret transport details"))
    client_type = FishAudioTTSProvider if provider_name == "fish" else ElevenLabsTTSProvider

    with pytest.raises(TTSProviderError) as caught:
        _audio(client_type("private-key", transport=transport))

    assert type(caught.value) is TTSProviderError
    assert "secret transport details" not in str(caught.value)


@pytest.mark.parametrize("provider_name", ["fish", "elevenlabs"])
def test_truncated_pcm_is_rejected(provider_name):
    from src.jarvis.output.cloud_tts import TTSProviderUnavailable
    from src.jarvis.output.cloud_tts_vendors import (
        ElevenLabsTTSProvider,
        FishAudioTTSProvider,
    )

    transport = _Transport(_Response(200, chunks=(b"\x01\x00", b"\x02")))
    client_type = FishAudioTTSProvider if provider_name == "fish" else ElevenLabsTTSProvider

    with pytest.raises(TTSProviderUnavailable):
        _audio(client_type("private-key", transport=transport))


@pytest.mark.parametrize("provider_name", ["fish", "elevenlabs"])
def test_sensitive_values_never_reach_logs_or_exceptions(provider_name, monkeypatch):
    from src.jarvis.output.cloud_tts import TTSProviderError
    from src.jarvis.output.cloud_tts_vendors import (
        ElevenLabsTTSProvider,
        FishAudioTTSProvider,
    )

    secret = "credential-that-must-stay-private"
    spoken = "spoken text that must stay private"
    endpoint = "https://private.invalid/speech"
    logs = []
    monkeypatch.setattr("src.jarvis.output.cloud_tts_vendors.debug_log", lambda message, area: logs.append(message))
    transport = _Transport(RuntimeError(f"{secret} {spoken} {endpoint}"))
    client_type = FishAudioTTSProvider if provider_name == "fish" else ElevenLabsTTSProvider
    client = client_type(secret, transport=transport)

    with pytest.raises(TTSProviderError) as caught:
        list(client.synthesise(spoken, "voice", "model", 1.0, threading.Event()))

    rendered = "\n".join([str(caught.value), *logs])
    assert secret not in rendered
    assert spoken not in rendered
    assert endpoint not in rendered


def test_default_configuration_orders_fish_then_elevenlabs_then_local():
    from src.jarvis.config import get_default_config

    defaults = get_default_config()

    assert defaults["tts_engine"] == "piper"
    assert [entry["provider"] for entry in defaults["tts_cloud_providers"]] == [
        "fish_audio",
        "elevenlabs",
    ]
    assert [entry["api_key_env"] for entry in defaults["tts_cloud_providers"]] == [
        "FISH_AUDIO_API_KEY",
        "ELEVENLABS_API_KEY",
    ]
    assert defaults["tts_local_fallback_engine"] == "piper"


def test_quota_exhaustion_falls_through_and_blocks_fish_on_next_utterance(tmp_path):
    from src.jarvis.output.cloud_tts import (
        CloudProviderConfig,
        CloudTTS,
        TTSProviderStateStore,
    )
    from src.jarvis.output.cloud_tts_vendors import (
        ElevenLabsTTSProvider,
        FishAudioTTSProvider,
    )

    fish_transport = _Transport(_Response(402, body={
        "status": 402,
        "message": "credits exhausted",
    }))
    eleven_transport = _Transport(
        _Response(200, chunks=(b"\x01\x00",)),
        _Response(200, chunks=(b"\x02\x00",)),
    )
    providers = [
        CloudProviderConfig("Fish Audio", "fish_audio", "FISH_KEY", "fish-voice", "s2.1-pro-free"),
        CloudProviderConfig("ElevenLabs", "elevenlabs", "ELEVEN_KEY", "eleven-voice", "eleven_multilingual_v2"),
    ]
    keys = {"fish_audio": "fish-key", "elevenlabs": "eleven-key"}

    def build(config, api_key):
        assert api_key == keys[config.provider]
        if config.provider == "fish_audio":
            return FishAudioTTSProvider(api_key, transport=fish_transport)
        return ElevenLabsTTSProvider(api_key, transport=eleven_transport)

    class Local:
        def _speak_once(self, utterance):
            raise AssertionError("local fallback should not be needed")

        def interrupt(self):
            pass

        def stop(self):
            pass

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("FISH_KEY", "fish-key")
    monkeypatch.setenv("ELEVEN_KEY", "eleven-key")
    try:
        engine = CloudTTS(
            providers=providers,
            local_engine=Local(),
            state_store=TTSProviderStateStore(tmp_path / "state.json", now=lambda: 2_000_000_000.0),
            provider_factory=build,
        )
        played = []
        engine._play_pcm = lambda pcm, rate, utterance: played.append((pcm, rate, utterance.text))

        engine._speak_once(Utterance("First utterance"))
        engine._speak_once(Utterance("Second utterance"))
    finally:
        monkeypatch.undo()

    assert played == [
        (b"\x01\x00", 24_000, "First utterance"),
        (b"\x02\x00", 24_000, "Second utterance"),
    ]
    assert len(fish_transport.requests) == 1
    assert len(eleven_transport.requests) == 2
