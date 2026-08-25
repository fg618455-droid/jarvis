"""Ordered cloud speech fallback with a mandatory local final stage."""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol

from ..debug import debug_log
from ..runtime import Phase, set_phase_if
from ..utils.audio_lock import portaudio_lock
from .tts import (
    Utterance,
    _feed_visualizer_waveform,
    _preprocess_for_speech,
    _resolve_output_device,
)


class TTSProviderError(Exception):
    """Base class for safe cloud provider failures."""


class TTSRateLimited(TTSProviderError):
    def __init__(self, message: str = "rate limited", *, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class TTSQuotaExhausted(TTSProviderError):
    def __init__(self, message: str = "quota exhausted", *, reset_at: float | None = None):
        super().__init__(message)
        self.reset_at = reset_at


class TTSAuthenticationError(TTSProviderError):
    pass


class TTSProviderTimeout(TTSProviderError):
    pass


class TTSNetworkError(TTSProviderError):
    pass


class TTSProviderUnavailable(TTSProviderError):
    """The provider or its PCM/WAV response cannot be used."""


@dataclass(frozen=True)
class TTSAudioChunk:
    """One provider-yielded PCM block or one complete WAV response.

    Raw PCM is signed 16-bit little-endian audio. WAV input must contain
    uncompressed, mono, signed 16-bit PCM. Keeping the accepted wire formats
    this narrow avoids an audio decoder dependency in the daemon.
    """

    data: bytes
    format: str
    sample_rate: int | None = None

    @classmethod
    def pcm16(cls, data: bytes, sample_rate: int) -> "TTSAudioChunk":
        return cls(bytes(data), "pcm", int(sample_rate))

    @classmethod
    def wav(cls, data: bytes) -> "TTSAudioChunk":
        return cls(bytes(data), "wav")


@dataclass(frozen=True)
class CloudProviderConfig:
    """Non-secret configuration for one place in the cloud chain."""

    name: str
    provider: str
    api_key_env: str
    voice_id: str
    model: str
    enabled: bool = True
    timeout_sec: float = 10.0


class CloudTTSProvider(Protocol):
    """Network-free contract implemented by an individual vendor client."""

    def synthesise(
        self,
        text: str,
        voice_id: str,
        model: str,
        timeout_sec: float,
        cancelled: threading.Event,
    ) -> Iterator[TTSAudioChunk]:
        """Yield PCM/WAV audio or raise a typed :class:`TTSProviderError`."""


_RUN_INVALID: dict[str, set[str]] = {}
_RUN_INVALID_LOCK = threading.RLock()


def default_tts_provider_state_path() -> Path:
    override = os.environ.get("JARVIS_TTS_PROVIDER_STATE_PATH")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".jarvis" / "tts_provider_state.json"


def provider_state_key(provider: CloudProviderConfig) -> str:
    """Return a stable identifier without exposing its input fields."""
    raw = "\0".join((provider.name, provider.provider, provider.api_key_env))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class TTSProviderStateStore:
    """Atomic store containing provider hashes and health counters only."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        now: Callable[[], float] = time.time,
        run_invalid: set[str] | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_tts_provider_state_path()
        self._now = now
        self._lock = threading.RLock()
        self._data = self._load()
        if run_invalid is None:
            with _RUN_INVALID_LOCK:
                run_invalid = _RUN_INVALID.setdefault(str(self.path.resolve()), set())
        self._run_invalid = run_invalid

    def _load(self) -> dict[str, Any]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and isinstance(raw.get("providers"), dict):
                return raw
        except (OSError, json.JSONDecodeError, TypeError):
            pass
        return {"version": 1, "providers": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, indent=2)
            try:
                temp_path.chmod(0o600)
            except OSError:
                pass
            os.replace(temp_path, self.path)
            try:
                self.path.chmod(0o600)
            except OSError:
                pass
        except OSError:
            try:
                temp_path.unlink()
            except OSError:
                pass
            raise

    def _persist(self) -> None:
        try:
            self._save()
        except OSError as error:
            debug_log(f"Cloud TTS provider state could not be saved: {type(error).__name__}", "tts")

    def _entry(self, provider: CloudProviderConfig) -> dict[str, Any]:
        return self._data["providers"].setdefault(provider_state_key(provider), {
            "blocked_until": 0.0,
            "hits": 0,
            "failures": 0,
            "last_error": "",
            "rate_limits": 0,
        })

    def is_blocked(self, provider: CloudProviderConfig) -> bool:
        with self._lock:
            blocked_until = float(self._entry(provider).get("blocked_until", 0.0) or 0.0)
            return blocked_until > self._now()

    def is_invalid_for_run(self, provider: CloudProviderConfig) -> bool:
        with _RUN_INVALID_LOCK:
            return provider_state_key(provider) in self._run_invalid

    def mark_invalid_for_run(self, provider: CloudProviderConfig) -> None:
        with _RUN_INVALID_LOCK:
            self._run_invalid.add(provider_state_key(provider))

    def record_hit(self, provider: CloudProviderConfig) -> None:
        with self._lock:
            entry = self._entry(provider)
            entry["hits"] = int(entry.get("hits", 0)) + 1
            entry["last_error"] = ""
            self._persist()

    def record_failure(
        self,
        provider: CloudProviderConfig,
        error: BaseException | str,
    ) -> None:
        with self._lock:
            entry = self._entry(provider)
            entry["failures"] = int(entry.get("failures", 0)) + 1
            entry["last_error"] = error if isinstance(error, str) else type(error).__name__
            now = self._now()
            if isinstance(error, TTSRateLimited):
                count = int(entry.get("rate_limits", 0)) + 1
                entry["rate_limits"] = count
                if error.retry_after is not None:
                    delay = max(0.0, float(error.retry_after))
                else:
                    delay = (60.0, 300.0, 900.0)[min(count - 1, 2)]
                entry["blocked_until"] = now + delay
            elif isinstance(error, TTSQuotaExhausted):
                reset = error.reset_at
                if reset is None or float(reset) <= now:
                    current = datetime.fromtimestamp(now, tz=timezone.utc)
                    reset_dt = (current + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    reset = reset_dt.timestamp()
                entry["blocked_until"] = float(reset)
            self._persist()


ProviderFactory = Callable[[CloudProviderConfig, str | None], CloudTTSProvider]


def _default_provider_client(
    config: CloudProviderConfig,
    api_key: str | None,
) -> CloudTTSProvider:
    from .cloud_tts_vendors import create_cloud_tts_provider

    return create_cloud_tts_provider(config, api_key)


class CloudTTS:
    """TTS engine that walks cloud providers before a local final stage."""

    def __init__(
        self,
        *,
        providers: Iterable[CloudProviderConfig],
        local_engine: Any,
        enabled: bool = True,
        output_device: str | None = None,
        state_store: TTSProviderStateStore | None = None,
        provider_factory: ProviderFactory = _default_provider_client,
    ) -> None:
        self.enabled = enabled
        self._providers = tuple(providers)
        self.local_engine = local_engine
        self._output_device = _resolve_output_device(output_device)
        self._state = state_store or TTSProviderStateStore()
        self._provider_factory = provider_factory
        self._clients: dict[CloudProviderConfig, CloudTTSProvider] = {}

        self._q: queue.Queue[Utterance] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._is_speaking = threading.Event()
        self._last_spoken_text = ""
        self._should_interrupt = threading.Event()
        self._audio_stream = None
        self._audio_lock = threading.Lock()

    def start(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._thread is not None:
            try:
                self.interrupt()
            except Exception:
                pass
            self._stop.set()
            try:
                self._q.put_nowait(Utterance(""))
            except Exception:
                pass
            self._thread.join(timeout=2.0)
            self._thread = None
            self._stop.clear()
        try:
            self.local_engine.stop()
        except Exception as error:
            debug_log(f"Cloud TTS local engine stop failed: {type(error).__name__}", "tts")

    def speak(
        self,
        text: str,
        completion_callback: Callable[[], None] | None = None,
        duration_callback: Callable[[float], None] | None = None,
        audio_start_callback: Callable[[], None] | None = None,
    ) -> None:
        if not self.enabled or not text.strip():
            return
        if self._thread is None:
            self.start()
        processed_text = _preprocess_for_speech(text)
        if not processed_text.strip():
            return
        try:
            self._q.put_nowait(Utterance(
                text=processed_text,
                completion_callback=completion_callback,
                duration_callback=duration_callback,
                audio_start_callback=audio_start_callback,
            ))
        except Exception:
            pass

    def end_of_reply(self, completion_callback: Callable[[], None] | None = None) -> None:
        if not self.enabled:
            return
        if self._thread is None:
            self.start()
        try:
            self._q.put_nowait(Utterance("", completion_callback=completion_callback))
        except Exception:
            pass

    def interrupt(self) -> None:
        self._should_interrupt.set()
        try:
            self.local_engine.interrupt()
        except Exception:
            pass
        with self._audio_lock:
            if self._audio_stream is not None:
                try:
                    with portaudio_lock:
                        self._audio_stream.abort()
                except Exception:
                    pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                utterance = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if utterance.is_end_of_reply:
                self._finish_reply(utterance)
                continue
            try:
                self._speak_once(utterance)
            except Exception as error:
                debug_log(f"Cloud TTS worker failure: {type(error).__name__}", "tts")

    def _finish_reply(self, utterance: Utterance) -> None:
        if utterance.completion_callback is not None:
            debug_log("Cloud TTS reached the end of a streamed reply", "tts")
            try:
                utterance.completion_callback()
            except Exception as error:
                debug_log(f"Cloud TTS end-of-reply callback error: {type(error).__name__}", "tts")
        self._finish_speech_phase()

    def _client(self, config: CloudProviderConfig) -> CloudTTSProvider:
        client = self._clients.get(config)
        if client is None:
            api_key = os.environ.get(config.api_key_env) if config.api_key_env else None
            client = self._provider_factory(config, api_key)
            self._clients[config] = client
        return client

    @staticmethod
    def _wav_pcm(data: bytes) -> tuple[bytes, int]:
        try:
            with wave.open(io.BytesIO(data), "rb") as source:
                if (
                    source.getcomptype() != "NONE"
                    or source.getnchannels() != 1
                    or source.getsampwidth() != 2
                    or source.getframerate() <= 0
                ):
                    raise TTSProviderUnavailable("WAV must be mono 16-bit PCM")
                return source.readframes(source.getnframes()), source.getframerate()
        except (EOFError, wave.Error) as error:
            raise TTSProviderUnavailable("invalid WAV audio") from error

    def _collect_pcm(
        self,
        chunks: Iterable[TTSAudioChunk],
    ) -> tuple[bytes, int]:
        blocks: list[bytes] = []
        sample_rate: int | None = None
        for chunk in chunks:
            if self._should_interrupt.is_set():
                return b"", 0
            if not isinstance(chunk, TTSAudioChunk):
                raise TTSProviderUnavailable("provider yielded an unsupported audio object")
            if chunk.format == "wav":
                block, block_rate = self._wav_pcm(chunk.data)
            elif chunk.format == "pcm":
                block = chunk.data
                block_rate = int(chunk.sample_rate or 0)
                if block_rate <= 0 or len(block) % 2:
                    raise TTSProviderUnavailable("invalid PCM audio")
            else:
                raise TTSProviderUnavailable("only PCM and WAV audio are supported")
            if not block:
                continue
            if sample_rate is None:
                sample_rate = block_rate
            elif block_rate != sample_rate:
                raise TTSProviderUnavailable("audio sample rate changed between chunks")
            blocks.append(block)
        return b"".join(blocks), int(sample_rate or 0)

    def _failed(self, config: CloudProviderConfig, error: BaseException | str) -> None:
        if isinstance(error, TTSAuthenticationError):
            self._state.mark_invalid_for_run(config)
        self._state.record_failure(config, error)
        label = error if isinstance(error, str) else type(error).__name__
        debug_log(f"Cloud TTS provider {config.name!r} failed ({label}); falling through", "tts")

    def _speak_once(self, utterance: Utterance) -> None:
        self._is_speaking.set()
        self._last_spoken_text = utterance.text
        self._should_interrupt.clear()
        self._notify_speaking_state(True)
        try:
            for config in self._providers:
                if self._should_interrupt.is_set():
                    return
                if not config.enabled:
                    continue
                if self._state.is_invalid_for_run(config):
                    debug_log(f"Cloud TTS provider {config.name!r} skipped for this process", "tts")
                    continue
                if self._state.is_blocked(config):
                    debug_log(f"Cloud TTS provider {config.name!r} skipped during cooldown", "tts")
                    continue

                debug_log(f"Cloud TTS selecting provider {config.name!r}", "tts")
                try:
                    chunks = self._client(config).synthesise(
                        utterance.text,
                        config.voice_id,
                        config.model,
                        config.timeout_sec,
                        self._should_interrupt,
                    )
                    pcm, sample_rate = self._collect_pcm(chunks)
                    if self._should_interrupt.is_set():
                        return
                    if not pcm:
                        self._failed(config, "EmptyAudio")
                        continue
                    self._play_pcm(pcm, sample_rate, utterance)
                    if self._should_interrupt.is_set():
                        return
                except TTSProviderError as error:
                    self._failed(config, error)
                    continue
                except Exception as error:
                    safe_error = TTSProviderUnavailable(type(error).__name__)
                    self._failed(config, safe_error)
                    continue
                self._state.record_hit(config)
                self._complete_utterance(utterance)
                return

            if self._should_interrupt.is_set():
                return
            debug_log("Cloud TTS selecting mandatory local fallback", "tts")
            self._speak_locally(utterance)
        finally:
            self._is_speaking.clear()
            self._notify_speaking_state(False)
            self._finish_speech_phase()

    def _speak_locally(self, utterance: Utterance) -> None:
        try:
            self.local_engine._speak_once(utterance)
        except Exception as error:
            debug_log(f"Cloud TTS local fallback failed: {type(error).__name__}", "tts")
            print("  ⚠️ Cloud TTS: local fallback failed; skipping utterance", flush=True)
            self._complete_utterance(utterance)

    def _play_pcm(self, pcm: bytes, sample_rate: int, utterance: Utterance) -> None:
        import numpy as np
        import sounddevice as sd

        audio = np.frombuffer(pcm, dtype="<i2")
        if len(audio) == 0:
            raise TTSProviderUnavailable("empty PCM audio")
        duration = len(audio) / sample_rate
        if utterance.duration_callback is not None:
            try:
                utterance.duration_callback(duration)
            except Exception as error:
                debug_log(f"Cloud TTS duration callback error: {type(error).__name__}", "tts")

        play_position = [0]

        def audio_callback(outdata, frames, time_info, status):
            del time_info, status
            if self._should_interrupt.is_set():
                raise sd.CallbackAbort()
            start = play_position[0]
            end = start + frames
            block = audio[start:end]
            if len(block) < frames:
                outdata[:len(block), 0] = block
                outdata[len(block):, 0] = 0
                raise sd.CallbackStop()
            outdata[:, 0] = block
            _feed_visualizer_waveform(block)
            play_position[0] = end

        with self._audio_lock:
            with portaudio_lock:
                self._audio_stream = sd.OutputStream(
                    samplerate=sample_rate,
                    channels=1,
                    dtype="int16",
                    blocksize=1024,
                    callback=audio_callback,
                    device=self._output_device,
                )
                self._audio_stream.start()
        self._notify_audio_start(utterance)
        try:
            while self._audio_stream is not None and self._audio_stream.active:
                if self._should_interrupt.wait(0.05):
                    with self._audio_lock:
                        if self._audio_stream is not None:
                            with portaudio_lock:
                                self._audio_stream.abort()
                    return
        finally:
            with self._audio_lock:
                if self._audio_stream is not None:
                    try:
                        with portaudio_lock:
                            self._audio_stream.close()
                    except Exception:
                        pass
                    self._audio_stream = None

    @staticmethod
    def _complete_utterance(utterance: Utterance) -> None:
        if utterance.completion_callback is not None:
            try:
                utterance.completion_callback()
            except Exception as error:
                debug_log(f"Cloud TTS completion callback error: {type(error).__name__}", "tts")

    @staticmethod
    def _notify_audio_start(utterance: Utterance) -> None:
        if utterance.audio_start_callback is not None:
            try:
                utterance.audio_start_callback()
            except Exception as error:
                debug_log(f"Cloud TTS audio-start callback error: {type(error).__name__}", "tts")

    def _finish_speech_phase(self) -> None:
        if not self._q.empty():
            return
        set_phase_if(Phase.SPEAKING, Phase.IDLE)
        set_phase_if(Phase.THINKING, Phase.IDLE)

    @staticmethod
    def _notify_speaking_state(is_speaking: bool) -> None:
        try:
            from desktop_app.face_widget import JarvisState, get_jarvis_state

            if is_speaking:
                debug_log("setting face state to SPEAKING (cloud)", "tts")
                get_jarvis_state().set_state(JarvisState.SPEAKING)
        except ImportError:
            debug_log("face widget not available (ImportError) (cloud)", "tts")
        except Exception as error:
            debug_log(f"failed to set face state to SPEAKING (cloud): {type(error).__name__}", "tts")

    def is_speaking(self) -> bool:
        return self._is_speaking.is_set()

    def get_last_spoken_text(self) -> str:
        return self._last_spoken_text
