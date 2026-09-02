"""
Voice Listener - Main orchestrator for voice capture and processing.

Coordinates audio capture, speech recognition, echo detection, and state management.
"""

from __future__ import annotations
import functools
import os
import threading
import time
import queue
import sys
import platform
from collections import deque
from typing import Any, Callable, Optional, TYPE_CHECKING
from datetime import datetime

from rapidfuzz import fuzz
from contextlib import contextmanager

from .audio_ingress import register_audio_sink
from .conversation_mode import register_conversation_controller
from .passive_capture import (
    capture_evicted_segment,
    register_passive_buffer,
)
from .echo_detection import EchoDetector
from .state_manager import StateManager
from ..utils.audio_lock import portaudio_lock
from .wake_detection import (
    is_wake_word_detected,
    extract_query_after_wake,
    extract_query_from_edge_wake,
)
from .transcript_buffer import TranscriptBuffer
from .intent_judge import (
    IntentJudge,
    _is_low_power_mode_enabled,
    create_intent_judge,
    warm_up_chat_model,
)
from ..config import resolve_transcription_language
from ..debug import debug_log
from ..llm import get_embedding_backend
from ..reply.engine import warm_up_reply_prefix
from ..runtime import Phase, get_recorder, get_runtime_state, set_phase, set_phase_if
from ..utils.location import is_location_available

if TYPE_CHECKING:
    from ..memory.db import Database
    from ..memory.conversation import DialogueMemory


def is_whisper_hallucination(no_speech_prob: float, threshold: float) -> bool:
    """Shared Whisper no-speech gate.

    Whisper can report high `avg_logprob` confidence on hallucinated phrases
    when the audio is silent or noise. `no_speech_prob` is an independent
    signal and must be checked first. Used by both the faster-whisper path
    (`_filter_noisy_segments`) and the MLX path (`_finalize_utterance`) so
    both backends apply identical policy.
    """
    return no_speech_prob >= threshold


def is_uncertain_language(language_probability, threshold: float) -> bool:
    """Language-identification gate for whole utterances.

    Whisper invents short filler phrases from room noise. Those carry a
    `no_speech_prob` of zero and a healthy `avg_logprob`, so neither existing
    filter sees them. Language identification does: Whisper is confident about
    the language of real speech and hesitant about noise.

    The gate compares only the reported probability, never the language itself,
    so it stays valid for every language the assistant supports.

    Fails open. A missing or malformed probability means "cannot judge", and
    discarding what the user actually said is worse than passing noise through.
    A threshold of 0.0 disables the gate.
    """
    if threshold <= 0.0:
        return False
    if not isinstance(language_probability, (int, float)) or isinstance(language_probability, bool):
        return False
    return language_probability < threshold


# Audio processing imports (optional).
#
# Kept apart on purpose: only sounddevice needs PortAudio. A machine with no
# working audio device still transcribes audio posted by the control centre,
# so nulling numpy and the VAD alongside it would disable a path that has no
# device to fail on.
try:
    import numpy as np
except ImportError:
    np = None

try:
    import webrtcvad
except ImportError:
    webrtcvad = None

try:
    import sounddevice as sd
except ImportError as e:
    sd = None
    # Log import error for debugging
    print(f"  ⚠️  Audio import error: {e}", flush=True)
    print("     This may indicate PortAudio is not found", flush=True)
    import sys as _sys
    if _sys.platform == 'linux':
        print("     On Linux, ensure PortAudio is installed: sudo apt install libportaudio2", flush=True)
    del _sys
except OSError as e:
    # PortAudio loading errors appear as OSError
    sd = None
    print(f"  ❌ PortAudio initialisation failed: {e}", flush=True)
    print("     Please reinstall the application or check audio drivers", flush=True)
    import sys as _sys
    if _sys.platform == 'linux':
        print("     On Linux, ensure PortAudio is installed: sudo apt install libportaudio2", flush=True)
    del _sys

# Whisper backend imports - try MLX first on Apple Silicon, fall back to faster-whisper
MLX_WHISPER_AVAILABLE = False
FASTER_WHISPER_AVAILABLE = False

def _is_apple_silicon() -> bool:
    """Check if running on Apple Silicon Mac."""
    return sys.platform == "darwin" and platform.machine() == "arm64"


def _get_mic_permission_hint() -> str:
    """Return platform-appropriate microphone permission guidance."""
    if sys.platform == 'win32':
        return "Windows Settings > Privacy > Microphone > Allow apps to access"
    elif sys.platform == 'darwin':
        return "System Settings > Privacy & Security > Microphone"
    else:
        return "`pactl list sources` or audio settings for your desktop environment"

def _resample(audio, src_rate: int, dst_rate: int):
    """Resample a 1-D float32 numpy array from *src_rate* to *dst_rate*.

    Uses linear interpolation — fast and good enough for speech going into Whisper.
    """
    if src_rate == dst_rate or np is None:
        return audio
    ratio = dst_rate / src_rate
    n_out = int(len(audio) * ratio)
    indices = np.arange(n_out) / ratio
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def _setup_nvidia_dll_path() -> None:
    """Add NVIDIA CUDA DLL directories to PATH on Windows.

    The pip packages nvidia-cublas-cu12 and nvidia-cudnn-cu12 install DLLs
    under site-packages/nvidia/*/bin/ which isn't on PATH by default.
    PyInstaller bundles place them in {app}/cuda/. This function finds
    both locations and prepends them to PATH so ctypes.CDLL can find them.
    """
    import os

    dirs_to_add = []

    # 1. Check for NVIDIA pip packages in site-packages
    try:
        import nvidia.cublas  # type: ignore[import-untyped]
        for pkg_path in nvidia.cublas.__path__:
            bin_dir = os.path.join(pkg_path, "bin")
            if os.path.isdir(bin_dir):
                dirs_to_add.append(bin_dir)
    except (ImportError, AttributeError):
        pass

    try:
        import nvidia.cudnn  # type: ignore[import-untyped]
        for pkg_path in nvidia.cudnn.__path__:
            bin_dir = os.path.join(pkg_path, "bin")
            if os.path.isdir(bin_dir):
                dirs_to_add.append(bin_dir)
    except (ImportError, AttributeError):
        pass

    # 2. Check for CUDA DLLs in app directory (installed by install_cuda.ps1)
    # For frozen apps: check next to the executable (not _MEIPASS, since
    # CUDA libs are downloaded post-install, not bundled in the archive)
    if getattr(sys, "frozen", False):
        app_dir = os.path.dirname(sys.executable)
    else:
        app_dir = None

    if app_dir:
        cuda_dir = os.path.join(app_dir, "cuda")
        if os.path.isdir(cuda_dir):
            dirs_to_add.append(cuda_dir)

    # 3. Register DLL directories (must happen before ctypes.CDLL probes)
    # Use both os.add_dll_directory (for ctypes.CDLL) and PATH (for
    # subprocess/child processes). On Windows, PATH changes after process
    # start don't affect ctypes.CDLL search — add_dll_directory is needed.
    if dirs_to_add:
        current_path = os.environ.get("PATH", "")
        new_entries = os.pathsep.join(dirs_to_add)
        os.environ["PATH"] = new_entries + os.pathsep + current_path
        for d in dirs_to_add:
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            debug_log(f"added NVIDIA DLL path: {d}", "voice")


@functools.lru_cache(maxsize=None)
def _probe_cuda_available() -> tuple[bool, list[str]]:
    """Probe cuBLAS + cuDNN availability once per process and cache the result.

    The version ranges intentionally span more than the currently pinned
    versions in `installer/windows/install_cuda.ps1` (`cublas64_12.dll`,
    `cudnn_ops64_9.dll`) so a future installer bump doesn't silently fall
    back to CPU until this probe is updated too. A bump outside the
    existing range still requires widening these ranges — the relationship
    is by convention, not enforced.

    Cached because DLLs don't appear or disappear while the process is
    running, and the scan does up to 18 `LoadLibrary` calls on a miss.
    """
    _setup_nvidia_dll_path()

    missing_libs: list[str] = []
    cublas_found = False
    cudnn_found = False
    try:
        import ctypes

        for ver in range(20, 10, -1):
            try:
                ctypes.CDLL(f"cublas64_{ver}.dll")
                cublas_found = True
                debug_log(f"cuBLAS found (cublas64_{ver}.dll)", "voice")
                break
            except OSError:
                continue
        if not cublas_found:
            missing_libs.append("cuBLAS")

        for ver in range(15, 7, -1):
            try:
                ctypes.CDLL(f"cudnn_ops64_{ver}.dll")
                cudnn_found = True
                debug_log(f"cuDNN found (cudnn_ops64_{ver}.dll)", "voice")
                break
            except OSError:
                continue
        if not cudnn_found:
            missing_libs.append("cuDNN")
    except Exception as e:
        debug_log(f"CUDA library probe failed: {e}", "voice")

    return cublas_found and cudnn_found, missing_libs


def _probe_windows_cuda_libraries(device: str) -> tuple[str, list[str]]:
    """Return the device to use and any missing CUDA lib names.

    Short-circuits on non-Windows or non-CUDA device strings. Otherwise
    delegates to the cached `_probe_cuda_available()` so the expensive DLL
    scan only runs once per process lifetime.
    """
    if sys.platform != "win32" or device not in ("auto", "cuda"):
        return device, []

    available, missing_libs = _probe_cuda_available()
    if not available:
        return "cpu", missing_libs
    return device, []


def _print_cuda_unavailable_hint(missing_libs: list[str]) -> None:
    """Print the user-facing CUDA-missing message and recovery hint.

    The hint deliberately points at the tray action, not at "reinstall the
    app". The Inno Setup task only fires once and skips on stale marker
    files, so reinstalling without first deleting `{app}\\cuda` rarely
    fixes the underlying problem. The tray action re-runs install_cuda.ps1
    directly with UAC, which is the actual recovery path.
    """
    debug_log(f"CUDA libraries missing: {missing_libs}, forcing CPU mode", "voice")
    print("  ℹ️  CUDA not available, using CPU mode", flush=True)
    if missing_libs:
        print(f"     Missing: {', '.join(missing_libs)}", flush=True)
    print(
        "  💡 For GPU acceleration, click 'Reinstall GPU libraries' in the Jarvis tray menu",
        flush=True,
    )


try:
    if _is_apple_silicon():
        import mlx_whisper
        MLX_WHISPER_AVAILABLE = True
except Exception:
    mlx_whisper = None

try:
    from faster_whisper import WhisperModel
    FASTER_WHISPER_AVAILABLE = True
except Exception:
    # Catch broad: the faster-whisper import chain can raise ValueError
    # (e.g. "psutil.__spec__ is not set") in some environments.
    WhisperModel = None


def _is_faster_whisper_turbo_supported() -> bool:
    """Check if the installed faster-whisper supports the large-v3-turbo model."""
    try:
        import faster_whisper
        from packaging.version import Version
        return Version(faster_whisper.__version__) >= Version("1.1.0")
    except Exception:
        return False


def _get_mlx_model_repo(model_name: str) -> str:
    """Get the MLX Community HuggingFace repo for a Whisper model."""
    # Map standard model names to MLX Community repos
    model_map = {
        "tiny": "mlx-community/whisper-tiny-mlx",
        "tiny.en": "mlx-community/whisper-tiny.en-mlx",
        "base": "mlx-community/whisper-base-mlx",
        "base.en": "mlx-community/whisper-base.en-mlx",
        "small": "mlx-community/whisper-small-mlx",
        "small.en": "mlx-community/whisper-small.en-mlx",
        "medium": "mlx-community/whisper-medium-mlx",
        "medium.en": "mlx-community/whisper-medium.en-mlx",
        "large": "mlx-community/whisper-large-v3-mlx",
        "large-v2": "mlx-community/whisper-large-v2-mlx",
        "large-v3": "mlx-community/whisper-large-v3-mlx",
        "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    }
    return model_map.get(model_name, f"mlx-community/whisper-{model_name}-mlx")


def _clear_corrupted_whisper_cache(error_message: str) -> bool:
    """Clear a corrupted Whisper model cache directory.

    Parses the CTranslate2 error message to find the snapshot directory,
    then deletes the parent ``models--`` directory so the model can be
    re-downloaded cleanly (including blobs that may also be corrupt).

    Returns ``True`` if a cache directory was found and deleted.
    """
    import re
    import shutil

    # CTranslate2 error format:
    #   "Unable to open file 'model.bin' in model '/path/to/snapshots/hash'"
    match = re.search(
        r"unable to open file\s+'[^']+'\s+in model\s+'([^']+)'",
        error_message,
        re.IGNORECASE,
    )
    if not match:
        debug_log("could not parse cache path from error message", "voice")
        return False

    snapshot_path = match.group(1)

    # Walk up to the models-- directory
    # snapshot_path is e.g. .../models--Org--Name/snapshots/<hash>
    # We want to delete .../models--Org--Name entirely
    from pathlib import Path
    path = Path(snapshot_path)
    model_dir = None
    for parent in [path] + list(path.parents):
        if parent.name.startswith("models--"):
            model_dir = parent
            break

    if model_dir is None or not model_dir.is_dir():
        debug_log(f"could not locate models-- cache directory from: {snapshot_path}", "voice")
        return False

    try:
        shutil.rmtree(model_dir)
        debug_log(f"cleared corrupted Whisper cache: {model_dir}", "voice")
        return True
    except OSError as e:
        debug_log(f"failed to clear corrupted cache: {e}", "voice")
        return False



@contextmanager
def _serialised_stream(stream):
    """Like ``with stream:`` but with lifecycle calls under portaudio_lock.

    sounddevice's context manager calls start() on enter and stop()/close()
    on exit; those are the thread-unsafe PortAudio lifecycle operations that
    must be serialised process-wide (see jarvis.utils.audio_lock).

    ``None`` means there is no local device to serialise, which is the normal
    shape when the microphone lives in the browser. The block still runs.
    """
    if stream is None:
        yield None
        return
    with portaudio_lock:
        stream.start()
    try:
        yield stream
    finally:
        with portaudio_lock:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass


class _SpeechSegmentSink:
    """Speaks a reply sentence by sentence as the engine writes it.

    A reply is written faster than it is spoken, so the engine hands over each
    finished sentence instead of the whole answer at the end. Three things
    only make sense once per reply rather than once per sentence, and this is
    what keeps them that way:

    - The felt wait ends when sound first leaves the speakers, so only the
      first sentence carries the callback that says so.
    - Echo suppression matches what the microphone hears against what was
      said, so the detector is given the reply accumulated so far. Handing it
      one sentence at a time would leave it able to recognise only the last.
    - Whether anything was spoken at all decides how the caller closes the
      reply, so the sink remembers.
    """

    def __init__(self, listener: "VoiceListener",
                 on_audio_start: Optional[Callable[[], None]] = None) -> None:
        self._listener = listener
        self._pending_audio_start = on_audio_start
        self._spoken: list[str] = []

    @property
    def anything_spoken(self) -> bool:
        return bool(self._spoken)

    @property
    def spoken_text(self) -> str:
        return " ".join(self._spoken)

    def __call__(self, sentence: str) -> None:
        tts = self._listener.tts
        if not (tts and tts.enabled):
            return
        audio_start, self._pending_audio_start = self._pending_audio_start, None
        self._spoken.append(sentence)
        self._listener.track_tts_start(self.spoken_text)
        debug_log(f"speaking streamed sentence ({len(sentence)} chars)", "tts")
        tts.speak(
            sentence,
            completion_callback=None,
            audio_start_callback=audio_start,
        )


class VoiceListener(threading.Thread):
    """Main voice listening thread that orchestrates all voice processing."""

    def __init__(self, db: "Database", cfg, tts: Optional[Any],
                 dialogue_memory: "DialogueMemory"):
        """
        Initialise voice listener.

        Args:
            db: Database instance for storage
            cfg: Configuration object
            tts: Text-to-speech engine (optional)
            dialogue_memory: Dialogue memory instance
        """
        super().__init__(daemon=True)

        self.db = db
        self.cfg = cfg
        self.tts = tts
        self.dialogue_memory = dialogue_memory
        self._should_stop = False
        self._dictation_active = False  # Pause flag set by dictation engine
        # Whether the local microphone is being captured. Decided in run();
        # False means every frame arrives from the control centre instead.
        self._local_capture = False
        self._first_utterance = True  # Suppress turn separator before the very first transcription
        # ISO-639-1 code Whisper detected for the most recent utterance.
        # Updated at every successful transcription site (MLX + faster-
        # whisper) and consumed by `_dispatch_query` so downstream tools
        # can pick locale-appropriate resources (e.g. tr.wikipedia.org).
        # One-utterance-at-a-time voice flow means the read in
        # `_dispatch_query` always matches the write from the Whisper
        # call that produced the transcript.
        self._last_detected_language: Optional[str] = None

        # Audio processing components
        self._whisper_backend: Optional[str] = None  # "mlx" or "faster-whisper"
        self._whisper_device: Optional[str] = None  # "cpu" or "cuda" (resolved from CTranslate2)
        self._mlx_model_repo: Optional[str] = None  # For MLX backend
        self.model: Optional[Any] = None  # WhisperModel for faster-whisper, None for MLX
        self.transcribe_lock = threading.Lock()  # Shared lock for Whisper model access
        self._audio_q: queue.Queue = queue.Queue(maxsize=64)
        self._pre_roll: deque = deque()

        # Audio callback monitoring (for debugging)
        self._callback_count = 0
        self._last_callback_log_time = 0

        # Voice activity detection
        self.is_speech_active = False
        self._silence_frames = 0
        self._utterance_frames: list = []
        self._frame_samples = 0
        self._samplerate = int(getattr(self.cfg, "sample_rate", 16000))
        self._vad: Optional = None

        # Initialise VAD if available
        if webrtcvad is not None and bool(getattr(self.cfg, "vad_enabled", True)):
            try:
                self._vad = webrtcvad.Vad(int(getattr(self.cfg, "vad_aggressiveness", 2)))
            except Exception:
                self._vad = None

        # Initialise modular components
        self.echo_detector = EchoDetector(
            echo_tolerance=float(getattr(self.cfg, "echo_tolerance", 0.3))
        )

        self.state_manager = StateManager(
            hot_window_seconds=float(getattr(self.cfg, "hot_window_seconds", 3.0)),
            echo_tolerance=float(getattr(self.cfg, "echo_tolerance", 0.3)),
            command_capture_seconds=float(
                getattr(self.cfg, "wake_command_timeout_seconds", 12.0)
            ),
        )

        # Audio-level wake word detection timestamp
        self._wake_timestamp: Optional[float] = None

        # Rolling transcript buffer for context-aware processing
        # Used for both retention and context passed to intent judge
        self._buffer_duration = float(getattr(self.cfg, "transcript_buffer_duration_sec", 120.0))
        self._transcript_buffer = TranscriptBuffer(max_duration_sec=self._buffer_duration)
        self._transcript_buffer.set_eviction_sink(
            lambda segment: capture_evicted_segment(self.db, self.cfg, segment)
        )
        register_passive_buffer(self._transcript_buffer)
        debug_log(f"transcript buffer initialised ({self._buffer_duration}s)", "voice")

        # Intent judge (full context, larger model) - always used when available
        self._intent_judge = create_intent_judge(self.cfg)
        if self._intent_judge is not None:
            debug_log(f"intent judge initialised (model: {self._intent_judge.config.model})", "voice")
        else:
            debug_log("intent judge unavailable, using simple wake word detection", "voice")

        # Thinking tune player
        self._tune_player: Optional = None

    def stop(self) -> None:
        """Stop the voice listener."""
        self._should_stop = True
        register_conversation_controller(None)
        register_audio_sink(None)
        self._transcript_buffer.flush()
        self._transcript_buffer.set_eviction_sink(None)
        register_passive_buffer(None)
        self.state_manager.stop()
        self._stop_thinking_tune()

    def _start_thinking_tune(self) -> None:
        """Start the thinking tune when processing a query."""
        if (self.cfg.tune_enabled and
            self._tune_player is None and
            (self.tts is None or not self.tts.is_speaking())):
            from ..output.tune_player import TunePlayer
            self._tune_player = TunePlayer(enabled=True)
            self._tune_player.start_tune()

    def _stop_thinking_tune(self) -> None:
        """Stop the thinking tune and revert face state to IDLE."""
        if self._tune_player is not None:
            self._tune_player.stop_tune()
            self._tune_player = None
            try:
                from desktop_app.face_widget import get_jarvis_state, JarvisState
                get_jarvis_state().set_state(JarvisState.IDLE)
            except ImportError:
                pass
            except Exception:
                pass

    def _is_thinking_tune_active(self) -> bool:
        """Check if thinking tune is currently active."""
        return self._tune_player is not None and self._tune_player.is_playing()

    def _set_face_state_listening(self) -> None:
        """Set the desktop face widget to LISTENING state."""
        try:
            from desktop_app.face_widget import get_jarvis_state, JarvisState
            get_jarvis_state().set_state(JarvisState.LISTENING)
        except ImportError:
            pass
        except Exception as e:
            debug_log(f"failed to set face state to LISTENING: {e}", "voice")

    def track_tts_start(self, tts_text: str) -> None:
        """Called when TTS starts speaking."""
        if self.tts and self.tts.enabled:
            # Playback is a closed listening interval. Drop anything captured
            # before the output stream starts so speaker audio cannot be
            # transcribed later as a delayed command.
            self._clear_audio_buffers()

            self.echo_detector.track_tts_start(tts_text)

    def activate_hot_window(self) -> None:
        """Activate hot window after TTS completion."""
        debug_log("TTS completed, checking hot window activation", "voice")

        if not self.cfg.hot_window_enabled:
            debug_log("hot window disabled in config, skipping", "voice")
            return

        # Track TTS finish time for echo detection
        self.echo_detector.track_tts_finish()

        # Schedule delayed hot window activation
        debug_log(f"scheduling hot window activation (echo_tolerance={self.state_manager.echo_tolerance}s, hot_window={self.state_manager.hot_window_seconds}s)", "voice")
        self.state_manager.schedule_hot_window_activation(self.cfg.voice_debug)

    def _process_transcript(self, text: str, utterance_energy: float = 0.0, utterance_start_time: float = 0.0, utterance_end_time: float = 0.0) -> None:
        """
        Process a transcript from speech recognition.

        Args:
            text: Transcribed text from audio
            utterance_energy: Pre-calculated energy from the utterance frames
        """
        if not text or not text.strip():
            # Check hot window expiry
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        text_lower = text.strip().lower()

        # Reset wake timestamp — it must reflect only the current utterance.
        # If this utterance contains a wake word, the early-beep check below
        # will set it. Without this reset, a prior rejected wake-worded
        # utterance would vouch for subsequent unrelated utterances via the
        # `_wake_timestamp is not None` guard in the intent-judge accept path.
        self._wake_timestamp = None

        start_time_str = datetime.fromtimestamp(utterance_start_time).strftime('%H:%M:%S.%f')[:-3] if utterance_start_time > 0 else "N/A"
        end_time_str = datetime.fromtimestamp(utterance_end_time).strftime('%H:%M:%S.%f')[:-3] if utterance_end_time > 0 else "N/A"
        debug_log(f"heard: '{text}' (utterance from {start_time_str} to {end_time_str})", "voice")

        received_during_tts = self.tts and self.tts.is_speaking()

        # The microphone callback is suspended throughout playback. This
        # defensive guard also discards a transcript that completed across the
        # playback boundary. Follow-ups begin after TTS in the hot window.
        if received_during_tts:
            debug_log("discarding transcript captured during TTS playback", "voice")
            self._transcript_buffer.mark_last_segment_echo()
            return

        in_hot_window = self.state_manager.was_speech_during_hot_window(
            utterance_start_time, utterance_end_time
        )

        # --- Early echo check + early beep ---
        # Check for echo BEFORE starting beep and BEFORE intent judge.
        # This prevents: false beeps on echo, intent judge blocking the audio
        # loop for seconds on echo, and hot window extending from echo resets.
        if not self._is_thinking_tune_active():
            if in_hot_window:
                # Fuzzy echo check — instant, no intent judge needed.
                # Only catches pure echo (transcript ≈ TTS text). Mixed
                # echo+speech chunks (user spoke over echo) go to the
                # intent judge which can extract the user's speech.
                last_tts_text = self.echo_detector._last_tts_text or ""
                if last_tts_text:
                    echo_score = fuzz.partial_ratio(
                        text_lower, last_tts_text.lower()
                    )
                    tts_words = len(last_tts_text.split())
                    text_words = len(text_lower.split())
                    is_pure_echo = (
                        echo_score >= 70
                        and text_words <= max(tts_words * 1.3, tts_words + 3)
                    )
                    if is_pure_echo:
                        # Before rejecting, try to salvage user speech appended
                        # after the echo prefix. Whisper commonly merges the tail
                        # of TTS echo with the user's follow-up into a single
                        # transcript; without salvage, the user's real speech
                        # would be dropped before the intent judge ever sees it.
                        # Try exact-word cleanup first (cheapest, most precise),
                        # then fall back to the rightmost-boundary scan which
                        # handles Whisper mis-transcriptions at the echo/speech
                        # join ("explores" → "laws") that exact matching can't.
                        salvaged = self.echo_detector.cleanup_leading_echo(text_lower)
                        if salvaged == text_lower:
                            salvaged_alt = self.echo_detector.salvage_after_echo_tail(text_lower)
                            if salvaged_alt:
                                salvaged = salvaged_alt
                        # Require ≥ min_salvage_words to avoid treating Whisper's
                        # echo-tail hallucinations ("…regions like Steneti") as
                        # genuine user speech. The threshold lives on the echo
                        # detector so every salvage site shares one policy.
                        min_words = self.echo_detector.min_salvage_words
                        if (salvaged != text_lower
                                and len(salvaged.split()) >= min_words):
                            debug_log(
                                f"salvaged user speech from hot-window echo+speech "
                                f"chunk: '{salvaged}'",
                                "voice",
                            )
                            print(
                                f"  ✂️ Stripped echo prefix, kept: \"{salvaged[:60]}"
                                f"{'...' if len(salvaged) > 60 else ''}\"",
                                flush=True,
                            )
                            self._transcript_buffer.update_last_segment_text(salvaged)
                            self._transcript_buffer.mark_last_segment_echo()
                            # text_lower now carries the salvaged query — the rest
                            # of _process_transcript reads from this variable.
                            text_lower = salvaged
                        else:
                            debug_log(f"🔇 Early echo rejection (score={echo_score}): \"{text_lower}\"", "voice")
                            print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                            self._transcript_buffer.mark_last_segment_echo()
                            return

                # Non-echo (or salvaged) in hot window — start beep
                self._start_thinking_tune()
                self._set_face_state_listening()
                debug_log("early beep: hot window active", "voice")
            else:
                # Not in hot window — check for wake word
                wake_word = getattr(self.cfg, "wake_word", "jarvis")
                aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
                fuzzy_ratio = float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78))
                if is_wake_word_detected(text_lower, wake_word, aliases, fuzzy_ratio):
                    self._wake_timestamp = utterance_start_time
                    self._start_thinking_tune()
                    self._set_face_state_listening()
                    debug_log("early beep: wake word detected", "voice")

        # A wake name at either utterance edge is an unambiguous address. VAD
        # has already established the utterance boundary, so dispatch it
        # directly instead of paying for an intent-judge round trip and a
        # second silence collection window. Interior occurrences remain with
        # the contextual judge because the name may be the subject.
        if not in_hot_window:
            wake_word = getattr(self.cfg, "wake_word", "jarvis")
            aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
            direct_query = extract_query_from_edge_wake(
                text_lower,
                wake_word,
                aliases,
                float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78)),
            )
            if direct_query:
                debug_log("edge wake address accepted without intent judge", "voice")
                self.state_manager.cancel_hot_window_activation()
                self._transcript_buffer.mark_segment_processed(text_lower)
                self._clear_audio_buffers()
                self._begin_reply(direct_query)
                return
            if direct_query == "":
                debug_log("standalone wake word accepted for one-request capture", "voice")
                self._transcript_buffer.mark_segment_processed(text_lower)
                self._clear_audio_buffers()
                self._begin_command_capture()
                return

        # Salvage user speech from post-playback echo+speech chunks. Speaker
        # tails can remain in the acoustic path during the short tolerance
        # interval, so strip a matching prefix and keep the user's suffix.
        # Skip entirely if there's no prior TTS — nothing to match against.
        last_tts_text_for_salvage = self.echo_detector._last_tts_text or ""
        last_tts_finish = self.echo_detector._last_tts_finish_time or 0.0
        # Use echo_tolerance as buffer — speaker/mic latency means the utterance
        # may start slightly after TTS finish yet still contain the echo.
        echo_tol = self.echo_detector.echo_tolerance
        if (last_tts_text_for_salvage and last_tts_finish > 0
                and utterance_start_time > 0
                and utterance_start_time < last_tts_finish + echo_tol):
            salvaged = self.echo_detector._salvage_suffix_from_echo(
                text_lower,
                getattr(self.cfg, 'tts_rate', 200),
                utterance_start_time,
            )
            # If the prefix-based salvage fails or truncates too aggressively
            # (Whisper-mangled echo boundary → exact cleanup misses; fuzzy
            # prefix iteration prefers shortest suffix), fall through to the
            # rightmost-boundary scan which recovers the full follow-up.
            boundary_salvaged = self.echo_detector.salvage_after_echo_tail(text_lower)
            if boundary_salvaged and (
                salvaged is None or salvaged == text_lower
                or len(boundary_salvaged.split()) > len(salvaged.split())
            ):
                salvaged = boundary_salvaged
            min_words = self.echo_detector.min_salvage_words
            if (salvaged and salvaged.strip() and salvaged != text_lower
                    and len(salvaged.split()) >= min_words):
                debug_log(f"salvaged user speech from merged echo+speech chunk: '{salvaged}'", "voice")
                self._transcript_buffer.update_last_segment_text(salvaged)
                self._transcript_buffer.mark_last_segment_echo()
                text_lower = salvaged

        # Check hot window expiry
        self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)

        # Intent judge — the decision-maker for contextual wake usage and
        # post-TTS follow-ups. Explicit edge addresses have already dispatched.
        # Gets full transcript context, last TTS text, and hot window state.
        # Handles: echo detection, wake word queries, hot window follow-ups.
        intent_judgment = None

        # Determine if this could be a hot window follow-up.
        # Only use formal hot window state — no time-based grace period.
        # The state manager already handles the timing (echo_tolerance
        # delay before activation, hot_window_seconds before expiry).
        # A generous grace period caused false hot window claims after
        # the user had already seen "Returning to wake word mode".
        could_be_hot_window = self.state_manager.was_speech_during_hot_window(
            utterance_start_time, utterance_end_time
        )

        # Gate the intent judge on an engagement signal. Without this check the
        # judge was called on every ambient utterance, blocking the audio loop
        # for up to `timeout_sec` on each background chatter — which could
        # cascade into UI freezes when many utterances queued up during a slow
        # or loaded Ollama. The judge adds value only when one of:
        #   1. A wake word was detected in the current utterance
        #   2. We are in (or pending) a hot window following TTS
        has_engagement_signal = (
            self._wake_timestamp is not None
            or could_be_hot_window
        )

        if not has_engagement_signal:
            debug_log(
                f"skipping intent judge — no wake word, no hot window, no TTS "
                f"(ambient: \"{text_lower[:40]}{'...' if len(text_lower) > 40 else ''}\")",
                "voice",
            )

        if (
            has_engagement_signal
            and self._intent_judge is not None
            and self._intent_judge.available
        ):
            # Get recent transcript segments for context (full buffer)
            context_segments = self._transcript_buffer.get_last_seconds(self._buffer_duration)

            # Get TTS context for echo detection
            last_tts_text = self.echo_detector._last_tts_text or ""
            last_tts_finish_time = self.echo_detector._last_tts_finish_time or 0.0

            intent_judgment = self._intent_judge.judge(
                segments=context_segments,
                wake_timestamp=self._wake_timestamp,
                last_tts_text=last_tts_text,
                last_tts_finish_time=last_tts_finish_time,
                in_hot_window=could_be_hot_window,
                current_text=text_lower,
            )

            if (
                intent_judgment is not None
                and intent_judgment.stop
                and self.state_manager.is_conversation_active
            ):
                # The judge decides what counts as asking Jarvis to stop, so
                # ending a conversation works in any language without a list
                # of stop words to maintain.
                debug_log(
                    f"conversation ended by request: \"{text_lower[:50]}\"", "voice"
                )
                self._stop_thinking_tune()
                self._transcript_buffer.mark_segment_processed(text_lower)
                self._clear_audio_buffers()
                self.state_manager.end_conversation()
                return

            if (
                intent_judgment is not None
                and intent_judgment.conversation_mode
                and self.state_manager.is_command_capture_active
            ):
                self._stop_thinking_tune()
                self._transcript_buffer.mark_segment_processed(text_lower)
                self._clear_audio_buffers()
                self.state_manager.start_conversation()
                self._announce_conversation_mode()
                return

            if intent_judgment is not None:
                # Log intent judge decision for user visibility
                if self.state_manager.is_conversation_active:
                    mode_str = "conversation"
                elif could_be_hot_window:
                    mode_str = "hot window"
                else:
                    mode_str = "wake word"
                if intent_judgment.directed:
                    print(f"  🧠 Intent ({mode_str}): directed → \"{intent_judgment.query or text_lower}\"", flush=True)
                else:
                    print(f"  🧠 Intent ({mode_str}): not directed ({intent_judgment.reasoning})", flush=True)
            else:
                reason = self._intent_judge.last_failure_reason or "no segments or unavailable"
                print(f"  🧠 Intent judge: unavailable ({reason})", flush=True)
                debug_log(f"intent judge returned None — falling back ({reason})", "voice")
                # Hot window fallback: if the early echo check already cleared
                # this text, accept it even without the judge's verdict.
                if could_be_hot_window:
                    last_tts_text_fb = self.echo_detector._last_tts_text or ""
                    is_pure_echo = False
                    if last_tts_text_fb:
                        echo_score = fuzz.partial_ratio(
                            text_lower, last_tts_text_fb.lower()
                        )
                        tts_words = len(last_tts_text_fb.split())
                        text_words = len(text_lower.split())
                        is_pure_echo = (
                            echo_score >= 70
                            and text_words <= max(tts_words * 1.3, tts_words + 3)
                        )
                    if not is_pure_echo:
                        print(f"  🧠 Intent fallback: accepting hot window speech", flush=True)
                        debug_log(f"✅ Hot window fallback (judge unavailable): \"{text_lower}\"", "voice")
                        self.state_manager.cancel_hot_window_activation()
                        self._transcript_buffer.mark_segment_processed(text_lower)
                        self._clear_audio_buffers()
                        self._begin_reply(text_lower)
                        return

            if intent_judgment is not None:
                # If directed with query, process it
                if intent_judgment.directed and intent_judgment.query:
                    # In wake word mode, verify the wake word is actually present
                    # The LLM sometimes hallucinates wake words that don't exist
                    if not could_be_hot_window:
                        wake_word = getattr(self.cfg, "wake_word", "jarvis")
                        aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
                        has_wake_word = self._wake_timestamp is not None or is_wake_word_detected(
                            text_lower, wake_word, aliases
                        )
                        if not has_wake_word:
                            print(f"  🧠 Intent override: no wake word found, ignoring", flush=True)
                            debug_log(
                                f"⚠️ Intent judge said directed but no wake word found in '{text_lower[:50]}...' "
                                f"(reasoning: {intent_judgment.reasoning})",
                                "voice"
                            )
                            # Don't accept - fall through to wake word check
                        else:
                            debug_log(f"✅ Intent judge accepted ({intent_judgment.confidence}): \"{intent_judgment.query}\"", "voice")
                            self.state_manager.cancel_hot_window_activation()
                            self._transcript_buffer.mark_segment_processed(text_lower)
                            self._clear_audio_buffers()
                            self._begin_reply(intent_judgment.query)
                            return
                    else:
                        # Hot window mode - no wake word needed, but check for echo.
                        # The mic can pick up Jarvis's own TTS output and Whisper
                        # transcribes it as user speech. Check fuzzy similarity.
                        # Only reject PURE echo — if the heard text is significantly
                        # longer than TTS, it contains user speech mixed with echo
                        # and the intent judge's extraction should be used instead.
                        judge_query = (intent_judgment.query or "").strip()
                        if last_tts_text:
                            echo_score = fuzz.partial_ratio(
                                text_lower, last_tts_text.lower()
                            )
                            query_echo_score = (
                                fuzz.partial_ratio(
                                    judge_query.lower(), last_tts_text.lower()
                                )
                                if judge_query else 0
                            )
                            if query_echo_score >= 70 and echo_score < 70:
                                debug_log(
                                    "intent judge query matched the previous TTS while "
                                    "the heard text did not; using heard text",
                                    "voice",
                                )
                                judge_query = ""
                            tts_words = len(last_tts_text.split())
                            text_words = len(text_lower.split())
                            is_pure_echo = (
                                echo_score >= 70
                                and text_words <= max(tts_words * 1.3, tts_words + 3)
                            )
                            if is_pure_echo:
                                # Also check judge's extracted query — if it matches
                                # TTS too, it's genuinely pure echo. If the query is
                                # different, the judge extracted real user speech.
                                if query_echo_score >= 70:
                                    debug_log(f"🔇 Echo in hot window (directed, score={echo_score}): \"{text_lower}\"", "voice")
                                    print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                                    self._stop_thinking_tune()
                                    self._transcript_buffer.mark_last_segment_echo()
                                    return
                                else:
                                    debug_log(
                                        f"echo in text (score={echo_score}) but judge extracted "
                                        f"non-echo query: \"{intent_judgment.query}\"", "voice"
                                    )

                        # The intent judge is explicitly designed to prune echo
                        # and extract the actual user query — always prefer its
                        # output when present. Falling back to raw heard text
                        # leaks partially-salvaged echo fragments into tool
                        # calls (e.g. "…amount now? okay, what is his best
                        # song?" reaching webSearch verbatim). If the judge
                        # returns an empty query (rare), fall back to raw text.
                        hot_query = judge_query or text_lower
                        if judge_query and judge_query.lower() != text_lower:
                            debug_log(
                                f"using judge query over heard text: "
                                f"\"{judge_query}\" (heard: \"{text_lower[:80]}\")",
                                "voice",
                            )
                        debug_log(f"✅ Intent judge accepted ({intent_judgment.confidence}): \"{hot_query}\"", "voice")
                        self.state_manager.cancel_hot_window_activation()
                        self._transcript_buffer.mark_segment_processed(text_lower)
                        self._clear_audio_buffers()

                        self._begin_reply(hot_query)
                        return

                # If directed with high confidence but no extracted query, use actual text
                # Per spec: "Hot window input should reflect what the user actually said"
                # This handles cases where intent judge correctly identifies directed speech
                # but fails to extract/synthesize a query (e.g., conversational follow-ups)
                if intent_judgment.directed and intent_judgment.confidence == "high":
                    # In wake word mode, verify the wake word is actually present
                    if not could_be_hot_window:
                        wake_word = getattr(self.cfg, "wake_word", "jarvis")
                        aliases = list(set(getattr(self.cfg, "wake_aliases", [])) | {wake_word})
                        has_wake_word = self._wake_timestamp is not None or is_wake_word_detected(
                            text_lower, wake_word, aliases
                        )
                        if not has_wake_word:
                            print(f"  🧠 Intent override: no wake word found, ignoring", flush=True)
                            debug_log(
                                f"⚠️ Intent judge said directed (no query) but no wake word in '{text_lower[:50]}...'",
                                "voice"
                            )
                            # Fall through to wake word check
                        else:
                            debug_log(f"✅ Intent judge accepted (directed, high confidence, using actual text): \"{text_lower}\"", "voice")
                            self.state_manager.cancel_hot_window_activation()
                            self._transcript_buffer.mark_segment_processed(text_lower)
                            self._clear_audio_buffers()
                            self._begin_reply(text_lower)
                            return
                    else:
                        # Hot window — echo check before accepting
                        # Only reject pure echo (similar word count to TTS)
                        if last_tts_text:
                            echo_score = fuzz.partial_ratio(
                                text_lower, last_tts_text.lower()
                            )
                            tts_words = len(last_tts_text.split())
                            text_words = len(text_lower.split())
                            is_pure_echo = (
                                echo_score >= 70
                                and text_words <= max(tts_words * 1.3, tts_words + 3)
                            )
                            if is_pure_echo:
                                debug_log(f"🔇 Echo in hot window (directed/no-query, score={echo_score}): \"{text_lower}\"", "voice")
                                print(f"  🔇 Heard (echo): \"{text_lower[:50]}{'...' if len(text_lower) > 50 else ''}\"", flush=True)
                                self._stop_thinking_tune()
                                self._transcript_buffer.mark_last_segment_echo()
                                return

                        debug_log(f"✅ Intent judge accepted (directed, high confidence, using actual text): \"{text_lower}\"", "voice")
                        self.state_manager.cancel_hot_window_activation()
                        self._transcript_buffer.mark_segment_processed(text_lower)
                        self._clear_audio_buffers()
                        self._begin_reply(text_lower)
                        return

                # If not directed with high confidence, check reasoning before rejecting
                if not intent_judgment.directed and intent_judgment.confidence == "high":
                    # Surgical fix: If intent judge claims "echo" but echo system already cleared
                    # this utterance (we reached here, meaning Priority 2 didn't reject), don't
                    # trust the LLM's echo reasoning - fall through to wake word detection instead.
                    # The echo system does actual text similarity matching; the LLM sometimes
                    # hallucinates echo matches that don't exist.
                    reasoning_lower = (intent_judgment.reasoning or "").lower()
                    if "echo" in reasoning_lower:
                        debug_log(
                            f"⚠️ Intent judge claimed echo but echo system cleared - "
                            f"checking if near hot window: \"{text_lower}\"",
                            "voice"
                        )
                        # Check if utterance started shortly after hot window expired
                        # This catches cases where user started speaking just as hot window expired
                        # Use a 2-second grace period after the 3-second hot window
                        hot_window_grace = 2.0
                        last_tts_finish = self.echo_detector._last_tts_finish_time or 0.0
                        hot_window_end = last_tts_finish + self.state_manager.hot_window_seconds
                        time_after_hot_window = utterance_start_time - hot_window_end if utterance_start_time > 0 and hot_window_end > 0 else float('inf')

                        if 0 <= time_after_hot_window < hot_window_grace:
                            # Utterance started within grace period after hot window
                            debug_log(
                                f"✅ Accepting as directed: started {time_after_hot_window:.2f}s after hot window expired",
                                "voice"
                            )
                            self.state_manager.cancel_hot_window_activation()

                            # Mark the current segment as processed to prevent re-extraction
                            self._transcript_buffer.mark_segment_processed(text_lower)

                            self._clear_audio_buffers()
                            self._begin_reply(text_lower)
                            return

                        # Check the recorded hot-window span as a second timing
                        # source when the timer has expired before transcription.
                        if could_be_hot_window:
                            # Verify it's not pure echo before overriding
                            echo_score = 0
                            is_pure_echo = False
                            if last_tts_text:
                                echo_score = fuzz.partial_ratio(
                                    text_lower, last_tts_text.lower()
                                )
                                tts_words = len(last_tts_text.split())
                                text_words = len(text_lower.split())
                                is_pure_echo = (
                                    echo_score >= 70
                                    and text_words <= max(tts_words * 1.3, tts_words + 3)
                                )
                            if is_pure_echo:
                                debug_log(f"🔇 Echo in hot window (echo reasoning confirmed, score={echo_score}): \"{text_lower}\"", "voice")
                                self._stop_thinking_tune()
                                self._transcript_buffer.mark_last_segment_echo()
                                return
                            # Mixed echo+speech — override the echo reasoning
                            print(f"  🧠 Intent override: accepting hot window speech (mixed echo+speech)", flush=True)
                            debug_log(
                                f"⚡ Overriding echo reasoning in hot window "
                                f"(echo_score={echo_score}, text longer than TTS): "
                                f"\"{text_lower}\"",
                                "voice"
                            )
                            self.state_manager.cancel_hot_window_activation()
                            self._transcript_buffer.mark_segment_processed(text_lower)
                            self._clear_audio_buffers()
                            self._begin_reply(text_lower)
                            return

                        # Otherwise fall through to wake word detection
                        debug_log(f"⏭️ Not near hot window ({time_after_hot_window:.2f}s after), falling through to wake word check", "voice")
                        # Continue to wake word detection below
                    else:
                        # Check if text is pure echo of TTS output
                        echo_score = 0
                        is_pure_echo = False
                        if last_tts_text:
                            echo_score = fuzz.partial_ratio(
                                text_lower, last_tts_text.lower()
                            )
                            tts_words = len(last_tts_text.split())
                            text_words = len(text_lower.split())
                            is_pure_echo = (
                                echo_score >= 70
                                and text_words <= max(tts_words * 1.3, tts_words + 3)
                            )

                        if could_be_hot_window and is_pure_echo:
                            # Confirmed pure echo — early check should have caught
                            # this, but handle as safety net.
                            debug_log(f"🔇 Echo in hot window (score={echo_score}): \"{text_lower}\"", "voice")
                            self._stop_thinking_tune()
                            self._transcript_buffer.mark_last_segment_echo()
                            return

                        if could_be_hot_window:
                            # Hot window + non-echo speech → user is talking to us.
                            # Override the intent judge rejection — small models
                            # sometimes reject valid follow-ups like "don't you
                            # already know that?" as not directed.
                            print(f"  🧠 Intent override: accepting hot window speech", flush=True)
                            debug_log(
                                f"⚡ Overriding intent judge in hot window "
                                f"(echo_score={echo_score}, reasoning={intent_judgment.reasoning}): "
                                f"\"{text_lower}\"",
                                "voice"
                            )
                            self.state_manager.cancel_hot_window_activation()
                            self._transcript_buffer.mark_segment_processed(text_lower)
                            self._clear_audio_buffers()
                            self._begin_reply(text_lower)
                            return

                        # Outside hot window — check if wake word is actually present
                        # before trusting the rejection. Small models sometimes
                        # classify wake-worded statements ("the light is bright,
                        # Jarvis") as "not directed" despite the prompt instructing
                        # otherwise. When the wake word is present, fall through to
                        # Priority 4 wake word detection as a safety net.
                        ww_wake = getattr(self.cfg, "wake_word", "jarvis")
                        ww_aliases = set(getattr(self.cfg, "wake_aliases", [])) | {ww_wake}
                        has_real_wake = is_wake_word_detected(text_lower, ww_wake, list(ww_aliases))
                        if has_real_wake:
                            debug_log(
                                f"⚠️ Intent judge rejected wake-worded utterance "
                                f"(reasoning: {intent_judgment.reasoning}) — "
                                f"falling through to wake word detection",
                                "voice"
                            )
                            # Fall through to Priority 4: wake word detection
                        else:
                            debug_log(f"🚫 Intent judge rejected (not directed, high confidence): \"{text_lower}\"", "voice")
                            self._stop_thinking_tune()
                            return
                else:
                    # For inconclusive results, fall through to wake word detection
                    debug_log(f"⏭️ Intent judge inconclusive ({intent_judgment.confidence}), checking wake word", "voice")

        # Priority 4: Wake word detection (fallback when intent judge unavailable/inconclusive)
        wake_word = getattr(self.cfg, "wake_word", "jarvis")
        aliases = set(getattr(self.cfg, "wake_aliases", [])) | {wake_word}
        fuzzy_ratio = float(getattr(self.cfg, "wake_fuzzy_ratio", 0.78))

        wake_detected = is_wake_word_detected(text_lower, wake_word, list(aliases), fuzzy_ratio)
        debug_log(f"wake word check: '{wake_word}' in '{text_lower}' → {wake_detected}", "voice")

        if wake_detected:
            # Cancel any pending hot window activation when new query starts
            self.state_manager.cancel_hot_window_activation()

            # Mark the current segment as processed to prevent re-extraction
            self._transcript_buffer.mark_segment_processed(text_lower)

            # Clear audio buffers to prevent concatenation issues
            self._clear_audio_buffers()

            query_fragment = extract_query_after_wake(text_lower, wake_word, list(aliases))
            self._begin_reply(query_fragment)
            return

        # Priority 5: Non-wake input (ignore)
        # Provide clear debug info about why input was ignored
        intent_info = ""
        if intent_judgment is not None:
            intent_info = f", intent={intent_judgment.directed}/{intent_judgment.confidence}"

        # Stop any early-started beep since we're not processing this input
        self._stop_thinking_tune()

        debug_log(f"input ignored (no wake word{intent_info}): {text_lower}", "voice")

    def _begin_reply(self, query: str) -> None:
        """Acknowledge and synchronously dispatch one completed utterance."""
        query = query.strip()
        if not query:
            return
        self.state_manager.end_command_capture()
        self._start_thinking_tune()
        try:
            print(f"\n✨ Working on it: {query}", flush=True)
        except Exception:
            pass
        self._dispatch_query(query)

    def _begin_command_capture(self) -> None:
        """Acknowledge a standalone wake word and wait for one request."""
        self._stop_thinking_tune()
        self.state_manager.start_command_capture()
        acknowledgement = str(getattr(self.cfg, "wake_acknowledgement", "") or "").strip()
        if not acknowledgement:
            debug_log("wake acknowledgement has no configured speech", "voice")
            return
        if self.tts and self.tts.enabled:
            self.track_tts_start(acknowledgement)
            self.tts.speak(
                acknowledgement,
                completion_callback=self.echo_detector.track_tts_finish,
            )
        else:
            print(f"👂 {acknowledgement}", flush=True)

    def _announce_conversation_mode(self) -> None:
        """Confirm that wake-word-free conversation is active."""
        acknowledgement = str(
            getattr(self.cfg, "conversation_mode_acknowledgement", "") or ""
        ).strip()
        if not acknowledgement:
            return
        if self.tts and self.tts.enabled:
            self.track_tts_start(acknowledgement)
            self.tts.speak(
                acknowledgement,
                completion_callback=self.echo_detector.track_tts_finish,
            )
        else:
            print(f"💬 {acknowledgement}", flush=True)

    def _speech_segment_sink(
        self,
        on_audio_start: Optional[Callable[[], None]] = None,
    ) -> "_SpeechSegmentSink":
        """Return a sink that speaks each finished sentence as it arrives."""
        return _SpeechSegmentSink(self, on_audio_start)

    def _dispatch_query(self, query: str) -> None:
        """
        Dispatch a complete query to the reply engine.

        Args:
            query: Complete user query to process
        """
        debug_log(f"dispatching query: '{query}'", "voice")

        # Clear audio buffers to prevent stale audio from next query
        self._clear_audio_buffers()

        recorder = get_recorder()
        trace = recorder.current()
        if trace is None:
            # Reached from a path that did not open a turn (a query assembled
            # across several utterances). Time it from here rather than not
            # at all.
            trace = recorder.begin(source="voice")
        trace.transcript = trace.transcript or query
        set_phase(Phase.THINKING)

        # Set face state to THINKING
        try:
            from desktop_app.face_widget import get_jarvis_state, JarvisState
            state_manager = get_jarvis_state()
            state_manager.set_state(JarvisState.THINKING)
            debug_log("face state set to THINKING (dispatch_query)", "voice")
        except Exception as e:
            debug_log(f"failed to set face state to THINKING: {e}", "voice")

        # Import reply engine
        from ..reply.engine import run_reply_engine
        from ..daemon import query_lock

        def _memory_lookup_started() -> None:
            phrase = str(
                getattr(self.cfg, "memory_lookup_acknowledgement", "") or ""
            ).strip()
            if phrase and self.tts and self.tts.enabled:
                try:
                    self.tts.speak(phrase)
                except Exception as exc:
                    debug_log(
                        f"memory acknowledgement TTS failed: {type(exc).__name__}",
                        "voice",
                    )

        # The turn ends when sound starts, not when the reply text exists:
        # synthesis is part of the wait. Runs on the TTS worker thread, so it
        # holds the trace rather than looking it up. With a streamed reply the
        # first sentence is spoken while the rest is still being written, so
        # this fires before the engine has returned.
        speech_begun = time.perf_counter()

        # A turn is finished once its reply text is known *and* sound has
        # started, because both are part of what the user waited for. Which
        # of the two lands first depends on whether the reply was streamed,
        # so neither side closes the turn alone: whichever arrives second
        # does. `_on_audio_start` runs on the TTS worker thread, so the join
        # is guarded.
        turn_done = threading.Lock()
        turn_state = {"reply": None, "audio_started": False, "finished": False}

        def _finish_turn_if_ready():
            with turn_done:
                if turn_state["finished"]:
                    return
                if turn_state["reply"] is None or not turn_state["audio_started"]:
                    return
                turn_state["finished"] = True
                reply_text = turn_state["reply"]
            recorder.finish(reply=reply_text, trace=trace)

        def _on_audio_start():
            trace.mark(
                "tts_synth",
                (time.perf_counter() - speech_begun) * 1000.0,
            )
            set_phase(Phase.SPEAKING)
            with turn_done:
                turn_state["audio_started"] = True
            _finish_turn_if_ready()

        # Speak each sentence the engine finishes rather than waiting for the
        # whole answer. This is the difference between a reply that starts
        # after the model has stopped writing and one that starts while it is
        # still going.
        speak_segment = self._speech_segment_sink(on_audio_start=_on_audio_start)

        # Process the query (keep thinking tune playing during processing).
        # Hold the shared voice+text query lock so a voice query and a text
        # chat query cannot run the reply engine concurrently against the
        # same dialogue memory. Voice blocks while a text query finishes
        # rather than being dropped (see daemon.query_lock).
        try:
            with query_lock():
                reply = run_reply_engine(
                    self.db, self.cfg, None, query, self.dialogue_memory,
                    language=self._last_detected_language,
                    on_memory_lookup_started=_memory_lookup_started,
                    on_speech_segment=speak_segment,
                )
        except Exception as e:
            # Log the error visibly - this should never happen silently
            print(f"\n  ❌ Reply engine error: {e}", flush=True)
            debug_log(f"reply engine exception: {e}", "voice")
            get_runtime_state().record_error(f"reply engine failed: {e}")
            recorder.finish(error=str(e))
            self._stop_thinking_tune()

            # Provide user feedback via TTS
            if self.tts and self.tts.enabled:
                self.tts.speak("Sorry, I encountered an error processing your request.")
            return

        # Handle TTS with proper callbacks
        if reply and self.tts and self.tts.enabled:
            # Stop thinking tune when TTS starts
            self._stop_thinking_tune()

            # TTS completion callback for hot window
            def _on_tts_complete():
                import time as _time
                debug_log(f"TTS completion callback triggered at {_time.time():.3f}", "voice")
                self.activate_hot_window()

            # Duration callback to update echo detector with exact timing (Piper only)
            def _on_duration_known(duration: float):
                debug_log(f"TTS exact duration: {duration:.2f}s", "voice")
                if self.echo_detector:
                    self.echo_detector._tts_exact_duration = duration

            if speak_segment.anything_spoken:
                # The reply is already on its way out. All that is left is to
                # mark where it ends, so the hot window opens after the last
                # sentence rather than after the first.
                debug_log(
                    f"streamed reply spoken in {len(speak_segment.spoken_text)} chars",
                    "voice",
                )
                self.tts.end_of_reply(completion_callback=_on_tts_complete)
            else:
                # Nothing was streamed: the backend could not stream, or the
                # reply was withheld from the speech path. Speak it whole.
                self.track_tts_start(reply)
                debug_log(f"starting TTS for reply ({len(reply)} chars)", "voice")
                self.tts.speak(reply, completion_callback=_on_tts_complete,
                              duration_callback=_on_duration_known,
                              audio_start_callback=_on_audio_start)
            with turn_done:
                turn_state["reply"] = reply
            _finish_turn_if_ready()
        else:
            debug_log(f"no TTS output: reply={bool(reply)}, tts={bool(self.tts)}, enabled={getattr(self.tts, 'enabled', False) if self.tts else False}", "voice")
            # Stop thinking tune if no TTS response
            self._stop_thinking_tune()
            recorder.finish(reply=reply)
            set_phase_if(Phase.THINKING, Phase.IDLE)

    def request_security_confirmation(
        self,
        action_name: str,
        action_args: dict[str, Any],
        challenge: str,
        timeout_seconds: int,
    ) -> str | None:
        """Speak a numeric challenge and transcribe one bounded response."""
        if np is None or self.tts is None or not self.tts.enabled:
            raise RuntimeError("Voice confirmation output is unavailable")
        if self._whisper_backend == "faster-whisper" and self.model is None:
            raise RuntimeError("Voice confirmation transcription is unavailable")
        if self._whisper_backend == "mlx" and not self._mlx_model_repo:
            raise RuntimeError("Voice confirmation transcription is unavailable")

        self._stop_thinking_tune()
        deadline = time.monotonic() + timeout_seconds
        spoken = threading.Event()
        self.tts.speak(
            f"{action_name}. {' '.join(challenge)}",
            completion_callback=spoken.set,
        )
        while time.monotonic() < deadline and not spoken.wait(0.05):
            if not self.tts.is_speaking():
                break
        if self.tts.is_speaking():
            debug_log("voice security prompt exceeded confirmation timeout", "security")
            return None

        self._clear_audio_buffers()
        energy_threshold = float(getattr(self.cfg, "voice_min_energy", 0.02))
        endpoint_seconds = float(getattr(self.cfg, "endpoint_silence_ms", 800)) / 1000.0
        captured: list[Any] = []
        speech_started = False
        silence_seconds = 0.0

        debug_log(f"voice security capture started for {action_name}", "security")
        while time.monotonic() < deadline:
            try:
                item = self._audio_q.get(timeout=min(0.2, max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                continue
            if item is None:
                continue
            try:
                mono = item.reshape(-1, item.shape[-1])[:, 0] if item.ndim > 1 else item.flatten()
                energy = float(np.sqrt(np.mean(np.square(mono)))) if mono.size else 0.0
            except Exception:
                continue

            duration = mono.size / self._samplerate
            if energy >= energy_threshold:
                speech_started = True
                silence_seconds = 0.0
                captured.append(mono.copy())
            elif speech_started:
                captured.append(mono.copy())
                silence_seconds += duration
                if silence_seconds >= endpoint_seconds:
                    break

        if not captured:
            debug_log("voice security capture timed out without speech", "security")
            return None
        audio = np.concatenate(captured).flatten()
        transcript = self._transcribe_security_audio(audio)
        debug_log("voice security capture transcribed", "security")
        return transcript

    def _transcribe_security_audio(self, audio) -> str:
        """Transcribe confirmation audio with the listener's loaded backend."""
        language = resolve_transcription_language(self.cfg)
        if self._whisper_backend == "mlx":
            with self.transcribe_lock:
                result = mlx_whisper.transcribe(
                    audio,
                    path_or_hf_repo=self._mlx_model_repo,
                    language=language,
                )
            return str(result.get("text") or "").strip()

        vad_filter = bool(getattr(self.cfg, "whisper_vad", True))
        with self.transcribe_lock:
            try:
                segments, _ = self.model.transcribe(
                    audio, language=language, vad_filter=vad_filter
                )
            except TypeError:
                segments, _ = self.model.transcribe(audio, language=language)
            return " ".join(
                str(getattr(segment, "text", "")).strip()
                for segment in segments
                if str(getattr(segment, "text", "")).strip()
            )

    def _calculate_audio_energy(self, frames: list) -> float:
        """Calculate RMS energy from audio frames."""
        if not frames or np is None:
            return 0.0
        try:
            audio_data = np.concatenate(frames)
            rms = float(np.sqrt(np.mean(np.square(audio_data))))
            return rms
        except Exception:
            return 0.0

    def _clear_audio_buffers(self) -> None:
        """Clear all audio buffers and reset speech state.

        Call this on state transitions to prevent old audio from being
        incorrectly concatenated with new input.
        """
        self._utterance_frames = []
        self._pre_roll.clear()
        self.is_speech_active = False
        self._silence_frames = 0

        # Clear wake detection state
        self._wake_timestamp = None

        # Drain the audio queue
        try:
            while not self._audio_q.empty():
                self._audio_q.get_nowait()
        except Exception:
            pass

        debug_log("audio buffers cleared", "voice")

    def _is_speech_frame(self, frame) -> bool:
        """Determine if audio frame contains speech."""
        if np is None:
            return True

        rms = float(np.sqrt(np.mean(np.square(frame))))

        if self._vad is None:
            return rms >= float(getattr(self.cfg, "voice_min_energy", 0.0045))

        # Use WebRTC VAD
        try:
            pcm16 = np.clip(frame.flatten() * 32768.0, -32768, 32767).astype(np.int16).tobytes()
            return bool(self._vad.is_speech(pcm16, getattr(self, "_stream_samplerate", self._samplerate)))
        except Exception:
            return False

    def _filter_noisy_segments(self, segments):
        """Filter out low-confidence Whisper segments."""
        min_confidence = getattr(self.cfg, "whisper_min_confidence", 0.3)
        marginal_threshold = min_confidence / 3  # Show user-visible log for marginal confidence
        # Threshold above which a segment is considered non-speech (hallucination during silence).
        # Checked independently of avg_logprob because Whisper can be confident about a
        # hallucinated phrase even when no real speech is present.
        no_speech_threshold = getattr(self.cfg, "whisper_no_speech_threshold", 0.5)
        filtered = []

        for seg in segments:
            # Hard filter: high no_speech_prob means no real speech regardless of logprob.
            if hasattr(seg, 'no_speech_prob') and is_whisper_hallucination(seg.no_speech_prob, no_speech_threshold):
                debug_log(
                    f"segment filtered (no_speech_prob={seg.no_speech_prob:.2f}): '{seg.text[:50]}'",
                    "voice",
                )
                continue

            confidence = None
            if hasattr(seg, 'avg_logprob'):
                confidence = min(1.0, max(0.0, (seg.avg_logprob + 1.0)))
            elif hasattr(seg, 'no_speech_prob'):
                confidence = 1.0 - seg.no_speech_prob

            if confidence is not None and confidence < min_confidence:
                if confidence >= marginal_threshold:
                    # Marginal confidence - show in log viewer (not debug)
                    print(f"🔇 Low confidence ({confidence:.2f}): \"{seg.text.strip()[:50]}...\"", flush=True)
                else:
                    # Very low confidence - debug only
                    debug_log(f"segment filtered (confidence={confidence:.2f}): '{seg.text}'", "voice")
                continue

            filtered.append(seg)

        return filtered

    def _is_repetitive_hallucination(self, text: str) -> bool:
        """
        Detect repetitive hallucinations that Whisper produces on quiet/ambiguous audio.

        Common patterns include repeated single words like "don't don't don't..."
        or repeated short phrases. Also detects character-level repetition patterns
        like "Jろ Jろ Jろ..." which may appear with or without spaces.

        Args:
            text: Transcribed text to check

        Returns:
            True if the text appears to be a hallucination
        """
        import re
        from collections import Counter

        if not text:
            return False

        text_stripped = text.strip()
        if len(text_stripped) < 6:
            return False

        # --- Character-level repetition detection ---
        # Remove all whitespace to detect patterns like "Jろ Jろ Jろ" or "JろJろJろ"
        text_no_space = re.sub(r'\s+', '', text_stripped.lower())

        # Look for repeating patterns of 1-5 characters appearing 3+ times consecutively
        # This catches "JろJろJろJろ" (pattern "Jろ" repeating)
        for pattern_len in range(1, 6):
            if len(text_no_space) < pattern_len * 3:
                continue

            # Check if text is mostly composed of a repeating pattern
            for start in range(pattern_len):
                pattern = text_no_space[start:start + pattern_len]
                if not pattern:
                    continue

                # Count how many times this pattern repeats consecutively from this start position
                remaining = text_no_space[start:]
                repeat_count = 0
                pos = 0
                while pos + pattern_len <= len(remaining) and remaining[pos:pos + pattern_len] == pattern:
                    repeat_count += 1
                    pos += pattern_len

                # If pattern repeats 4+ times and covers most of the string, it's a hallucination
                covered_chars = repeat_count * pattern_len
                coverage = covered_chars / len(text_no_space) if text_no_space else 0

                if repeat_count >= 4 and coverage >= 0.6:
                    debug_log(f"char-level repetition detected: pattern '{pattern}' repeats {repeat_count}x, coverage={coverage:.0%}", "voice")
                    return True

        # --- Word-level repetition detection (existing logic) ---
        words = text_stripped.lower().split()
        if len(words) < 4:
            return False

        # Strip punctuation from words for comparison (handles "word..." vs "word")
        clean_words = [re.sub(r'[^\w]', '', w) for w in words]
        clean_words = [w for w in clean_words if w]  # Remove empty strings

        if len(clean_words) < 4:
            return False

        word_counts = Counter(clean_words)
        most_common_word, most_common_count = word_counts.most_common(1)[0]

        # If a single word makes up more than 50% of all words and appears 4+ times
        if most_common_count >= 4 and most_common_count / len(clean_words) > 0.5:
            debug_log(f"repetitive hallucination detected: '{most_common_word}' repeated {most_common_count}x in '{text[:50]}...'", "voice")
            return True

        # Check for repeated consecutive sequences (e.g., "don don don" or "stop stop stop")
        # Look for any word repeated 3+ times consecutively
        consecutive_count = 1
        for i in range(1, len(clean_words)):
            if clean_words[i] == clean_words[i-1]:
                consecutive_count += 1
                if consecutive_count >= 3:
                    debug_log(f"consecutive repetition detected: '{clean_words[i]}' repeated {consecutive_count}+ times", "voice")
                    return True
            else:
                consecutive_count = 1

        return False

    def _check_query_timeout(self) -> None:
        """Check hot-window expiry when no audio is being processed."""
        # even when there's no audio being processed
        self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)

    def _on_audio(self, indata, frames, time_info, status):
        """Audio callback from sounddevice."""
        try:
            if not self._accepting_audio():
                return
            self._callback_count += 1
            chunk = (indata.copy() if hasattr(indata, "copy") else indata)
            try:
                self._audio_q.put_nowait(chunk)
            except Exception:
                pass
        except Exception:
            return

    def _accepting_audio(self) -> bool:
        """Whether captured audio may enter the pipeline right now."""
        return not (
            self._should_stop
            or self._dictation_active
            or (self.tts is not None and self.tts.is_speaking())
        )

    def feed_external_audio(self, pcm16: bytes) -> bool:
        """Accept 16-bit little-endian mono PCM captured outside this process.

        The control centre records the microphone in the browser and posts the
        frames here. They join the queue the local callback fills, so VAD,
        transcription and the reply path treat them like any other audio.

        Returns whether the frame entered the pipeline. A refusal is normal:
        frames arriving while Jarvis speaks are echo, and a full queue means
        the consumer is behind. Neither may block the socket thread.
        """
        if np is None or not self._accepting_audio():
            return False
        if not pcm16 or len(pcm16) % 2:
            return False
        try:
            frame = np.frombuffer(pcm16, dtype="<i2").astype(np.float32) / 32768.0
        except Exception:
            return False
        if frame.size == 0:
            return False
        try:
            self._audio_q.put_nowait(frame)
        except Exception:
            return False
        return True

    def _determine_whisper_backend(self) -> str:
        """Determine which Whisper backend to use based on config and availability."""
        backend_pref = getattr(self.cfg, "whisper_backend", "auto")

        if backend_pref == "mlx":
            if MLX_WHISPER_AVAILABLE:
                return "mlx"
            debug_log("MLX Whisper requested but not available, falling back to faster-whisper", "voice")
            return "faster-whisper"

        if backend_pref == "faster-whisper":
            return "faster-whisper"

        # Auto mode: prefer MLX on Apple Silicon
        if MLX_WHISPER_AVAILABLE and _is_apple_silicon():
            return "mlx"

        return "faster-whisper"

    def _apply_whisper_load_success(
        self, model_name: str, try_device: str, try_compute: str,
        device: str, compute: str, cpu_threads: int,
        context: str = "",
    ) -> str:
        """Record state and print diagnostics after a successful Whisper model load.

        Returns the resolved device string.
        """
        ct2_model = getattr(self.model, "model", None)
        resolved_device = str(getattr(ct2_model, "device", try_device)).lower()
        debug_log(
            f"faster-whisper initialised{context}: name={model_name}, "
            f"device={resolved_device}, compute={try_compute}, "
            f"cpu_threads={cpu_threads}",
            "voice",
        )
        self._whisper_device = resolved_device

        if try_device != device and device in ("auto", "cuda"):
            print("     ⚠️  CUDA not available, using CPU (this may be slower)", flush=True)
            print("     💡 Tip: Install NVIDIA CUDA toolkit for faster speech recognition", flush=True)
        if try_compute != compute:
            print(f"     ⚠️  Using '{try_compute}' compute type ('{compute}' not supported)", flush=True)
        if resolved_device == "cpu":
            print(f"     ⚡ CPU mode: using {cpu_threads} threads with optimised decoding", flush=True)

        suffix = f" ({context})" if context else ""
        print(f"     🎤 Whisper '{model_name}' loaded on {resolved_device}{suffix}", flush=True)
        return resolved_device

    def _start_llm_warmup(self) -> list[threading.Thread]:
        """Pre-load chat and intent judge models via the active backend.

        Warmup goes through ``warm_up_chat_model`` → ``LLMBackend.warm_up``,
        so it pages models into Ollama's resident memory on the Ollama path
        and sends a minimal inference to load the model on an OpenAI-
        compatible server. Starts up to two daemon threads concurrently so
        warmup overlaps with Whisper initialisation. When both models point
        at the same model, a single warmup covers both.

        Results land in ``self._llm_warmup_results`` keyed by role. The
        caller joins the returned threads with a shared deadline before
        announcing "Listening!" so the ready state actually means ready.
        """
        self._llm_warmup_results: dict[str, tuple[str, bool]] = {}

        if _is_low_power_mode_enabled(self.cfg):
            print("     🌱 Low power mode: LLM warmup skipped", flush=True)
            debug_log("low power mode enabled: skipping LLM warmup", "voice")
            return []

        chat_model = str(getattr(self.cfg, "llm_chat_model", "") or "").strip()
        # Cap warmup at 60s total: the join budget is hardcoded at 60s (see
        # warmup-join logic below), so a longer per-thread timeout would
        # leave daemon threads running after the deadline.
        chat_timeout = min(
            max(float(getattr(self.cfg, "llm_tools_timeout_sec", 8.0)), 60.0),
            60.0,
        )
        judge = self._intent_judge
        judge_model = judge.config.model if judge is not None else ""
        shared_judge = bool(chat_model) and judge_model == chat_model

        # Tool router — only warmed when the LLM selection strategy is active
        # AND it points at a model distinct from chat/judge. Routing runs on
        # the fast tier; resolving through the same tier helper the reply
        # engine uses keeps warmup targeting whatever the engine will actually
        # call. Skipping warmup for non-LLM strategies avoids loading a model
        # that won't be used this session.
        strategy = str(getattr(self.cfg, "tool_selection_strategy", "") or "").lower()
        from ..llm import resolve_model, Tier
        router_model_effective = resolve_model(self.cfg, Tier.FAST)
        router_model = router_model_effective if strategy == "llm" else ""
        shared_router = bool(router_model) and router_model in {chat_model, judge_model}

        embed_model = str(getattr(self.cfg, "embedding_model", "") or "").strip()
        shared_embed = bool(embed_model) and embed_model in {
            m for m in (chat_model, judge_model, router_model) if m
        }

        threads: list[threading.Thread] = []

        if chat_model:
            def _warm_chat() -> None:
                ok = warm_up_chat_model(self.cfg, chat_model, timeout=chat_timeout)
                if ok:
                    prefix_ok = warm_up_reply_prefix(
                        self.cfg,
                        chat_model,
                        timeout_sec=chat_timeout,
                    )
                    if not prefix_ok:
                        debug_log(
                            "chat model resident but reply prefix prefill failed",
                            "voice",
                        )
                self._llm_warmup_results["chat"] = (chat_model, ok)
                # When chat and judge share a model, one warmup covers both.
                if shared_judge:
                    self._llm_warmup_results["judge"] = (chat_model, ok)
                # Router reusing chat_model is already covered.
                if router_model and router_model == chat_model:
                    self._llm_warmup_results["router"] = (chat_model, ok)
                # When the embed model matches chat, the chat warmup already
                # loaded the model into memory; no separate embed thread runs.
                if shared_embed and embed_model == chat_model:
                    self._llm_warmup_results["embed"] = (chat_model, ok)

            threads.append(threading.Thread(target=_warm_chat, daemon=True, name="warmup-chat"))

        if judge is not None and not shared_judge:
            def _warm_judge() -> None:
                ok = judge.warm_up()
                self._llm_warmup_results["judge"] = (judge_model, ok)
                if router_model and router_model == judge_model:
                    self._llm_warmup_results["router"] = (judge_model, ok)
                if shared_embed and embed_model == judge_model:
                    self._llm_warmup_results["embed"] = (judge_model, ok)

            threads.append(threading.Thread(target=_warm_judge, daemon=True, name="warmup-judge"))

        if router_model and not shared_router:
            def _warm_router() -> None:
                ok = warm_up_chat_model(self.cfg, router_model, timeout=chat_timeout)
                self._llm_warmup_results["router"] = (router_model, ok)
                if shared_embed and embed_model == router_model:
                    self._llm_warmup_results["embed"] = (router_model, ok)

            threads.append(threading.Thread(target=_warm_router, daemon=True, name="warmup-router"))

        if embed_model and not shared_embed:
            def _warm_embed() -> None:
                try:
                    backend = get_embedding_backend(self.cfg)
                    # Embedding-only models are not served on the chat endpoint.
                    # Warming the static tool catalogue both loads the model and
                    # removes every description embedding from the first reply.
                    embed_timeout = min(chat_timeout, 15.0)
                    from ..tools.registry import BUILTIN_TOOLS, get_cached_mcp_tools
                    from ..tools.selection import warm_tool_embedding_cache
                    mcp_tools = get_cached_mcp_tools()
                    expected = (
                        len([name for name in BUILTIN_TOOLS if name != "stop"])
                        + len(mcp_tools)
                    )
                    warmed = warm_tool_embedding_cache(
                        BUILTIN_TOOLS,
                        mcp_tools,
                        backend,
                        embed_model,
                        timeout_sec=embed_timeout,
                    )
                    ok = warmed == expected
                    debug_log(
                        f"tool embedding cache warmed: {warmed}/{expected}",
                        "planning",
                    )
                except Exception as exc:
                    debug_log(f"embed warmup failed: {exc}", "voice")
                    ok = False
                self._llm_warmup_results["embed"] = (embed_model, ok)

            threads.append(threading.Thread(target=_warm_embed, daemon=True, name="warmup-embed"))

        for t in threads:
            t.start()

        debug_log(
            f"LLM warmup started (chat={chat_model or 'n/a'}, "
            f"judge={judge_model or 'n/a'}, router={router_model or 'n/a'}, "
            f"embed={embed_model or 'n/a'}, "
            f"shared_judge={shared_judge}, shared_router={shared_router})",
            "voice",
        )
        return threads

    def _weather_example(self, wake_title: str) -> str:
        """Return the weather query example for the startup banner.

        Shows the plain form when a location source is configured, or the
        [your city] placeholder form so the user knows to supply a city.
        """
        location_enabled = getattr(self.cfg, "location_enabled", True)
        location_auto_detect = getattr(self.cfg, "location_auto_detect", True)
        location_ip_address = getattr(self.cfg, "location_ip_address", None)
        location_known = (
            location_enabled
            and (location_auto_detect or bool(location_ip_address))
            and is_location_available()
        )
        if location_known:
            return f"\"How's the weather, {wake_title}?\""
        return f"\"How's the weather in [your city], {wake_title}?\""

    def _open_local_stream(self):
        """Open the local microphone, or return None when it cannot be had.

        A device that is missing, blocked or busy is not fatal: the loop
        still serves audio posted by the control centre. The reasons are
        printed either way, because a silent downgrade to browser-only
        capture would look like a broken microphone.
        """
        # Audio device setup
        stream_kwargs = {}
        device_env = (self.cfg.voice_device or '').strip().lower()

        if self.cfg.voice_debug:
            debug_log("available input devices:", "voice")
            try:
                for idx, dev in enumerate(sd.query_devices()):
                    try:
                        max_in = int(dev.get("max_input_channels", 0))
                    except Exception:
                        max_in = 0
                    if max_in > 0:
                        name = dev.get("name")
                        rate = dev.get("default_samplerate")
                        debug_log(f"  [{idx}] {name} (channels={max_in}, default_sr={rate})", "voice")
            except Exception:
                pass

        # Configure audio device
        if device_env and device_env not in ("default", "system"):
            try:
                device_index = int(self.cfg.voice_device)
            except ValueError:
                device_index = None
                try:
                    for idx, dev in enumerate(sd.query_devices()):
                        if isinstance(dev.get("name"), str) and (self.cfg.voice_device or '').lower() in dev.get("name").lower():
                            device_index = idx
                            break
                except Exception:
                    device_index = None
            if device_index is not None:
                stream_kwargs["device"] = device_index

        # Log which device will be used
        try:
            if "device" in stream_kwargs:
                dev = sd.query_devices(stream_kwargs["device"])
                device_name = dev.get('name', 'Unknown')
                debug_log(f"using input device: {device_name} (index {stream_kwargs['device']})", "voice")
                print(f"  🎤 Using audio device: {device_name}", flush=True)
            else:
                debug_log("using system default input device", "voice")
                try:
                    default_dev = sd.query_devices(sd.default.device[0])
                    print(f"  🎤 Using default device: {default_dev.get('name', 'Unknown')}", flush=True)
                except Exception:
                    print("  🎤 Using system default input device", flush=True)
        except Exception:
            pass

        # Open audio stream — try configured rate first, fall back to device
        # native rate when the hardware rejects 16 kHz (common on Linux ALSA).
        self._stream_samplerate = self._samplerate
        open_error = None
        try:
            with portaudio_lock:
                stream = sd.InputStream(
                    samplerate=self._samplerate,
                    channels=1,
                    dtype="float32",
                    blocksize=self._frame_samples,
                    callback=self._on_audio,
                    **stream_kwargs,
                )
        except Exception as e:
            error_msg = str(e).lower()
            is_rate_error = "sample rate" in error_msg or "9987" in error_msg
            if is_rate_error:
                debug_log(f"device rejected {self._samplerate} Hz, querying native rate", "voice")
                try:
                    if "device" in stream_kwargs:
                        dev_info = sd.query_devices(stream_kwargs["device"])
                    else:
                        dev_info = sd.query_devices(kind="input")
                    native_rate = int(dev_info.get("default_samplerate", self._samplerate))
                    if native_rate != self._samplerate:
                        self._stream_samplerate = native_rate
                        native_frame_samples = max(1, int(native_rate * 30 / 1000))
                        print(f"  ⚠️  Device doesn't support {self._samplerate} Hz — using {native_rate} Hz with resampling", flush=True)
                        debug_log(f"retrying stream at native {native_rate} Hz", "voice")
                        with portaudio_lock:
                            stream = sd.InputStream(
                                samplerate=native_rate,
                                channels=1,
                                dtype="float32",
                                blocksize=native_frame_samples,
                                callback=self._on_audio,
                                **stream_kwargs,
                            )
                    else:
                        open_error = e
                except Exception:
                    open_error = e
            else:
                open_error = e

        if open_error is not None:
            error_msg = str(open_error).lower()
            debug_log(f"failed to open input stream: {open_error}", "voice")

            # Provide helpful error messages for common issues
            if "access" in error_msg or "permission" in error_msg:
                print(f"  ❌ Microphone access denied. Please check: {_get_mic_permission_hint()}", flush=True)
            elif "device" in error_msg and ("use" in error_msg or "busy" in error_msg):
                print("  ❌ Microphone is being used by another application", flush=True)
            elif "device" in error_msg:
                print(f"  ❌ Failed to open microphone: {open_error}", flush=True)
                print("     Try selecting a different audio device in settings", flush=True)
            else:
                print(f"  ❌ Failed to start audio recording: {open_error}", flush=True)
            return None
        return stream

    def run(self) -> None:
        """Main voice listening loop."""
        # Publish the conversation switch so interfaces outside the voice
        # loop can turn it on without reaching into the listener.
        register_conversation_controller(self.state_manager)
        # Publish the audio ingress so the control centre can post the
        # browser's microphone into this loop.
        register_audio_sink(self)

        # Capturing the local microphone is optional. Without it this loop
        # still transcribes and answers audio posted by the control centre,
        # so a missing or busy device downgrades the listener rather than
        # ending it. Everything below that touches `sd` is gated on this.
        self._local_capture = sd is not None
        if not self._local_capture:
            debug_log("sounddevice not available", "voice")
            print("  🔇 No local audio device - listening only to the control centre", flush=True)

        # Verify PortAudio is working by querying devices (catches Windows DLL issues)
        if self._local_capture:
            try:
                devices = sd.query_devices()
                input_devices = [d for d in devices if d.get('max_input_channels', 0) > 0]
                debug_log(f"PortAudio initialised: {len(input_devices)} input device(s) found", "voice")
                if not input_devices:
                    print("  🔇 No microphone found - listening only to the control centre", flush=True)
                    self._local_capture = False
            except Exception as e:
                debug_log(f"PortAudio device query failed: {e}", "voice")
                print(f"  ⚠️  Audio system error: {e}", flush=True)
                print("     PortAudio may not be properly installed", flush=True)
                if sys.platform == 'linux':
                    print("     On Linux, ensure PortAudio is installed: sudo apt install libportaudio2", flush=True)
                self._local_capture = False

        # Windows 11: Test microphone permission by attempting a brief recording
        # This catches privacy settings that silently block audio access.
        # A 5-second timeout prevents indefinite hangs when Windows blocks
        # the audio device at the system level without raising an error.
        # Uses InputStream (not sd.rec) so the stream can be explicitly closed
        # on timeout, avoiding resource leaks that could block later audio init.
        if sys.platform == 'win32' and self._local_capture:
            try:
                print("  🔐 Checking microphone permission...", flush=True)
                mic_ok = threading.Event()
                mic_error: list = [None]

                def _mic_check():
                    # Deliberately NOT under portaudio_lock: this probe's
                    # open/start can hang indefinitely when Windows blocks
                    # mic access at the system level (that is what the 5s
                    # timeout below is for), and hanging while holding the
                    # process-wide lock would freeze every other audio user
                    # (listener, dictation, TTS). The probe runs once at
                    # startup before the listener's main stream opens, so
                    # the residual open/open race is minimal; the quick
                    # stop/close after a successful start stays guarded.
                    stream = None
                    try:
                        stream = sd.InputStream(
                            samplerate=self._samplerate, channels=1,
                            dtype="float32", blocksize=int(self._samplerate * 0.1),
                        )
                        stream.start()
                        time.sleep(0.15)
                        with portaudio_lock:
                            stream.stop()
                            stream.close()
                        stream = None
                        mic_ok.set()
                    except Exception as exc:
                        mic_error[0] = exc
                        if stream is not None:
                            try:
                                with portaudio_lock:
                                    stream.close()
                            except Exception:
                                pass

                check_thread = threading.Thread(target=_mic_check, daemon=True)
                check_thread.start()
                check_thread.join(timeout=5.0)

                if check_thread.is_alive():
                    # Do NOT abort/close the stream from this thread: the
                    # check thread may still be blocked inside start()/stop()
                    # on it, and closing a stream under another thread's feet
                    # is a native use-after-free that aborts the whole app on
                    # Windows (#401). Abandon it — the daemon check thread
                    # will finish the stop/close itself if it ever unblocks.
                    debug_log("microphone permission check timed out after 5s", "voice")
                    print("  ⚠️  Microphone permission check timed out", flush=True)
                    print("     This may indicate Windows is blocking microphone access.", flush=True)
                    print("     Continuing anyway — voice input may not work.", flush=True)
                elif mic_error[0] is not None:
                    e = mic_error[0]
                    error_str = str(e).lower()
                    print(f"  ❌ Microphone permission check failed: {e}", flush=True)
                    if "unapproved" in error_str or "denied" in error_str or "access" in error_str or "-9999" in str(e):
                        print("", flush=True)
                        print("  ┌─────────────────────────────────────────────────────────┐", flush=True)
                        print("  │  🔒 MICROPHONE ACCESS BLOCKED BY WINDOWS               │", flush=True)
                        print("  │                                                         │", flush=True)
                        print("  │  To fix this:                                          │", flush=True)
                        print("  │  1. Open Windows Settings                              │", flush=True)
                        print("  │  2. Go to Privacy & security → Microphone              │", flush=True)
                        print("  │  3. Turn ON 'Microphone access'                        │", flush=True)
                        print("  │  4. Turn ON 'Let apps access your microphone'          │", flush=True)
                        print("  │  5. Turn ON 'Let desktop apps access your microphone'  │", flush=True)
                        print("  │                                                         │", flush=True)
                        print("  │  Then restart Jarvis.                                  │", flush=True)
                        print("  └─────────────────────────────────────────────────────────┘", flush=True)
                        print("", flush=True)
                    return
                elif mic_ok.is_set():
                    print("  ✅ Microphone permission OK", flush=True)
                else:
                    print("  ⚠️  Microphone returned empty audio", flush=True)
            except Exception as e:
                debug_log(f"microphone permission check error: {e}", "voice")
                print(f"  ⚠️  Microphone check error: {e}", flush=True)

        # Kick off LLM warmups in parallel with Whisper load so the first
        # user engagement doesn't pay cold-load cost on either model. All
        # warmup output (Whisper + LLMs) is indented under this header to
        # visually group the phase.
        print("  🔥 Warming up models...", flush=True)
        self._llm_warmup_started_at = time.time()
        self._llm_warmup_threads = self._start_llm_warmup()

        # Determine and initialise Whisper backend
        self._whisper_backend = self._determine_whisper_backend()
        model_name = getattr(self.cfg, "whisper_model", "small")

        # Validate large-v3-turbo support for faster-whisper backend
        if model_name == "large-v3-turbo" and self._whisper_backend != "mlx":
            if not _is_faster_whisper_turbo_supported():
                debug_log(
                    "faster-whisper does not support large-v3-turbo, "
                    "falling back to large-v3", "voice",
                )
                print(
                    "  ⚠️  large-v3-turbo is not supported by the installed Whisper engine, "
                    "using large-v3 instead", flush=True,
                )
                model_name = "large-v3"

        if self._whisper_backend == "mlx":
            if not MLX_WHISPER_AVAILABLE:
                debug_log("MLX Whisper not available", "voice")
                print("  ❌ MLX Whisper not available. Install with: pip install mlx-whisper", flush=True)
                return

            self._mlx_model_repo = _get_mlx_model_repo(model_name)
            print(f"     🎤 Loading MLX Whisper '{model_name}' (Apple Silicon GPU)...", flush=True)

            max_retries = 4
            for attempt in range(max_retries + 1):
                try:
                    # Pre-load the model by doing a warmup transcription.
                    # Use low-amplitude noise (not silence) so the decoder actually runs —
                    # silent audio trips the no-speech short-circuit and leaves the decode
                    # path cold, so the first real utterance still pays the full cost.
                    if np is not None:
                        rng = np.random.default_rng(0)
                        warmup_audio = rng.standard_normal(self._samplerate).astype(np.float32) * 0.01
                        _ = mlx_whisper.transcribe(
                            warmup_audio,
                            path_or_hf_repo=self._mlx_model_repo,
                            language=resolve_transcription_language(self.cfg),
                        )
                        debug_log(f"MLX Whisper model pre-loaded: repo={self._mlx_model_repo}", "voice")

                    print(f"     🎤 MLX Whisper '{model_name}' ready (Apple Silicon GPU)", flush=True)
                    break
                except Exception as e:
                    error_str = str(e).lower()
                    is_rate_limited = (
                        any(x in error_str for x in ["429", "too many requests", "rate limit"])
                        or getattr(getattr(e, "response", None), "status_code", None) == 429
                    )
                    if is_rate_limited and attempt < max_retries:
                        wait = 2 ** (attempt + 1)
                        debug_log(f"rate limited loading MLX Whisper (attempt {attempt + 1}): {e}", "voice")
                        print(f"  ⏳ Rate limited by HuggingFace, retrying in {wait}s ({attempt + 1}/{max_retries})...", flush=True)
                        time.sleep(wait)
                        continue
                    debug_log(f"failed to initialise MLX Whisper: {e}", "voice")
                    print(f"  ❌ Failed to initialise MLX Whisper: {e}", flush=True)
                    if is_rate_limited:
                        print("  💡 HuggingFace is rate limiting downloads. Please wait a few minutes and restart.", flush=True)
                    return
        else:
            # faster-whisper backend
            if not FASTER_WHISPER_AVAILABLE:
                debug_log("faster-whisper not available", "voice")
                print("  ❌ faster-whisper not available. Install with: pip install faster-whisper", flush=True)
                return

            device = getattr(self.cfg, "whisper_device", "auto")
            compute = getattr(self.cfg, "whisper_compute_type", "int8")

            # On Windows, probe for CUDA runtime libraries before trying to
            # use them. faster-whisper/CTranslate2 lazily loads cuBLAS and
            # cuDNN during transcription, so without this check a model
            # that loaded fine on cuda will crash on the first audio chunk.
            resolved_device, missing_libs = _probe_windows_cuda_libraries(device)
            if missing_libs:
                _print_cuda_unavailable_hint(missing_libs)
            device = resolved_device

            # Build list of (device, compute_type) combinations to try
            # This handles both compute type fallbacks and CUDA -> CPU fallbacks
            configs_to_try = []

            # Start with preferred config
            compute_types = [compute]
            if compute == "int8":
                compute_types.extend(["float16", "float32"])
            elif compute == "float16":
                compute_types.append("float32")

            # Add preferred device with all compute types
            for ct in compute_types:
                configs_to_try.append((device, ct))

            # If device is "auto" or "cuda", add CPU fallback configs
            # This handles Windows without CUDA libraries
            if device in ("auto", "cuda"):
                for ct in compute_types:
                    configs_to_try.append(("cpu", ct))

            last_error = None
            used_device = device
            used_compute = compute
            for try_device, try_compute in configs_to_try:
                try:
                    cpu_threads = (os.cpu_count() or 4) if try_device in ("cpu", "auto") else 0
                    print(f"     🎤 Loading Whisper '{model_name}' (device={try_device}, compute={try_compute})...", flush=True)
                    self.model = WhisperModel(
                        model_name, device=try_device, compute_type=try_compute,
                        cpu_threads=cpu_threads,
                    )
                    self._apply_whisper_load_success(
                        model_name, try_device, try_compute,
                        device, compute, cpu_threads,
                    )
                    used_device = try_device
                    used_compute = try_compute
                    last_error = None
                    break
                except Exception as e:
                    last_error = e
                    error_str = str(e).lower()

                    # Check if this is a CUDA/GPU-related error that we should fall back from
                    is_cuda_error = any(x in error_str for x in [
                        "cuda", "cublas", "cudnn", "gpu", "nvidia",
                        ".dll is not found", "library", "ctypes"
                    ])
                    is_compute_error = any(x in error_str for x in [
                        "compute type", "int8", "float16"
                    ])

                    if is_cuda_error or is_compute_error:
                        debug_log(f"config ({try_device}, {try_compute}) failed, trying fallback: {e}", "voice")
                        continue

                    # Check for corrupted model cache (e.g. interrupted download)
                    is_corrupted_cache = "unable to open file" in error_str

                    if is_corrupted_cache:
                        debug_log(f"detected corrupted Whisper model cache: {e}", "voice")
                        print("  ⚠️  Whisper model cache appears corrupted, attempting recovery...", flush=True)

                        cache_cleared = _clear_corrupted_whisper_cache(str(e))
                        if cache_cleared:
                            try:
                                print(f"     🎤 Re-downloading Whisper '{model_name}'...", flush=True)
                                self.model = WhisperModel(
                                    model_name, device=try_device, compute_type=try_compute,
                                    cpu_threads=cpu_threads,
                                )
                                self._apply_whisper_load_success(
                                    model_name, try_device, try_compute,
                                    device, compute, cpu_threads,
                                    context="recovered",
                                )
                                used_device = try_device
                                used_compute = try_compute
                                last_error = None
                                break
                            except Exception as retry_e:
                                debug_log(f"retry after cache clear also failed: {retry_e}", "voice")
                                print(f"  ❌ Failed to load Whisper model after cache recovery: {retry_e}", flush=True)
                                debug_log("trying next device/compute fallback config", "voice")
                                continue
                        else:
                            debug_log("could not clear corrupted cache automatically", "voice")
                            print(f"  ❌ Failed to load Whisper model: {e}", flush=True)
                            print("  💡 Try manually deleting the Whisper model cache directory and restarting", flush=True)
                            continue
                    # Check for rate limiting (HTTP 429) — check string and response status code
                    # (HfHubHTTPError may carry the status on .response without "429" in str(e))
                    is_rate_limited = (
                        any(x in error_str for x in ["429", "too many requests", "rate limit"])
                        or getattr(getattr(e, "response", None), "status_code", None) == 429
                    )

                    if is_rate_limited:
                        _max_retries = 4
                        _backoff = 2
                        debug_log(f"rate limited loading Whisper model: {e}", "voice")
                        retry_succeeded = False
                        for retry_num in range(1, _max_retries + 1):
                            wait = _backoff ** retry_num
                            print(f"  ⏳ Rate limited by HuggingFace, retrying in {wait}s ({retry_num}/{_max_retries})...", flush=True)
                            time.sleep(wait)
                            try:
                                self.model = WhisperModel(
                                    model_name, device=try_device, compute_type=try_compute,
                                    cpu_threads=cpu_threads,
                                )
                                self._apply_whisper_load_success(
                                    model_name, try_device, try_compute,
                                    device, compute, cpu_threads,
                                    context="rate-limit retry",
                                )
                                used_device = try_device
                                used_compute = try_compute
                                last_error = None
                                retry_succeeded = True
                                break
                            except Exception as retry_e:
                                debug_log(f"rate-limit retry {retry_num} failed: {retry_e}", "voice")
                                last_error = retry_e
                        if retry_succeeded:
                            break
                        debug_log(f"gave up after {_max_retries} rate-limit retries", "voice")
                        print(f"  ❌ Failed to load Whisper model after {_max_retries} retries: {last_error}", flush=True)
                        print("  💡 HuggingFace is rate limiting downloads. Please wait a few minutes and restart.", flush=True)
                        return
                    else:
                        # For other errors (model not found, etc.), don't try fallbacks
                        debug_log(f"failed to initialise faster-whisper: {e}", "voice")
                        print(f"  ❌ Failed to load Whisper model: {e}", flush=True)
                        return

            if last_error is not None:
                debug_log(f"failed to initialise faster-whisper with any config: {last_error}", "voice")
                print(f"  ❌ Failed to load Whisper model: {last_error}", flush=True)
                return

            # Warm up faster-whisper so the first real utterance doesn't pay
            # the cold-decode cost. Use low-amplitude noise rather than pure
            # silence — silence trips faster-whisper's no-speech short-circuit
            # and the decoder never actually runs. Mirror the real transcribe
            # parameters, the configured language included, so beam search and
            # the timestamp path are exercised here rather than on the user's
            # first word. The VAD filter stays off regardless of the setting:
            # it would discard the warmup noise and leave the decoder cold,
            # which is the one thing this call exists to prevent.
            if np is not None and self.model is not None:
                try:
                    cpu_mode = self._whisper_device == "cpu"
                    language = resolve_transcription_language(self.cfg)
                    rng = np.random.default_rng(0)
                    warmup_audio = rng.standard_normal(self._samplerate).astype(np.float32) * 0.01
                    try:
                        segments_iter, _ = self.model.transcribe(
                            warmup_audio,
                            language=language,
                            vad_filter=False,
                            condition_on_previous_text=not cpu_mode,
                            without_timestamps=cpu_mode,
                        )
                    except TypeError:
                        segments_iter, _ = self.model.transcribe(warmup_audio, language=language)
                    for _ in segments_iter:
                        pass
                    debug_log("faster-whisper warmup transcription complete", "voice")
                except Exception as e:
                    debug_log(f"faster-whisper warmup failed: {e}", "voice")

        # Wait for LLM warmups before announcing "Listening!" so the first
        # engagement is responsive. A single 60s budget is shared across
        # all warmup threads so a slow/down Ollama can't block us from
        # listening — we'll just pay the cold-load cost on demand.
        warmup_threads = getattr(self, "_llm_warmup_threads", [])
        if warmup_threads:
            budget = 60.0
            deadline = getattr(self, "_llm_warmup_started_at", time.time()) + budget
            for t in warmup_threads:
                remaining = max(0.0, deadline - time.time())
                t.join(timeout=remaining)

            still_warming = any(t.is_alive() for t in warmup_threads)
            results = getattr(self, "_llm_warmup_results", {})

            # Trailing space after ⚠️ intentional: the warning glyph renders
            # narrower than 🧠/💬, so the pad keeps columns aligned.
            def _print_status(role_key: str, label: str, ok_icon: str) -> None:
                entry = results.get(role_key)
                if entry is None:
                    return
                name, ok = entry
                icon = ok_icon if ok else "⚠️ "
                status = "ready" if ok else "warmup failed — will load on first use"
                print(f"     {icon} {label} '{name}' {status}", flush=True)

            _print_status("chat", "Chat model", "💬")
            _print_status("judge", "Intent judge", "🧠")
            _print_status("router", "Tool router", "🔧")
            _print_status("embed", "Embed model", "📐")

            if still_warming:
                debug_log("LLM warmup still running after 60s — continuing without", "voice")
                print("     ⏳ Some models still warming — continuing anyway", flush=True)

        # Audio parameters
        frame_ms = int(getattr(self.cfg, "vad_frame_ms", 20))
        self._frame_samples = max(1, int(self._samplerate * frame_ms / 1000))
        pre_roll_ms = int(getattr(self.cfg, "vad_pre_roll_ms", 240))
        endpoint_silence_ms = int(getattr(self.cfg, "endpoint_silence_ms", 800))
        max_utt_ms = int(getattr(self.cfg, "max_utterance_ms", 12000))

        pre_roll_max_frames = max(1, int(pre_roll_ms / frame_ms))
        endpoint_silence_frames = max(1, int(endpoint_silence_ms / frame_ms))
        normal_max_utt_frames = max(1, int(max_utt_ms / frame_ms))

        debug_log(f"audio params: sample_rate={self._samplerate}, frame_ms={frame_ms}, frame_samples={self._frame_samples}", "voice")
        debug_log(f"VAD: enabled={bool(self._vad is not None)}, aggressiveness={getattr(self.cfg, 'vad_aggressiveness', 2)}", "voice")

        # Audio device setup. Browser-only capture leaves this None.
        self._stream_samplerate = self._samplerate
        stream = self._open_local_stream() if self._local_capture else None
        if stream is None:
            self._local_capture = False

        # Main audio processing loop
        with _serialised_stream(stream):
            # Verify stream is actually recording (helps catch permission issues)
            if stream is not None and not stream.active:
                try:
                    with portaudio_lock:
                        stream.start()
                except Exception as e:
                    error_msg = str(e).lower()
                    debug_log(f"failed to start audio stream: {e}", "voice")
                    if "access" in error_msg or "permission" in error_msg:
                        print(f"  ❌ Microphone access denied. Please check: {_get_mic_permission_hint()}", flush=True)
                    else:
                        print(f"  ❌ Failed to start recording: {e}", flush=True)
                    return

            # Show ready message only after stream is confirmed active
            wake_word = getattr(self.cfg, "wake_word", "jarvis").lower()
            wake_title = wake_word.title()
            print(f"\n{'─' * 50}\n🎙️  Listening! Try:", flush=True)
            print(f"      {self._weather_example(wake_title)}", flush=True)
            print(f"      \"I just ate a Big Mac, {wake_title}.\"", flush=True)
            print(f"      \"What are you thinking, {wake_title}?\"", flush=True)
            print(f"      \"What do you know about me, {wake_title}?\"", flush=True)

            # Small-model disclaimer: SMALL models can't infer your intent
            # from vague prompts, but they can still execute complex flows
            # if you spell out the steps. Assume the model is dumb and lay
            # things out for it. Classification lives in model_variants so
            # it stays in sync when supported models change.
            from ..reply.prompts.model_variants import detect_model_size, ModelSize
            chat_model_name = str(getattr(self.cfg, "llm_chat_model", "") or "").strip()
            if chat_model_name and detect_model_size(chat_model_name) == ModelSize.SMALL:
                print(
                    f"  ⚠️  Small model in use ({chat_model_name}). Assume it can't infer — spell out the steps for anything more involved:",
                    flush=True,
                )
                print(
                    f"      \"Tell me tomorrow's weather, then find local events for tomorrow, then recommend ones that suit the weather, {wake_title}.\"",
                    flush=True,
                )

            # Chrome MCP tip: the chrome MCP exposes a `navigate` tool that
            # takes a URL. Vague phrasing like "Open YouTube" forces the model
            # to guess a URL; "Navigate to youtube.com" maps directly to the
            # tool's argument and is more reliable on small models.
            try:
                from ..tools.registry import get_cached_mcp_tools
                mcp_tool_names = list(get_cached_mcp_tools().keys())
                has_chrome_mcp = any("chrome" in name.lower() for name in mcp_tool_names)
            except Exception:
                has_chrome_mcp = False
            if has_chrome_mcp:
                print(
                    f"  🌐 Chrome MCP detected. Name the destination URL so the browser tool can act directly:",
                    flush=True,
                )
                print(
                    f"      \"Navigate to youtube.com, {wake_title}.\"",
                    flush=True,
                )

            # Set face state to IDLE (awake and ready, waiting for wake word)
            try:
                from desktop_app.face_widget import get_jarvis_state, JarvisState
                state_manager = get_jarvis_state()
                state_manager.set_state(JarvisState.IDLE)
            except Exception:
                pass

            # Track start time for audio health monitoring
            _audio_start_time = time.time()
            _audio_health_logged = False

            while not self._should_stop:
                # One-time audio health check after 5 seconds
                if self._local_capture and not _audio_health_logged and time.time() - _audio_start_time > 5:
                    _audio_health_logged = True
                    if self._callback_count == 0:
                        print("  ⚠️  No audio received after 5 seconds!", flush=True)
                        print(f"     Check: {_get_mic_permission_hint()}", flush=True)
                        print("     Also check that your microphone is not muted", flush=True)

                try:
                    item = self._audio_q.get(timeout=0.2)
                except queue.Empty:
                    # Critical: Check timeouts even when no audio is being received
                    # This ensures hot window expiry fires reliably
                    self._check_query_timeout()
                    continue

                if item is None:
                    # Reset marker
                    self.is_speech_active = False
                    self._silence_frames = 0
                    self._utterance_frames = []
                    self._pre_roll.clear()
                    continue

                if np is None:
                    continue

                # Process audio buffer
                buf = item
                try:
                    mono = buf.reshape(-1, buf.shape[-1])[:, 0] if buf.ndim > 1 else buf.flatten()
                except Exception:
                    mono = buf.flatten()

                # Process frames
                offset = 0
                total = mono.shape[0]
                frame_timestamp = time.time()  # Timestamp for this batch of frames

                while offset + self._frame_samples <= total:
                    frame = mono[offset: offset + self._frame_samples]
                    offset += self._frame_samples

                    # VAD decision
                    is_voice = self._is_speech_frame(frame)

                    if not self.is_speech_active:
                        if is_voice:
                            self.is_speech_active = True
                            set_phase_if(Phase.IDLE, Phase.CAPTURING)

                            # Backdate start time by pre-roll duration — the
                            # actual speech onset was before VAD triggered.
                            pre_roll_sec = len(self._pre_roll) * frame_ms / 1000.0
                            utterance_start_time = time.time() - pre_roll_sec

                            # Track utterance timing for echo detection
                            self.echo_detector.track_utterance_timing(utterance_start_time, 0.0)

                            # Seed with pre-roll
                            if self._pre_roll:
                                self._utterance_frames.extend(list(self._pre_roll))
                            self._utterance_frames.append(frame.copy())
                            self._silence_frames = 0
                        else:
                            # Maintain pre-roll buffer
                            self._pre_roll.append(frame.copy())
                            while len(self._pre_roll) > pre_roll_max_frames:
                                try:
                                    self._pre_roll.popleft()
                                except Exception:
                                    break
                    else:
                        if is_voice:
                            self._utterance_frames.append(frame.copy())
                            self._silence_frames = 0
                        else:
                            self._silence_frames += 1
                            if (
                                self._silence_frames >= endpoint_silence_frames
                                or len(self._utterance_frames) >= normal_max_utt_frames
                            ):
                                self._finalize_utterance()
                                self._pre_roll.clear()

                    # Check for query timeouts
                    self._check_query_timeout()

                # Handle remaining audio
                if offset < total:
                    tail = mono[offset:]
                    if tail.size > 0:
                        self._pre_roll.append(tail.copy())
                        while len(self._pre_roll) > pre_roll_max_frames:
                            try:
                                self._pre_roll.popleft()
                            except Exception:
                                break

    def _finalize_utterance(self) -> None:
        """Process completed utterance through speech recognition.

        Wraps the work so that an utterance which never became a reply, and
        the phase it was measured in, are both cleared on the way out.
        """
        try:
            self._transcribe_and_route()
        finally:
            get_recorder().abandon()
            set_phase_if(Phase.CAPTURING, Phase.IDLE)
            set_phase_if(Phase.TRANSCRIBING, Phase.IDLE)

    def _transcribe_and_route(self) -> None:
        """Transcribe the captured utterance and decide what to do with it."""
        if np is None or not self._utterance_frames:
            self.is_speech_active = False
            self._silence_frames = 0
            self._utterance_frames = []
            return

        # Track when utterance ends - but don't overwrite global timing yet
        utterance_end_time = time.time()
        # The turn's clock starts the moment speaking stopped: that is when
        # the wait the user feels begins.
        trace = get_recorder().begin(source="voice", started_at=utterance_end_time)
        utterance_start_time = self.echo_detector._utterance_start_time

        if self.cfg.voice_debug:
            utterance_duration = utterance_end_time - utterance_start_time if utterance_start_time > 0 else 0
            start_time_str = datetime.fromtimestamp(utterance_start_time).strftime('%H:%M:%S.%f')[:-3] if utterance_start_time > 0 else "N/A"
            end_time_str = datetime.fromtimestamp(utterance_end_time).strftime('%H:%M:%S.%f')[:-3]
            debug_log(f"utterance captured: duration={utterance_duration:.2f}s (started: {start_time_str}, ended: {end_time_str})", "voice")

        # Transcribe full audio - the intent judge will extract the relevant query
        try:
            audio = np.concatenate(self._utterance_frames, axis=0).flatten()
        except Exception:
            audio = None

        # Calculate energy before clearing frames for transcript processing
        utterance_energy = self._calculate_audio_energy(self._utterance_frames[-10:] if self._utterance_frames else [])

        # Reset state before processing
        self.is_speech_active = False
        self._silence_frames = 0
        self._utterance_frames = []

        if audio is None or audio.size == 0:
            return

        # Resample to Whisper's expected rate if the stream ran at a different rate
        stream_rate = getattr(self, "_stream_samplerate", self._samplerate)
        if stream_rate != self._samplerate:
            audio = _resample(audio, stream_rate, self._samplerate)

        # Filter short audio
        audio_duration = len(audio) / self._samplerate
        min_duration = getattr(self.cfg, "whisper_min_audio_duration", 0.3)
        if audio_duration < min_duration:
            debug_log(f"audio too short ({audio_duration:.2f}s < {min_duration}s), ignoring", "voice")
            get_runtime_state().count_discard("too_short")
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        # Speech recognition with appropriate backend
        set_phase(Phase.TRANSCRIBING)
        discard_reason = "no_speech"
        transcription_begun = time.perf_counter()
        try:
            if self._whisper_backend == "mlx":
                # MLX Whisper transcription
                with self.transcribe_lock:
                    result = mlx_whisper.transcribe(
                        audio,
                        path_or_hf_repo=self._mlx_model_repo,
                        language=resolve_transcription_language(self.cfg),
                    )

                # Capture the language (ISO-639-1) so downstream tools can pick
                # locale-appropriate resources. Whisper echoes back whichever
                # language was pinned, so this holds either way.
                detected = result.get("language")
                if isinstance(detected, str) and detected:
                    self._last_detected_language = detected

                # Filter segments by confidence (MLX Whisper returns segments with avg_logprob)
                min_confidence = getattr(self.cfg, "whisper_min_confidence", 0.3)
                marginal_threshold = min_confidence / 3  # Show user-visible log for marginal confidence
                no_speech_threshold = getattr(self.cfg, "whisper_no_speech_threshold", 0.5)
                segments = result.get("segments", [])

                if segments:
                    filtered_texts = []
                    for seg in segments:
                        avg_logprob = seg.get("avg_logprob", 0)
                        no_speech_prob = seg.get("no_speech_prob", 0)

                        # Convert avg_logprob to confidence (typically -1 to 0, so add 1)
                        confidence = min(1.0, max(0.0, avg_logprob + 1.0))
                        seg_text = seg.get("text", "").strip()

                        # Hard filter: high no_speech_prob means no real speech regardless of logprob.
                        if is_whisper_hallucination(no_speech_prob, no_speech_threshold):
                            debug_log(f"MLX segment filtered (no_speech_prob={no_speech_prob:.2f}): '{seg_text[:50]}'", "voice")
                            continue

                        if confidence < min_confidence:
                            if confidence >= marginal_threshold:
                                # Marginal confidence - show in log viewer (not debug)
                                print(f"🔇 Low confidence ({confidence:.2f}): \"{seg_text[:50]}...\"", flush=True)
                            else:
                                # Very low confidence - debug only
                                debug_log(f"MLX segment filtered (confidence={confidence:.2f}): '{seg_text[:50]}'", "voice")
                            continue

                        filtered_texts.append(seg.get("text", ""))

                    text = " ".join(filtered_texts).strip()
                else:
                    # Fallback to full text if no segments
                    text = result.get("text", "").strip()
            else:
                # faster-whisper transcription
                # CPU mode: skip timestamps and disable context carry-over for speed
                cpu_mode = self._whisper_device == "cpu"
                language = resolve_transcription_language(self.cfg)
                # Whisper's own VAD is the only filter that catches the stock
                # phrase it invents from room noise: that transcript arrives
                # with a no_speech_prob of 0.000 and a confident language
                # identification, so every later filter waves it through.
                vad_filter = bool(getattr(self.cfg, "whisper_vad", True))
                with self.transcribe_lock:
                    try:
                        segments, _info = self.model.transcribe(
                            audio, language=language, vad_filter=vad_filter,
                            condition_on_previous_text=not cpu_mode,
                            without_timestamps=cpu_mode,
                            hotwords=" ".join(dict.fromkeys(filter(None, (
                                str(getattr(self.cfg, "wake_word", "Jarvis") or "Jarvis")
                                .strip().capitalize(),
                                "Vault",
                            )))),
                        )
                    except TypeError:
                        segments, _info = self.model.transcribe(audio, language=language)
                    segments_list = list(segments)
                # Capture the detected language (faster-whisper exposes it
                # on the info object). Guard against older API variants
                # where the attribute may be absent.
                detected = getattr(_info, "language", None)
                if isinstance(detected, str) and detected:
                    self._last_detected_language = detected
                # Hesitant language identification marks the whole utterance as
                # noise, so it is judged before the per-segment filters.
                language_probability = getattr(_info, "language_probability", None)
                min_language_probability = getattr(
                    self.cfg, "whisper_min_language_probability", 0.0
                )
                trace.language = detected if isinstance(detected, str) else None
                trace.language_probability = language_probability
                if is_uncertain_language(language_probability, min_language_probability):
                    debug_log(
                        f"utterance filtered (language={detected} at "
                        f"{language_probability:.2f} < {min_language_probability:.2f})",
                        "voice",
                    )
                    discard_reason = "language_probability"
                    segments_list = []
                filtered_segments = self._filter_noisy_segments(segments_list)
                text = " ".join(seg.text for seg in filtered_segments).strip()
        except Exception as e:
            debug_log(f"transcription error: {e}", "voice")
            if sys.platform == 'win32':
                print(f"  ❌ Whisper error: {e}", flush=True)
            discard_reason = "stt_error"
            get_runtime_state().record_error(f"transcription failed: {e}")
            text = ""
        finally:
            trace.mark("stt", (time.perf_counter() - transcription_begun) * 1000.0)

        if not text or not text.strip():
            get_runtime_state().count_discard(discard_reason)
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        trace.transcript = text

        # Log successful transcription — separator omitted on the first utterance since
        # there is no prior turn to visually separate from.
        separator = "" if self._first_utterance else f"\n{'─' * 50}"
        self._first_utterance = False
        print(f"{separator}\n📝 Heard: \"{text}\"", flush=True)

        # Filter out repetitive hallucinations (e.g., "don't don't don't...")
        if self._is_repetitive_hallucination(text):
            debug_log(f"rejected repetitive hallucination: '{text[:80]}...'", "voice")
            get_runtime_state().count_discard("repetitive")
            self.state_manager.check_hot_window_expiry(self.cfg.voice_debug)
            return

        # Playback is a closed listening interval, so every completed microphone
        # utterance presented to the intent judge is post-playback speech.
        self._transcript_buffer.add(
            text=text,
            start_time=utterance_start_time,
            end_time=utterance_end_time,
            energy=utterance_energy,
            is_during_tts=False,
            language=self._last_detected_language,
        )

        # Process the transcript with pre-calculated energy and utterance timing
        self._process_transcript(text, utterance_energy, utterance_start_time, utterance_end_time)
