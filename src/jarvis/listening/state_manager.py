"""State management for wake-word and follow-up listening modes."""

import time
import threading
from typing import Optional
from enum import Enum
from datetime import datetime

from ..debug import debug_log


class ListeningState(Enum):
    """Possible listening states."""
    WAKE_WORD = "wake_word"      # Waiting for wake word
    HOT_WINDOW = "hot_window"    # Listening without wake word after TTS


class StateManager:
    """Manages listening state transitions and timing."""

    def __init__(self, hot_window_seconds: float = 3.0, echo_tolerance: float = 0.3):
        """
        Initialize state manager.

        Args:
            hot_window_seconds: Duration of hot window listening
            echo_tolerance: Delay before activating hot window (for echo suppression)
        """
        self.hot_window_seconds = hot_window_seconds
        self.echo_tolerance = echo_tolerance

        # Current state
        self._state = ListeningState.WAKE_WORD
        self._state_lock = threading.Lock()

        # Hot window state
        self._hot_window_start_time: float = 0.0
        self._hot_window_span_start: float = 0.0  # When window span began (schedule time)
        self._hot_window_span_end: float = 0.0     # When window span ended (expiry time)

        # Timer-based hot window management
        self._hot_window_activation_timer: Optional[threading.Timer] = None
        self._hot_window_expiry_timer: Optional[threading.Timer] = None
        self._timer_lock = threading.Lock()
        self._voice_debug: bool = False  # Cache for use in timer callbacks

        # Stop flag for background threads
        self._should_stop = False

    def get_state(self) -> ListeningState:
        """Get current listening state."""
        with self._state_lock:
            return self._state

    def is_hot_window_active(self) -> bool:
        """Check if hot window is currently active."""
        return self.get_state() == ListeningState.HOT_WINDOW

    def was_speech_during_hot_window(self, utterance_start_time: float,
                                     utterance_end_time: float = 0.0) -> bool:
        """Check if speech overlapped with the hot window time span.

        Uses timestamps instead of a mutable boolean flag. This eliminates
        race conditions between the hot window expiry timer and slow Whisper
        transcription — the check works regardless of when the transcript arrives.

        Args:
            utterance_start_time: When VAD detected voice onset (time.time()).
                                  If 0, falls back to current state check.
            utterance_end_time: When the utterance ended (time.time()).
                                Used to detect overlap when transcription
                                completes after the window expires.

        Returns:
            True if:
            - Hot window is currently active, OR
            - Hot window activation is pending (echo_tolerance delay), OR
            - Speech started during the window span (even if window has since expired)
            - Speech started before the span but ended during it (overlap)
        """
        with self._state_lock:
            is_active = self._state == ListeningState.HOT_WINDOW
            span_start = self._hot_window_span_start
            span_end = self._hot_window_span_end

        with self._timer_lock:
            is_pending = self._hot_window_activation_timer is not None

        # Currently active — always accept regardless of timing
        if is_active:
            return True

        # No timestamp — fall back to current state
        if utterance_start_time <= 0:
            return is_pending

        # Pending activation — accept if speech started after scheduling
        if is_pending:
            return span_start <= 0 or utterance_start_time >= span_start

        # Window expired — accept if speech overlapped with the recorded span.
        if span_start > 0 and span_end > 0:
            if span_start <= utterance_start_time <= span_end:
                return True
            if (utterance_end_time > 0
                    and utterance_start_time < span_start
                    and utterance_end_time >= span_start):
                debug_log(
                    f"utterance overlaps hot window span "
                    f"(start={utterance_start_time:.2f} < span_start={span_start:.2f}, "
                    f"end={utterance_end_time:.2f} >= span_start)", "state"
                )
                return True

        return False

    def cancel_hot_window_activation(self) -> None:
        """Cancel any pending hot window activation timer.

        Call this when user starts a new query to prevent delayed activation
        from interfering with the current interaction.
        """
        with self._timer_lock:
            if self._hot_window_activation_timer is not None:
                self._hot_window_activation_timer.cancel()
                self._hot_window_activation_timer = None
                debug_log("cancelled pending hot window activation", "state")

    def _cancel_hot_window_expiry_timer(self) -> None:
        """Cancel the hot window expiry timer."""
        with self._timer_lock:
            if self._hot_window_expiry_timer is not None:
                self._hot_window_expiry_timer.cancel()
                self._hot_window_expiry_timer = None

    def reset_hot_window_expiry(self) -> None:
        """Reset the hot window expiry timer to give the user the full window.

        Called when echo is rejected during the hot window, so the time spent
        processing echo doesn't eat into the user's actual follow-up window.

        If the hot window already expired while the echo was being transcribed,
        this reactivates it — the user shouldn't lose their follow-up window
        just because Whisper was slow to produce the echo transcript.
        """
        with self._state_lock:
            if self._state == ListeningState.HOT_WINDOW:
                # Still active — just reset the timer
                self._hot_window_start_time = time.time()
            elif self._state == ListeningState.WAKE_WORD:
                # Expired while processing echo — reactivate
                self._state = ListeningState.HOT_WINDOW
                self._hot_window_start_time = time.time()
                debug_log("hot window reactivated (expired during echo processing)", "state")
                try:
                    print(f"👂 Listening for follow-up ({int(self.hot_window_seconds)}s)...", flush=True)
                except Exception:
                    pass
            else:
                return

        self._schedule_hot_window_expiry()
        debug_log(f"hot window expiry reset (echo rejected, restarting {self.hot_window_seconds}s timer)", "state")

    def _schedule_hot_window_expiry(self) -> None:
        """Schedule hot window expiry timer.

        This timer guarantees expiry will fire even if no audio is being processed.
        """
        self._cancel_hot_window_expiry_timer()

        def _expire():
            with self._state_lock:
                if self._state != ListeningState.HOT_WINDOW:
                    return
                self._state = ListeningState.WAKE_WORD
                self._hot_window_span_end = time.time()

            expiry_time = self._hot_window_span_end
            duration = expiry_time - self._hot_window_start_time if self._hot_window_start_time > 0 else 0
            expiry_time_str = datetime.fromtimestamp(expiry_time).strftime('%H:%M:%S.%f')[:-3]
            debug_log(f"hot window expired (timer) at {expiry_time_str} after {duration:.2f}s", "state")

            # Set face state to IDLE
            try:
                from desktop_app.face_widget import get_jarvis_state, JarvisState
                face_state_manager = get_jarvis_state()
                face_state_manager.set_state(JarvisState.IDLE)
                debug_log("face state set to IDLE (hot window timer expiry)", "state")
            except ImportError:
                # Desktop app not available (headless mode)
                pass
            except Exception as e:
                debug_log(f"failed to set face state to IDLE: {e}", "state")

            # Always show user-facing output
            try:
                print("💤 Returning to wake word mode\n", flush=True)
            except Exception:
                pass

        with self._timer_lock:
            self._hot_window_expiry_timer = threading.Timer(self.hot_window_seconds, _expire)
            self._hot_window_expiry_timer.daemon = True
            self._hot_window_expiry_timer.start()

        debug_log(f"scheduled hot window expiry in {self.hot_window_seconds}s", "state")

    def schedule_hot_window_activation(self, voice_debug: bool = False) -> None:
        """
        Schedule hot window activation after echo tolerance delay.

        Uses threading.Timer for reliable activation instead of daemon thread + sleep.

        Args:
            voice_debug: Whether to enable debug logging
        """
        schedule_time_str = datetime.fromtimestamp(time.time()).strftime('%H:%M:%S.%f')[:-3]
        debug_log(f"scheduling hot window activation at {schedule_time_str} (delay={self.echo_tolerance}s, should_stop={self._should_stop})", "state")

        # Cancel any pending activation first
        self.cancel_hot_window_activation()

        # Start a new window span — reset end so old expired spans don't interfere
        with self._state_lock:
            self._hot_window_span_start = time.time()
            self._hot_window_span_end = 0.0

        # Cache voice_debug for use in timer callbacks
        self._voice_debug = voice_debug

        def _activate():
            # Clear the timer reference now that it's fired
            with self._timer_lock:
                self._hot_window_activation_timer = None

            # Check if we should still activate
            if self._should_stop:
                debug_log("hot window activation cancelled (should_stop=True)", "state")
                return

            with self._state_lock:
                self._state = ListeningState.HOT_WINDOW
                self._hot_window_start_time = time.time()

            activation_time_str = datetime.fromtimestamp(self._hot_window_start_time).strftime('%H:%M:%S.%f')[:-3]
            debug_log(f"hot window activated at {activation_time_str} for {self.hot_window_seconds}s (after {self.echo_tolerance}s echo delay)", "state")

            # Set face state to LISTENING
            try:
                from desktop_app.face_widget import get_jarvis_state, JarvisState
                face_state_manager = get_jarvis_state()
                face_state_manager.set_state(JarvisState.LISTENING)
                debug_log("face state set to LISTENING (hot window activated)", "state")
            except ImportError:
                pass
            except Exception as e:
                debug_log(f"failed to set face state to LISTENING: {e}", "state")

            # Always show user-facing output
            try:
                print(f"👂 Listening for follow-up ({int(self.hot_window_seconds)}s)...", flush=True)
            except Exception as e:
                debug_log(f"failed to print hot window message: {e}", "state")

            # Schedule the expiry timer now that hot window is active
            self._schedule_hot_window_expiry()

        # Use Timer for more reliable activation
        with self._timer_lock:
            self._hot_window_activation_timer = threading.Timer(self.echo_tolerance, _activate)
            self._hot_window_activation_timer.daemon = True
            self._hot_window_activation_timer.start()

        debug_log("hot window activation timer started", "state")

    def _should_expire_hot_window(self) -> bool:
        """Check if hot window should expire due to timeout.

        Note: With timer-based expiry, this is now mainly a fallback check.
        The timer should handle expiry automatically.
        """
        if not self.is_hot_window_active():
            return False
        current_time = time.time()
        return (current_time - self._hot_window_start_time) >= self.hot_window_seconds

    def check_hot_window_expiry(self, voice_debug: bool = False) -> bool:
        """
        Check and handle hot window expiry.

        Note: With timer-based expiry, this is now a fallback check.
        The timer should handle expiry automatically, but this method
        provides a synchronous check for the main audio processing loop.

        Args:
            voice_debug: Whether to enable debug logging

        Returns:
            True if hot window was expired
        """
        if self._should_expire_hot_window():
            # Cancel expiry timer since we're handling it here
            self._cancel_hot_window_expiry_timer()

            with self._state_lock:
                self._state = ListeningState.WAKE_WORD
                self._hot_window_span_end = time.time()

            debug_log("hot window expired (poll)", "state")

            # Set face state to IDLE (awake and ready, waiting for wake word)
            try:
                from desktop_app.face_widget import get_jarvis_state, JarvisState
                face_state_manager = get_jarvis_state()
                face_state_manager.set_state(JarvisState.IDLE)
                debug_log("face state set to IDLE (hot window poll expiry)", "state")
            except ImportError:
                pass
            except Exception as e:
                debug_log(f"failed to set face state to IDLE: {e}", "state")

            # Always show user-facing output
            try:
                print("💤 Returning to wake word mode\n", flush=True)
            except Exception:
                pass

            return True
        return False

    def expire_hot_window(self, voice_debug: bool = False) -> None:
        """
        Manually expire the hot window.

        Args:
            voice_debug: Whether to enable debug logging
        """
        # Cancel expiry timer since we're manually expiring
        self._cancel_hot_window_expiry_timer()

        if self.is_hot_window_active():
            with self._state_lock:
                self._state = ListeningState.WAKE_WORD
                self._hot_window_span_end = time.time()

            debug_log("hot window manually expired", "state")

            # Set face state to IDLE (awake and ready, waiting for wake word)
            try:
                from desktop_app.face_widget import get_jarvis_state, JarvisState
                face_state_manager = get_jarvis_state()
                face_state_manager.set_state(JarvisState.IDLE)
                debug_log("face state set to IDLE (hot window manually expired)", "state")
            except ImportError:
                pass
            except Exception as e:
                debug_log(f"failed to set face state to IDLE: {e}", "state")

            # Always show user-facing output
            try:
                print("💤 Returning to wake word mode", flush=True)
            except Exception:
                pass

    def stop(self) -> None:
        """Stop the state manager and cancel all timers."""
        self._should_stop = True

        # Cancel all timers
        self.cancel_hot_window_activation()
        self._cancel_hot_window_expiry_timer()

        with self._state_lock:
            self._state = ListeningState.WAKE_WORD
