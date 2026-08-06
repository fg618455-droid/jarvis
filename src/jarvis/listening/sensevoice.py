"""SenseVoice (FunASR) speech-recognition engine.

SenseVoiceSmall is the offline speech-recognition engine: non-autoregressive
(much faster than Whisper on CPU), supporting Mandarin, Cantonese, English,
Japanese, and Korean, emitting language / emotion / event tags alongside the
transcript.

The wrapper keeps the heavy ``funasr`` / ``torch`` import lazy so that
importing lightweight listening submodules (e.g. ``echo_detection``)
does not drag them in via ``listener.py``.
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np

from ..debug import debug_log

# SenseVoiceSmall language-identification tags (mirrors ``lid_dict`` in
# funasr's sense_voice model: auto/zh/en/yue/ja/ko/nospeech).
_LID_TAG_RE = re.compile(r"<\|(zh|yue|en|ja|ko|nospeech)\|>", re.IGNORECASE)
_TAG_RE = re.compile(r"<\|[^|]*\|>")
_NOSPEECH_RE = re.compile(r"<\|nospeech\|>", re.IGNORECASE)

DEFAULT_MODEL_ID = "FunAudioLLM/SenseVoiceSmall"
BUNDLED_MODEL_DIR_NAME = "SenseVoiceSmall"

# Repository root for dev checkouts: src/jarvis/listening/sensevoice.py
# is four levels below it. Frozen builds don't have a repo root.
_REPO_ROOT = Path(__file__).resolve().parents[3]

_AutoModel = None  # cached funasr.AutoModel class (or None on import failure)


class SenseVoiceUnavailableError(RuntimeError):
    """Raised when funasr is not installed / cannot be imported."""


def _get_auto_model():
    """Lazily import and cache ``funasr.AutoModel``.

    Returns the class or ``None`` when funasr (or its import chain, e.g.
    torch) is unavailable. Cached so repeated probes are free.
    """
    global _AutoModel
    if _AutoModel is None:
        try:
            from funasr import AutoModel as _am

            _AutoModel = _am
        except Exception as exc:  # noqa: BLE001 - import chains vary wildly
            debug_log(f"funasr import failed: {exc}", "voice")
            _AutoModel = False
    return _AutoModel or None


def is_available() -> bool:
    """True when funasr can be imported (i.e. the engine can load)."""
    return _get_auto_model() is not None


def _is_apple_silicon() -> bool:
    import platform

    return sys.platform == "darwin" and platform.machine() == "arm64"


def _torch_cuda_available() -> bool:
    try:
        import torch

        return bool(torch.cuda.is_available())
    except Exception:
        return False


def _torch_mps_available() -> bool:
    try:
        import torch

        return bool(
            getattr(torch.backends, "mps", None) is not None
            and torch.backends.mps.is_available()
        )
    except Exception:
        return False


def resolve_device(pref: str = "auto") -> str:
    """Resolve the FunASR device string from a preference.

    ``"auto"`` picks the best available device: CUDA, then Apple Silicon
    MPS, then CPU. An explicit preference is returned verbatim — funasr
    itself falls back to CPU when the requested device is unavailable.
    """
    if pref != "auto":
        return pref
    if _torch_cuda_available():
        return "cuda:0"
    if _is_apple_silicon() and _torch_mps_available():
        return "mps"
    return "cpu"


def bundled_model_dir() -> Optional[str]:
    """Return the path of the model bundled with the app, if present.

    Frozen builds ship the weights under ``models/SenseVoiceSmall`` next
    to the executable (or inside ``_MEIPASS`` for onefile bundles); dev
    checkouts can place them at the repository root's ``models/`` (see
    ``scripts/fetch_sensevoice_model.py``).
    """
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        base = Path(sys.executable).parent
        candidates.append(base / "models" / BUNDLED_MODEL_DIR_NAME)
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "models" / BUNDLED_MODEL_DIR_NAME)
    else:
        # Dev checkout: same directory scripts/fetch_sensevoice_model.py and
        # the CI build populate (repository root models/SenseVoiceSmall).
        candidates.append(_REPO_ROOT / "models" / BUNDLED_MODEL_DIR_NAME)
    for candidate in candidates:
        if candidate.is_dir():
            return str(candidate)
    return None


def default_model_dir() -> Path:
    """Persistent local directory for the model weights.

    Dev checkouts share the repository ``models/`` dir (the same location
    ``scripts/fetch_sensevoice_model.py`` and the CI build use, so a dev
    download and a CI-cached build land in the same place). Frozen apps
    without bundled weights store the download next to the config file.
    """
    if not getattr(sys, "frozen", False):
        return _REPO_ROOT / "models" / BUNDLED_MODEL_DIR_NAME
    from ..config import default_config_path

    return Path(default_config_path()).parent / "models" / BUNDLED_MODEL_DIR_NAME


def download_model(model_id: str, target_dir: Optional[Path] = None) -> str:
    """Download the model weights into a local directory and return its path.

    Uses ``snapshot_download(local_dir=...)`` so files are copied rather
    than symlinked. The plain HF-cache path creates symlinks, which
    Windows refuses without Developer Mode or admin (WinError 1314), and
    funasr surfaces that as a scary "Download failed!" warning even when
    the copy fallback then succeeds. A local dir also keeps first-run
    progress visible and is reused across runs.
    """
    target = Path(target_dir or default_model_dir())
    target.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import snapshot_download

    print(
        f"     🎤 Downloading SenseVoice model '{model_id}' to {target} "
        "(one-time, ~940 MB)...",
        flush=True,
    )
    snapshot_download(repo_id=model_id, local_dir=str(target))
    return str(target)


def resolve_model_ref(configured: Optional[str]) -> tuple[str, str]:
    """Return ``(model, hub)`` for ``funasr.AutoModel``.

    A bundled model directory wins over the configured id so installs that
    ship the weights never touch the network. Otherwise the configured id
    is used, with the hub guessed from the id prefix: ``"iic/"`` is a
    ModelScope id, anything else is a HuggingFace id.
    """
    bundled = bundled_model_dir()
    if bundled:
        return bundled, "hf"
    model = (configured or DEFAULT_MODEL_ID).strip() or DEFAULT_MODEL_ID
    hub = "ms" if model.startswith("iic/") else "hf"
    return model, hub


def clean_transcript(raw: Optional[str]) -> tuple[str, Optional[str], bool]:
    """Strip SenseVoice rich tags, returning ``(text, language, no_speech)``.

    Raw output looks like ``"<|zh|><|NEUTRAL|><|Speech|><|woitn|>大家好"``.
    Emotion/event tags are dropped — they would be noise in a query
    pipeline. The language tag (``zh``/``yue``/``en``/``ja``/``ko``) is
    returned for downstream language-aware features, and ``<|nospeech|>``
    marks a non-speech clip (the analogue of Whisper's ``no_speech_prob``
    gate, which SenseVoice does not expose).
    """
    if not raw:
        return "", None, False
    no_speech = bool(_NOSPEECH_RE.search(raw))
    language: Optional[str] = None
    match = _LID_TAG_RE.search(raw)
    if match:
        tag = match.group(1).lower()
        if tag == "nospeech":
            no_speech = True
        else:
            language = tag
    text = _TAG_RE.sub("", raw).strip()
    return text, language, no_speech


@dataclass
class SenseVoiceResult:
    """Result of one transcription call."""

    text: str
    language: Optional[str]  # zh / yue / en / ja / ko
    no_speech: bool


class SenseVoiceEngine:
    """Offline speech recognition via FunASR SenseVoiceSmall.

    One instance is shared by the voice listener and the dictation
    engine (both serialise calls through the same lock); do not construct
    it directly, use :meth:`load`.
    """

    def __init__(self, auto_model, model: str, device: str, model_path: str):
        self._auto = auto_model
        self.model = model
        self.device = device
        self.model_path = model_path

    @classmethod
    def load(
        cls, model: Optional[str] = None, device: str = "auto"
    ) -> "SenseVoiceEngine":
        """Load the engine.

        Raises :class:`SenseVoiceUnavailableError` when funasr is not
        installed; model initialisation errors (e.g. download failures)
        propagate to the caller, which prints the user-facing message.

        The model is resolved to a local directory whenever possible:
        bundled weights first, then the configured id is downloaded into
        ``default_model_dir()`` (file copies — no HF cache symlinks, which
        fail on Windows without symlink privileges).
        """
        auto_model_cls = _get_auto_model()
        if auto_model_cls is None:
            raise SenseVoiceUnavailableError(
                "funasr is not installed (run: pip install funasr)"
            )
        resolved_device = resolve_device(device)
        model_ref, hub = resolve_model_ref(model)
        if not os.path.isdir(model_ref):
            model_ref = download_model(model_ref)
        try:
            auto = auto_model_cls(
                model=model_ref,
                device=resolved_device,
                hub=hub,
                disable_update=True,
                disable_pbar=True,
            )
        except TypeError:
            # Older funasr versions may not accept disable_pbar.
            auto = auto_model_cls(
                model=model_ref,
                device=resolved_device,
                hub=hub,
                disable_update=True,
            )
        model_path = str(getattr(auto, "model_path", None) or model_ref)
        debug_log(
            f"sensevoice loaded: model={model_ref} device={resolved_device} "
            f"path={model_path}",
            "voice",
        )
        return cls(auto_model=auto, model=model_ref, device=resolved_device, model_path=model_path)

    def transcribe(self, audio: np.ndarray) -> SenseVoiceResult:
        """Transcribe one float32 16 kHz mono utterance (≤ ~30 s).

        Never raises: transcription failures degrade to an empty result so
        the audio loop stays alive.
        """
        try:
            results = self._auto.generate(
                input=audio,
                cache={},
                language="auto",
                use_itn=True,
            )
        except Exception as exc:
            debug_log(f"sensevoice transcription error: {exc}", "voice")
            return SenseVoiceResult(text="", language=None, no_speech=False)
        if not results or not isinstance(results, list):
            return SenseVoiceResult(text="", language=None, no_speech=False)
        raw = str(results[0].get("text", "") or "")
        text, language, no_speech = clean_transcript(raw)
        return SenseVoiceResult(text=text, language=language, no_speech=no_speech)

    def warmup(self, samplerate: int) -> None:
        """Run one non-silent transcription so the first real utterance
        doesn't pay the cold-decode cost. Mirror the real transcribe call.

        Low-amplitude noise (not silence) is used so the decoder actually
        runs — a silent clip can short-circuit as no-speech.
        """
        try:
            rng = np.random.default_rng(0)
            audio = rng.standard_normal(samplerate).astype(np.float32) * 0.01
            self.transcribe(audio)
        except Exception as exc:
            debug_log(f"sensevoice warmup failed: {exc}", "voice")
