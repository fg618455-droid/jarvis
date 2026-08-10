"""⚙️ Config field metadata.

Describes every user-facing key in ``config.json``: its label, help text,
category, and the widget shape a settings interface should render for it.
Both the Qt settings window and the control centre build their forms from
this one registry, so a new config key becomes editable in both places by
adding a single entry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, List, Optional

from jarvis.config import SUPPORTED_CHAT_MODELS


@dataclass
class FieldMeta:
    """Metadata for a single config field."""
    key: str
    label: str
    description: str
    category: str
    field_type: str  # "bool", "int", "float", "str", "choice", "device", "list"
    choices: Optional[List[tuple[str, str]]] = None  # [(value, display), ...]
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    step: Optional[float] = None
    suffix: Optional[str] = None
    nullable: bool = False  # Whether None/"" is a valid value (shows "Default" option)


# Categories and their display order
CATEGORIES = [
    ("llm", "🤖 LLM & AI Models"),
    ("llm_provider", "🔌 LLM Provider"),
    ("tts", "🔊 Text-to-Speech"),
    ("piper", "🎵 Piper TTS"),
    ("chatterbox", "🎭 Chatterbox TTS"),
    ("voice_input", "🎤 Voice Input"),
    ("wake", "👂 Wake Word"),
    ("whisper", "🗣️ Speech Recognition"),
    ("vad", "📊 Voice Activity Detection"),
    ("timing", "⏱️ Timing & Windows"),
    ("memory", "🧠 Memory & Dialogue"),
    ("security", "🔐 Security"),
    ("location", "📍 Location"),
    ("features", "✨ Features"),
    ("webui", "🖥️ Control Centre"),
    ("mcps", "🔌 MCP Servers"),
    ("advanced", "🔧 Advanced"),
]


def _is_default_value(val: Any, default_val: Any) -> bool:
    """True when ``val`` should be treated as the default and omitted from
    ``config.json`` (the minimal-config invariant).

    A value equal to the default is omitted. An emptied nullable field reads
    back as ``None``; treat that as the default when the default is itself
    empty (``""`` or ``None``) so we never persist a ``null`` for a field
    that would just fall back anyway.
    """
    if val == default_val:
        return True
    return val is None and default_val in (None, "")


def _dictation_hotkey_choices() -> list:
    """Build platform-aware dictation hotkey dropdown choices."""
    from jarvis.dictation.dictation_engine import format_hotkey_display
    from jarvis.config import _default_dictation_hotkey
    default = _default_dictation_hotkey()
    options = [
        ("ctrl+alt", format_hotkey_display("ctrl+alt")),
        ("ctrl+cmd", format_hotkey_display("ctrl+cmd")),
        ("ctrl+shift+d", format_hotkey_display("ctrl+shift+d")),
        ("ctrl+shift", format_hotkey_display("ctrl+shift")),
    ]
    return [
        (val, f"{label} (default)" if val == default else label)
        for val, label in options
    ]


def _build_field_metadata() -> List[FieldMeta]:
    """Build the metadata registry for all user-facing config fields."""
    fields = []

    def f(key, label, desc, cat, ftype, **kw):
        fields.append(FieldMeta(key=key, label=label, description=desc,
                                category=cat, field_type=ftype, **kw))

    # --- LLM & AI Models ---
    model_choices = [(mid, info["name"]) for mid, info in SUPPORTED_CHAT_MODELS.items()]
    f("ollama_chat_model", "Chat Model", "Primary LLM for conversations",
      "llm", "choice", choices=model_choices)
    f("ollama_embed_model", "Embedding Model", "Model for text embeddings",
      "llm", "str")
    f("ollama_base_url", "Ollama URL", "Ollama server base URL",
      "llm", "str")
    f("llm_chat_timeout_sec", "Chat Timeout", "Max seconds for chat responses",
      "llm", "float", min_val=10, max_val=600, step=10, suffix="s")
    f("llm_tools_timeout_sec", "Tools Timeout", "Max seconds for tool calls",
      "llm", "float", min_val=10, max_val=600, step=10, suffix="s")
    f("llm_embedding_timeout_sec", "Embedding Timeout", "Max seconds for embeddings",
      "llm", "float", min_val=5, max_val=300, step=5, suffix="s")
    f("llm_profile_select_timeout_sec", "Profile Select Timeout",
      "Max seconds for profile selection",
      "llm", "float", min_val=5, max_val=120, step=5, suffix="s")
    f("fast_model", "Fast Model",
      "Small, quick model for real-time work: voice intent, tool routing, "
      "quick classifications. Automatic picks the right default for your provider",
      "llm", "choice", choices=[("", "Automatic (recommended)")] + model_choices)
    f("intent_judge_timeout_sec", "Intent Judge Timeout",
      "Max seconds for intent judgement",
      "llm", "float", min_val=1, max_val=30, step=0.5, suffix="s")
    f("llm_thinking_enabled", "Chat Thinking Mode",
      "Let the chat model think/reason before answering (slower but may improve quality)",
      "llm", "bool")
    f("intent_judge_thinking_enabled", "Intent Judge Thinking Mode",
      "Let the intent judge think before classifying (adds latency to wake detection)",
      "llm", "bool")

    # --- LLM Provider ---
    # Selects which local runtime serves the LLM. The connection and model
    # fields below are nullable: leaving them empty falls back to the Ollama
    # settings on the "LLM & AI Models" page, so a default (Ollama) install
    # never needs to touch this page.
    f("llm_provider", "Provider", "Which local runtime serves the LLM",
      "llm_provider", "choice",
      choices=[("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("llm_base_url", "Base URL",
      "Provider API base URL (e.g. http://localhost:1234/v1 for LM Studio). "
      "Leave empty to use the Ollama URL.",
      "llm_provider", "str", nullable=True)
    f("llm_api_key", "API Key",
      "Bearer token for the provider, if it requires one. Leave empty for none.",
      "llm_provider", "password", nullable=True)
    f("llm_chat_model", "Chat Model",
      "Model name the provider exposes. Leave empty to use the Ollama chat model.",
      "llm_provider", "str", nullable=True)
    f("embedding_provider", "Embedding Provider",
      "Runtime for embeddings. Leave on 'Same as chat provider' unless your "
      "chat runtime has no embeddings endpoint (then route them to Ollama).",
      "llm_provider", "choice",
      choices=[("", "Same as chat provider"),
               ("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("embedding_base_url", "Embedding Base URL",
      "Override base URL for embeddings. Leave empty to inherit from the "
      "chat provider (or the Ollama URL).",
      "llm_provider", "str", nullable=True)
    f("embedding_api_key", "Embedding API Key",
      "Override bearer token for embeddings. Leave empty to inherit the chat key.",
      "llm_provider", "password", nullable=True)
    f("embedding_model", "Embedding Model",
      "Embedding model name. Leave empty to use the Ollama embedding model.",
      "llm_provider", "str", nullable=True)

    # --- Text-to-Speech ---
    f("tts_enabled", "Enable TTS", "Enable text-to-speech output",
      "tts", "bool")
    f("tts_engine", "TTS Engine", "Speech synthesis engine",
      "tts", "choice", choices=[("piper", "Piper (Neural)"), ("chatterbox", "Chatterbox (Voice Cloning)")])
    f("tts_rate", "Speech Rate", "Words per minute (200 = normal)",
      "tts", "int", min_val=80, max_val=400, step=10, suffix="WPM", nullable=True)

    # --- Piper TTS ---
    f("tts_piper_length_scale", "Speed Scale",
      "Speech speed: <1.0 faster, >1.0 slower",
      "piper", "float", min_val=0.1, max_val=3.0, step=0.05)
    f("tts_piper_noise_scale", "Audio Variation",
      "Higher = more expressive",
      "piper", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_piper_noise_w", "Phoneme Width Variation",
      "Higher = more lively rhythm",
      "piper", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_piper_sentence_silence", "Sentence Silence",
      "Pause after each sentence",
      "piper", "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s")
    f("tts_piper_model_path", "Custom Voice Model",
      "Path to .onnx voice model (leave empty for default)",
      "piper", "str", nullable=True)
    f("tts_piper_speaker", "Speaker ID",
      "Speaker index for multi-speaker models",
      "piper", "int", min_val=0, max_val=99, nullable=True)

    # --- Chatterbox TTS ---
    f("tts_chatterbox_device", "Device",
      "Compute device for Chatterbox",
      "chatterbox", "choice",
      choices=[("cuda", "CUDA (GPU)"), ("auto", "Auto"), ("cpu", "CPU")])
    f("tts_chatterbox_exaggeration", "Exaggeration",
      "Emotion exaggeration (0.0–1.0+)",
      "chatterbox", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_chatterbox_cfg_weight", "CFG Weight",
      "Quality/speed trade-off",
      "chatterbox", "float", min_val=0.0, max_val=2.0, step=0.05)
    f("tts_chatterbox_audio_prompt", "Voice Clone Audio",
      "Path to audio file for voice cloning (leave empty to disable)",
      "chatterbox", "str", nullable=True)

    # --- Voice Input ---
    f("voice_device", "Input Device",
      "Microphone device (name or index). Leave empty for system default.",
      "voice_input", "device")
    f("sample_rate", "Sample Rate",
      "Audio sample rate in Hz",
      "voice_input", "choice",
      choices=[("16000", "16000 Hz"), ("44100", "44100 Hz"), ("48000", "48000 Hz")])
    f("voice_min_energy", "Min Energy",
      "Minimum audio energy to register voice",
      "voice_input", "float", min_val=0.0, max_val=1.0, step=0.005)

    # --- Wake Word ---
    f("wake_word", "Wake Word",
      "Primary wake word to activate Jarvis",
      "wake", "str")
    f("wake_fuzzy_ratio", "Fuzzy Match Ratio",
      "How loosely to match the wake word (0.0–1.0)",
      "wake", "float", min_val=0.5, max_val=1.0, step=0.01)
    # --- Whisper ---
    f("whisper_model", "Model Size",
      "Whisper model size (tiny/base/small/medium/large)",
      "whisper", "choice",
      choices=[("tiny", "Tiny"), ("base", "Base"), ("small", "Small"),
               ("medium", "Medium"), ("large-v3", "Large v3")])
    f("whisper_backend", "Backend",
      "Speech recognition backend",
      "whisper", "choice",
      choices=[("auto", "Auto"), ("mlx", "MLX (Apple Silicon)"),
               ("faster-whisper", "Faster Whisper")])
    f("whisper_device", "Compute Device",
      "Device for Whisper inference",
      "whisper", "choice",
      choices=[("auto", "Auto"), ("cuda", "CUDA (GPU)"), ("cpu", "CPU")])
    f("whisper_compute_type", "Compute Type",
      "Quantisation level for inference",
      "whisper", "choice",
      choices=[("int8", "INT8 (Fast)"), ("float16", "Float16"), ("float32", "Float32")])
    f("whisper_vad", "Use VAD Filter",
      "Filter audio with VAD before transcription",
      "whisper", "bool")
    f("whisper_min_confidence", "Min Confidence",
      "Filter low-confidence segments (hallucination guard)",
      "whisper", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_no_speech_threshold", "No-Speech Threshold",
      "Reject segments where no_speech_prob is at or above this value (filters hallucinations during silence)",
      "whisper", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_min_language_probability", "Min Language Confidence",
      "Reject an utterance when Whisper is unsure which language it heard (0 disables; ignored when a language is set below)",
      "whisper", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_language", "Spoken Language",
      "ISO-639-1 code of the language you speak, e.g. de or ja. Empty identifies the language per utterance",
      "whisper", "str")

    # --- VAD ---
    f("vad_enabled", "Enable VAD",
      "Use Voice Activity Detection",
      "vad", "bool")
    f("vad_aggressiveness", "Aggressiveness",
      "VAD aggressiveness (0=least, 3=most aggressive)",
      "vad", "int", min_val=0, max_val=3)
    f("endpoint_silence_ms", "Endpoint Silence",
      "Silence duration to end an utterance",
      "vad", "int", min_val=100, max_val=5000, step=50, suffix="ms")
    f("max_utterance_ms", "Max Utterance",
      "Maximum single utterance duration",
      "vad", "int", min_val=1000, max_val=60000, step=1000, suffix="ms")
    # --- Timing & Windows ---
    f("hot_window_enabled", "Hot Window",
      "Enable follow-up window after responses",
      "timing", "bool")
    f("hot_window_seconds", "Hot Window Duration",
      "Duration of follow-up window",
      "timing", "float", min_val=1.0, max_val=30.0, step=0.5, suffix="s")
    f("transcript_buffer_duration_sec", "Transcript Buffer",
      "Duration of rolling transcript history for intent judging",
      "timing", "float", min_val=10, max_val=600, step=10, suffix="s")

    # --- Memory & Dialogue ---
    f("dialogue_memory_timeout", "Memory & Diary Window",
      "Duration for dialogue memory and forced diary updates",
      "memory", "float", min_val=30, max_val=3600, step=30, suffix="s")
    f("memory_enrichment_max_results", "Enrichment Results",
      "Max memory results for context enrichment",
      "memory", "int", min_val=1, max_val=50)
    f("memory_enrichment_source", "Enrichment Source",
      "Which memory system enriches replies: all (diary + graph), diary only, or graph only",
      "memory", "choice", choices=[("diary", "Diary only"), ("graph", "Graph only"), ("all", "All (diary + graph)")])
    f("tool_carryover_max_turns", "Tool Carryover Turns",
      "How many prior replies' tool results to keep visible for follow-up questions",
      "memory", "int", min_val=0, max_val=10)
    f("tool_carryover_per_entry_chars", "Tool Carryover Length",
      "Chars kept per carried-over tool result (UNTRUSTED fence markers preserved)",
      "memory", "int", min_val=200, max_val=8000, step=100)
    f("agentic_max_turns", "Agentic Max Turns",
      "Maximum turns in agentic tool-use loops",
      "memory", "int", min_val=1, max_val=30)

    # --- Security ---
    f("security_level", "Confirmation Level",
      "Critical protects sensitive actions; paranoid confirms every tool; off disables protection",
      "security", "choice", choices=[
          ("critical", "Critical (recommended)"),
          ("paranoid", "Paranoid"),
          ("off", "Off"),
      ])
    f("security_confirm_channels", "Confirmation Channels",
      "Channels tried in order when an action needs approval: desktop, web, telegram, voice",
      "security", "list")
    f("security_confirmation_timeout_sec", "Confirmation Timeout",
      "Maximum time to wait for a decision",
      "security", "int", min_val=5, max_val=300, suffix="s")
    f("telegram_bot_token", "Telegram Bot Token",
      "Bot token for mobile confirmations; leave empty to disable Telegram",
      "security", "password", nullable=True)
    f("telegram_chat_id", "Telegram Chat ID",
      "Only decisions from this chat are accepted; leave empty to disable Telegram",
      "security", "str", nullable=True)
    f("telegram_api_base_url", "Telegram API Host",
      "Bot API server to call; point it at a self-hosted instance to keep confirmations local",
      "security", "str")

    # --- Location ---
    f("location_enabled", "Enable Location",
      "Allow location-aware responses",
      "location", "bool")
    f("location_auto_detect", "Auto-Detect",
      "Automatically detect location from IP",
      "location", "bool")
    f("location_cache_minutes", "Cache Duration",
      "Minutes to cache location data",
      "location", "int", min_val=1, max_val=1440, step=5, suffix="min")
    f("location_ip_address", "IP Address Override",
      "Manual IP for geolocation (leave empty for auto)",
      "location", "str", nullable=True)
    f("location_cgnat_resolve_public_ip", "CGNAT Resolve",
      "Resolve public IP when behind CGNAT",
      "location", "bool")

    # --- Features ---
    f("web_search_enabled", "Web Search",
      "Enable web search tool",
      "features", "bool")
    f("brave_search_api_key", "Brave Search API Key",
      "Optional. When set, Brave is used as the primary fallback if DuckDuckGo "
      "is blocked. Free tier: 2,000 queries/month at api.search.brave.com.",
      "features", "str", nullable=True)
    f("wikipedia_fallback_enabled", "Wikipedia Fallback",
      "Use Wikipedia as a last-resort source when other search engines fail. "
      "No key, no account, privacy-light.",
      "features", "bool")
    f("low_power_mode", "Low Power Mode",
      "Reduce background LLM residency and skip LLM startup warmup",
      "features", "bool")
    f("tune_enabled", "Startup Tune",
      "Play startup sound",
      "features", "bool")
    f("dictation_enabled", "Dictation Mode",
      "Hold a hotkey to record speech, release to paste transcription into any app",
      "features", "bool")
    f("dictation_hotkey", "Dictation Hotkey",
      "Key combination to hold for dictation. Double-tap for hands-free mode.",
      "features", "choice", choices=_dictation_hotkey_choices())
    f("dictation_filler_removal", "Filler Word Removal",
      "Use the local LLM to remove filler words (um, uh, like) from dictation output",
      "features", "bool")
    f("dictation_thinking_enabled", "Dictation Thinking Mode",
      "Let the LLM think when cleaning dictation (adds latency after each dictation)",
      "features", "bool")
    f("dictation_custom_dictionary", "Custom Dictionary",
      "Correction rules for dictation. Use 'wrong -> right' format (e.g. 'Jarvice -> Jarvis')",
      "features", "list")

    # --- Control Centre ---
    f("webui_enabled", "Enable Control Centre",
      "Serve the local control centre while the daemon runs",
      "webui", "bool")
    f("webui_port", "Port",
      "TCP port the control centre listens on",
      "webui", "int", min_val=1024, max_val=65535)
    f("webui_bind_host", "Bind Address",
      "127.0.0.1 keeps it on this machine. 0.0.0.0 reaches it from your phone "
      "on the same network and then requires the access token below.",
      "webui", "str")
    f("webui_token", "Access Token",
      "Required once the bind address leaves loopback. Empty mints a fresh "
      "token at every start and prints it to the console.",
      "webui", "password", nullable=True)
    f("webui_open_browser", "Open on Start",
      "Open the control centre in your browser when the daemon starts",
      "webui", "bool")

    # --- Advanced ---
    f("echo_tolerance", "Echo Tolerance",
      "Time tolerance for echo detection",
      "advanced", "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s")

    return fields


FIELD_METADATA = _build_field_metadata()
