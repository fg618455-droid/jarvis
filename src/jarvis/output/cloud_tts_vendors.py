"""Plain-HTTP Fish Audio and ElevenLabs cloud TTS providers."""

from __future__ import annotations

import time
from datetime import timezone
from email.utils import parsedate_to_datetime
from typing import Any, Iterator
from urllib.parse import quote

import requests

from ..debug import debug_log
from .cloud_tts import (
    CloudProviderConfig,
    CloudTTSProvider,
    TTSAudioChunk,
    TTSAuthenticationError,
    TTSProviderError,
    TTSProviderUnavailable,
    TTSQuotaExhausted,
    TTSRateLimited,
)


_FISH_TTS_ENDPOINT = "https://api.fish.audio/v1/tts"
_ELEVENLABS_TTS_ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream"
_PCM_SAMPLE_RATE = 24_000
_STREAM_CHUNK_SIZE = 8192


def _header(headers: Any, name: str) -> str | None:
    """Read a response header from real or minimal fake mappings."""
    for key, value in getattr(headers, "items", lambda: ())():
        if str(key).lower() == name.lower():
            rendered = str(value).strip()
            return rendered or None
    return None


def _retry_after(headers: Any) -> float | None:
    value = _header(headers, "Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, parsed.timestamp() - time.time())
    except (TypeError, ValueError, OverflowError):
        return None


def _response_json(response: Any) -> dict[str, Any]:
    try:
        body = response.json()
    except (AttributeError, TypeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def _elevenlabs_detail(body: dict[str, Any]) -> tuple[str, str]:
    detail = body.get("detail")
    if not isinstance(detail, dict):
        return "", ""
    error_type = detail.get("type")
    code = detail.get("code")
    if not isinstance(code, str) or not code.strip():
        code = detail.get("status")
    return (
        str(error_type).strip().lower() if isinstance(error_type, str) else "",
        str(code).strip().lower() if isinstance(code, str) else "",
    )


def _raise_fish_failure(response: Any) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    if status == 401:
        raise TTSAuthenticationError("cloud TTS credential was rejected")
    if status == 402:
        raise TTSQuotaExhausted("cloud TTS quota is exhausted")
    if status == 429:
        raise TTSRateLimited(
            "cloud TTS request was rate limited",
            retry_after=_retry_after(getattr(response, "headers", {})),
        )
    raise TTSProviderError("cloud TTS provider rejected the request")


def _raise_elevenlabs_failure(response: Any) -> None:
    status = int(getattr(response, "status_code", 0) or 0)
    error_type, code = _elevenlabs_detail(_response_json(response))
    if (
        status == 402
        or error_type == "payment_required"
        or code in {"insufficient_credits", "quota_exceeded"}
    ):
        raise TTSQuotaExhausted("cloud TTS quota is exhausted")
    if status == 401 or error_type == "authentication_error":
        raise TTSAuthenticationError("cloud TTS credential was rejected")
    if status == 429 or error_type == "rate_limit_error":
        raise TTSRateLimited(
            "cloud TTS request was rate limited",
            retry_after=_retry_after(getattr(response, "headers", {})),
        )
    if code in {"feature_not_available", "subscription_required", "invalid_output_format"}:
        raise TTSProviderUnavailable("required PCM output is unavailable")
    raise TTSProviderError("cloud TTS provider rejected the request")


def _iter_pcm(response: Any, cancelled: Any) -> Iterator[TTSAudioChunk]:
    content_type = _header(getattr(response, "headers", {}), "Content-Type")
    if content_type and "json" in content_type.lower():
        raise TTSProviderUnavailable("cloud TTS response was not PCM audio")

    pending = b""
    yielded = False
    try:
        parts = response.iter_content(chunk_size=_STREAM_CHUNK_SIZE)
        for part in parts:
            if cancelled.is_set():
                return
            if not isinstance(part, (bytes, bytearray, memoryview)):
                raise TTSProviderUnavailable("cloud TTS returned invalid PCM audio")
            block = pending + bytes(part)
            complete_length = len(block) - (len(block) % 2)
            if complete_length:
                yielded = True
                yield TTSAudioChunk.pcm16(block[:complete_length], _PCM_SAMPLE_RATE)
            pending = block[complete_length:]
    except TTSProviderError:
        raise
    except Exception:
        raise TTSProviderError("cloud TTS transport failed") from None

    if pending:
        raise TTSProviderUnavailable("cloud TTS returned truncated PCM audio")
    if not yielded and not cancelled.is_set():
        raise TTSProviderUnavailable("cloud TTS returned empty PCM audio")


class FishAudioTTSProvider:
    """Fish Audio's chunked HTTP TTS endpoint using 24 kHz raw PCM."""

    def __init__(self, api_key: str | None, *, transport: Any = requests) -> None:
        self._api_key = str(api_key or "").strip()
        self._transport = transport

    def synthesise(
        self,
        text: str,
        voice_id: str,
        model: str,
        timeout_sec: float,
        cancelled: Any,
    ) -> Iterator[TTSAudioChunk]:
        if not self._api_key or not voice_id:
            raise TTSProviderUnavailable("cloud TTS provider is not configured")
        response = None
        debug_log("Fish Audio TTS request starting", "tts")
        try:
            response = self._transport.post(
                _FISH_TTS_ENDPOINT,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "model": model,
                },
                json={
                    "text": text,
                    "reference_id": voice_id,
                    "format": "pcm",
                    "sample_rate": _PCM_SAMPLE_RATE,
                },
                stream=True,
                timeout=float(timeout_sec),
            )
            if int(getattr(response, "status_code", 0) or 0) != 200:
                _raise_fish_failure(response)
            yield from _iter_pcm(response, cancelled)
        except TTSProviderError:
            raise
        except Exception:
            raise TTSProviderError("cloud TTS transport failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


class ElevenLabsTTSProvider:
    """ElevenLabs' chunked HTTP TTS endpoint using 24 kHz raw PCM."""

    def __init__(self, api_key: str | None, *, transport: Any = requests) -> None:
        self._api_key = str(api_key or "").strip()
        self._transport = transport

    def synthesise(
        self,
        text: str,
        voice_id: str,
        model: str,
        timeout_sec: float,
        cancelled: Any,
    ) -> Iterator[TTSAudioChunk]:
        if not self._api_key or not voice_id:
            raise TTSProviderUnavailable("cloud TTS provider is not configured")
        response = None
        debug_log("ElevenLabs TTS request starting", "tts")
        try:
            response = self._transport.post(
                _ELEVENLABS_TTS_ENDPOINT.format(voice_id=quote(voice_id, safe="")),
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                params={"output_format": "pcm_24000"},
                json={"text": text, "model_id": model},
                stream=True,
                timeout=float(timeout_sec),
            )
            if int(getattr(response, "status_code", 0) or 0) != 200:
                _raise_elevenlabs_failure(response)
            yield from _iter_pcm(response, cancelled)
        except TTSProviderError:
            raise
        except Exception:
            raise TTSProviderError("cloud TTS transport failed") from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


def create_cloud_tts_provider(
    config: CloudProviderConfig,
    api_key: str | None,
) -> CloudTTSProvider:
    """Build a supported client without resolving or retaining config secrets."""
    provider = config.provider.strip().lower()
    if provider == "fish_audio":
        return FishAudioTTSProvider(api_key)
    if provider == "elevenlabs":
        return ElevenLabsTTSProvider(api_key)
    raise TTSProviderUnavailable("cloud TTS provider is unsupported")
