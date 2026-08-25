"""Technical readings: GPU, models in memory, paths, and this process.

The 8 GB card is the hard ceiling of this setup, so what is resident and
what is left is the number worth showing first. Everything here is read
from the system on request and cached briefly, because a page that polls
must not turn into a load of its own.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from flask import Blueprint, Response, current_app, jsonify

from jarvis.config import load_settings, resolve_config_path
from jarvis.debug import debug_log


bp = Blueprint("system", __name__, url_prefix="/api")

# Shelling out to nvidia-smi costs about 100 ms, and the readings do not
# change meaningfully faster than this.
CACHE_SECONDS = 2.0
SUBPROCESS_TIMEOUT = 4.0

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, produce) -> Any:
    now = time.monotonic()
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and now - entry[0] < CACHE_SECONDS:
            return entry[1]
    value = produce()
    with _cache_lock:
        _cache[key] = (now, value)
    return value


def _run(command: list[str]) -> Optional[str]:
    """Run a short read-only command, returning None when it is unavailable."""
    if shutil.which(command[0]) is None:
        return None
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        debug_log(f"system reading failed for {command[0]}: {exc}", "webui")
        return None
    return completed.stdout if completed.returncode == 0 else None


def read_gpu() -> Optional[dict]:
    """Name, memory, temperature and load of the first NVIDIA adapter."""
    output = _run([
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu",
        "--format=csv,noheader,nounits",
    ])
    if not output:
        # No NVIDIA tooling: the total is still worth knowing, and the
        # existing detector reads it from the platform instead.
        from jarvis.utils.vram import detect_total_vram_mb

        total = detect_total_vram_mb()
        return {"total_mb": total} if total else None

    first = output.strip().splitlines()[0]
    parts = [part.strip() for part in first.split(",")]
    if len(parts) < 5:
        return None
    try:
        return {
            "name": parts[0],
            "total_mb": int(float(parts[1])),
            "used_mb": int(float(parts[2])),
            "temperature_c": int(float(parts[3])),
            "utilisation_percent": int(float(parts[4])),
        }
    except ValueError:
        return None


def read_loaded_models() -> list[dict]:
    """Which models Ollama is holding, and how much of the card they take."""
    output = _run(["ollama", "ps"])
    if not output:
        return []

    lines = [line for line in output.strip().splitlines() if line.strip()]
    if len(lines) < 2:
        return []

    models = []
    for line in lines[1:]:
        # NAME  ID  SIZE  PROCESSOR  CONTEXT  UNTIL — columns are padded,
        # and both size and until carry spaces, so split on runs of two or
        # more spaces rather than on single ones.
        import re

        columns = re.split(r"\s{2,}", line.strip())
        if len(columns) < 3:
            continue
        models.append({
            "name": columns[0],
            "size": columns[2] if len(columns) > 2 else "",
            "processor": columns[3] if len(columns) > 3 else "",
            "context": columns[4] if len(columns) > 4 else "",
            "until": columns[5] if len(columns) > 5 else "",
        })
    return models


def read_ollama_environment() -> dict[str, str]:
    """The Ollama settings that decide whether models stay resident."""
    keys = (
        "OLLAMA_MAX_LOADED_MODELS",
        "OLLAMA_KEEP_ALIVE",
        "OLLAMA_NUM_PARALLEL",
        "OLLAMA_CONTEXT_LENGTH",
        "OLLAMA_HOST",
    )
    return {key: os.environ.get(key, "") for key in keys}


def _speech_model_reading(name: Optional[str], cache_root: Optional[Path] = None) -> dict:
    """Report the Whisper model by name, not as a path.

    The setting normally holds a model name ("base", "large-v3-turbo") that
    faster-whisper downloads into the Hugging Face cache. Treating it as a
    filesystem path marks every healthy install as missing while it is busy
    transcribing. A setting that does name a real directory is still honoured,
    because some setups point straight at a converted model.
    """
    reading: dict[str, Any] = {"label": "speech model", "name": str(name or ""), "exists": False}
    if not name:
        return reading

    as_path = Path(name)
    if as_path.exists():
        reading["exists"] = True
        reading["path"] = str(as_path)
        return reading

    root = cache_root or (Path.home() / ".cache" / "huggingface" / "hub")
    try:
        # Repositories are named models--<owner>--faster-whisper-<model>. Match
        # the tail exactly so "base" cannot answer for "base.en".
        suffix = f"faster-whisper-{name}"
        for entry in root.iterdir():
            if entry.is_dir() and entry.name.endswith(suffix):
                reading["exists"] = True
                reading["path"] = str(entry)
                return reading
    except OSError:
        pass
    return reading


def _path_reading(label: str, path: Optional[str]) -> dict:
    reading: dict[str, Any] = {"label": label, "path": str(path or ""), "exists": False}
    if not path:
        return reading
    target = Path(path)
    reading["exists"] = target.exists()
    try:
        if target.is_file():
            reading["size_bytes"] = target.stat().st_size
        usage = shutil.disk_usage(target if target.exists() else target.parent)
        reading["disk_free_bytes"] = usage.free
        reading["disk_total_bytes"] = usage.total
    except OSError:
        pass
    return reading


def read_process() -> dict:
    reading: dict[str, Any] = {
        "pid": os.getpid(),
        "python": platform.python_version(),
        "platform": f"{platform.system()} {platform.release()}",
    }
    try:
        import psutil

        process = psutil.Process()
        reading["rss_bytes"] = process.memory_info().rss
        reading["threads"] = process.num_threads()
        reading["started_at"] = process.create_time()
    except Exception as exc:
        debug_log(f"process reading unavailable: {exc}", "webui")
    return reading


def _speech_output_reading(cfg) -> dict[str, Any]:
    """Describe the configured voice without labelling a fallback as active."""
    from jarvis.output.tts import resolve_kokoro_voice_language, resolve_voice_language

    engine = str(cfg.tts_engine or "piper")
    voice_engine = (
        str(cfg.tts_local_fallback_engine or "piper")
        if engine == "cloud" else engine
    )
    if voice_engine == "piper":
        model = getattr(cfg, "tts_piper_model_path", "") or ""
        language = resolve_voice_language(model)
    elif voice_engine == "kokoro":
        model = getattr(cfg, "tts_kokoro_voice", "") or ""
        language = resolve_kokoro_voice_language(model)
    elif voice_engine == "chatterbox":
        model = getattr(cfg, "tts_chatterbox_audio_prompt", "") or ""
        language = "English"
    else:
        model = ""
        language = None

    providers = [
        {
            "name": str(provider.get("name", "")),
            "provider": str(provider.get("provider", "")),
            "model": str(provider.get("model", "")),
            "enabled": bool(provider.get("enabled", True)),
        }
        for provider in getattr(cfg, "tts_cloud_providers", [])
        if isinstance(provider, dict)
    ]
    return {
        "engine": engine,
        "enabled": bool(cfg.tts_enabled),
        "model": model,
        "language": language,
        "cloud_providers": providers,
        "local_fallback_engine": str(cfg.tts_local_fallback_engine or "piper"),
    }


@bp.route("/system/restart", methods=["POST"])
def restart() -> Response:
    """Tear down every component and start a fresh generation in the same process.

    Returns before the restart happens: the daemon's main loop notices the
    request on its next poll (at most a second later), runs the same
    shutdown path a normal stop takes, then reinitialises. A page open
    against this server will see the connection drop and can poll
    ``/api/health`` to know when the new generation is ready.
    """
    if current_app.config["JARVIS_WEBUI"].standalone:
        return jsonify(error="no daemon is running"), 409

    from jarvis.daemon import request_restart

    request_restart()
    return jsonify({"restarting": True})


@bp.route("/system")
def system() -> Response:
    """Everything the technical view shows, in one reading."""
    cfg = load_settings()
    piper_model = getattr(cfg, "tts_piper_model_path", "") or ""

    return jsonify({
        "gpu": _cached("gpu", read_gpu),
        "models": {
            "loaded": _cached("ollama_ps", read_loaded_models),
            "chat": cfg.llm_chat_model,
            "fast": getattr(cfg, "fast_model", "") or cfg.llm_chat_model,
            "embedding": cfg.embedding_model,
            "provider": cfg.llm_provider,
            "base_url": cfg.llm_base_url,
        },
        "ollama_environment": read_ollama_environment(),
        "speech_recognition": {
            "model": cfg.whisper_model,
            "backend": cfg.whisper_backend,
            "device": cfg.whisper_device,
            "compute_type": cfg.whisper_compute_type,
            "vad": bool(getattr(cfg, "whisper_vad", True)),
            "language": getattr(cfg, "whisper_language", "") or "auto",
            "min_language_probability": getattr(cfg, "whisper_min_language_probability", 0.0),
        },
        "speech_output": _speech_output_reading(cfg),
        "paths": [
            _path_reading("config", str(resolve_config_path())),
            _path_reading("database", cfg.db_path),
            _path_reading("turn journal", str(Path(cfg.db_path).parent / "turns.jsonl")),
            _speech_model_reading(cfg.whisper_model),
            _path_reading("voice model", piper_model),
        ],
        "process": read_process(),
        "interpreter": sys.executable,
    })
