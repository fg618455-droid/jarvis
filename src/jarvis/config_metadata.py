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
    field_type: str  # "bool", "int", "float", "str", "choice", "device", "list", "object_list"
    choices: Optional[List[tuple[str, str]]] = None  # [(value, display), ...]
    min_val: Optional[float] = None
    max_val: Optional[float] = None
    step: Optional[float] = None
    suffix: Optional[str] = None
    nullable: bool = False  # Whether None/"" is a valid value (shows "Default" option)
    item_fields: Optional[tuple["FieldMeta", ...]] = None
    # Initial value used only when a structured-list editor adds an item.
    # It is metadata, never a value resolved from the process environment.
    default_value: Any = None


LLM_ROUTE_FIELD_METADATA = (
    FieldMeta("name", "Name", "Display name for this route", "llm_routes", "str",
              default_value="New route"),
    FieldMeta("provider", "Protocol", "Wire protocol used by the endpoint", "llm_routes", "choice",
              choices=[("openai_compatible", "OpenAI-compatible"), ("ollama", "Ollama"),
                       ("claude_subscription", "Claude subscription"),
                       ("codex_subscription", "Codex subscription"),
                       ("crew_chat", "Crew chat")],
              default_value="openai_compatible"),
    FieldMeta("base_url", "Base URL", "Endpoint base URL", "llm_routes", "str"),
    FieldMeta("api_key", "API Key", "Bearer credential for this endpoint", "llm_routes", "password",
              nullable=True, default_value=""),
    FieldMeta("api_key_env", "API Key Environment", "Environment variable containing the bearer credential", "llm_routes", "str",
              nullable=True, default_value=""),
    FieldMeta("model", "Model", "Model name exposed by the endpoint", "llm_routes", "str"),
    FieldMeta("tier", "Tier", "Route chain that uses this endpoint", "llm_routes", "choice",
              choices=[("fast", "Fast"), ("chat", "Chat")], default_value="chat"),
    FieldMeta("timeout_sec", "Timeout", "Seconds before trying the next route", "llm_routes", "float",
              min_val=0.1, max_val=600, step=0.5, suffix="s", default_value=4.0),
    FieldMeta("enabled", "Enabled", "Whether this route participates in its tier chain", "llm_routes", "bool",
              default_value=True),
    FieldMeta("capabilities", "Capabilities", "Supported request shapes: chat, stream, tools", "llm_routes", "list",
              default_value=["chat", "stream", "tools"]),
)


# Hints are display-only. Subscription and crew backends ignore their inert
# endpoint/model placeholders, but storing explicit values keeps every route
# in one stable schema and lets the factory reject malformed network routes.
LLM_ROUTE_PROVIDER_PLACEHOLDERS = {
    "ollama": {
        "base_url": "http://127.0.0.1:11434",
        "model": "qwen2.5:7b",
        "api_key_env": "",
    },
    "openai_compatible": {
        "base_url": "https://provider.example/v1",
        "model": "provider-model-id",
        "api_key_env": "PROVIDER_API_KEY",
    },
    "claude_subscription": {
        "base_url": "claude-cli",
        "model": "claude-subscription",
        "api_key_env": "",
    },
    "codex_subscription": {
        "base_url": "codex-cli",
        "model": "gpt-5.6-sol",
        "api_key_env": "",
    },
    "crew_chat": {
        "base_url": "crew-chat",
        "model": "crew-chat",
        "api_key_env": "",
    },
}


CLOUD_TTS_PROVIDER_FIELD_METADATA = (
    FieldMeta("name", "Name", "Display name for this provider", "tts_cloud_providers", "str",
              default_value="Cloud provider"),
    FieldMeta("provider", "Provider", "Vendor client identifier", "tts_cloud_providers", "choice",
              choices=[("fish_audio", "Fish Audio"), ("elevenlabs", "ElevenLabs")],
              default_value="fish_audio"),
    FieldMeta("api_key_env", "API Key Environment", "Environment variable containing the credential", "tts_cloud_providers", "str",
              default_value=""),
    FieldMeta("voice_id", "Voice ID", "Opaque provider voice identifier", "tts_cloud_providers", "str",
              default_value=""),
    FieldMeta("model", "Model", "Provider model name", "tts_cloud_providers", "str",
              default_value=""),
    FieldMeta("enabled", "Enabled", "Whether this provider participates in the chain", "tts_cloud_providers", "bool",
              default_value=True),
    FieldMeta("timeout_sec", "Timeout", "Seconds before trying the next provider", "tts_cloud_providers", "float",
              min_val=0.1, max_val=600, step=0.5, suffix="s", default_value=10.0),
)


# Categories and their display order
CATEGORIES = [
    ("llm", "🤖 LLM & AI Models"),
    ("tts", "🔊 Text-to-Speech"),
    ("piper", "🎵 Piper TTS"),
    ("chatterbox", "🎭 Chatterbox TTS"),
    ("kokoro", "🎤 Kokoro TTS"),
    ("speech_input", "🎤 Speech Input"),
    ("timing", "⏱️ Timing & Windows"),
    ("memory", "🧠 Memory & Dialogue"),
    ("school", "🎓 School"),
    ("passive", "📝 Passive Capture"),
    ("security", "🔐 Security"),
    ("location", "📍 Location"),
    ("features", "✨ Features"),
    ("webui", "🖥️ Control Centre"),
    ("crew", "👥 Mission Control"),
    ("mcps", "🔌 MCP Servers"),
    ("advanced", "🔧 Advanced"),
]


CATEGORY_DETAILS = {
    "llm": {
        "description": (
            "The ordered route chain decides FAST and CHAT requests first. "
            "The single-endpoint and Ollama fields below remain the local and "
            "legacy fallback configuration."
        ),
        "action_label": "Open LLM routes",
        "action_href": "#/llm-routes",
    },
}


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


def choices_for(meta: FieldMeta, value: Any) -> List[tuple[str, str]]:
    """The choices to offer for a field, given what is configured.

    Curated lists such as the supported chat models are a shortlist, not the
    set of values a local runtime can serve: a tag built from a custom
    Modelfile, or a model pulled after this release, will never appear in
    one. A select that cannot show the configured value displays a different
    one instead, which misreports the running configuration and, in an
    interface that reads every widget back on save, replaces it.

    An unlisted value is therefore offered under its own name, at the top,
    so the form can always show the truth.
    """
    choices = list(meta.choices or [])
    # A field that offers nothing is not a select, and its value is not a
    # choice: echoing it here would put values such as a credential into a
    # payload that is otherwise careful never to carry one.
    if not choices or value in (None, ""):
        return choices
    text = str(value)
    if any(str(known) == text for known, _ in choices):
        return choices
    return [(text, text)] + choices


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


def _crew_chat_agent_choices() -> list:
    """Which crew specialist can answer a crew_chat turn, from the same
    fixed roster ``askCrew`` delegates to (``AGENT_THREADS``). Read-only
    import: this only offers the existing names as dropdown choices, it
    does not change askCrew's own fire-and-forget behaviour."""
    from jarvis.tools.builtin.ask_crew import AGENT_THREADS
    return [("", "Not set")] + [(name, name) for name in sorted(AGENT_THREADS.keys())]


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
      "llm", "choice",
      choices=[("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("llm_base_url", "Base URL",
      "Provider API base URL (e.g. http://localhost:1234/v1 for LM Studio). "
      "Leave empty to use the Ollama URL.",
      "llm", "str", nullable=True)
    f("llm_api_key", "API Key",
      "Bearer token for the provider, if it requires one. Leave empty for none.",
      "llm", "password", nullable=True)
    f("llm_chat_model", "Chat Model",
      "Model name the provider exposes. Leave empty to use the Ollama chat model.",
      "llm", "str", nullable=True)
    f("chat_backend_override", "Chat Backend Override",
      "Backend to try first for main CHAT replies. An unavailable choice falls "
      "through to the configured route chain.",
      "llm", "choice",
      choices=[("auto", "Automatic"),
               ("ollama", "Ollama"),
               ("claude_subscription", "Claude subscription"),
               ("codex_subscription", "Codex subscription"),
               ("crew_chat", "Crew chat (Hermes)")])
    f("embedding_provider", "Embedding Provider",
      "Runtime for embeddings. Leave on 'Same as chat provider' unless your "
      "chat runtime has no embeddings endpoint (then route them to Ollama).",
      "llm", "choice",
      choices=[("", "Same as chat provider"),
               ("ollama", "Ollama (local)"),
               ("openai_compatible", "OpenAI-compatible server")])
    f("embedding_base_url", "Embedding Base URL",
      "Override base URL for embeddings. Leave empty to inherit from the "
      "chat provider (or the Ollama URL).",
      "llm", "str", nullable=True)
    f("embedding_api_key", "Embedding API Key",
      "Override bearer token for embeddings. Leave empty to inherit the chat key.",
      "llm", "password", nullable=True)
    f("embedding_model", "Embedding Model",
      "Embedding model name. Leave empty to use the Ollama embedding model.",
      "llm", "str", nullable=True)

    # --- Text-to-Speech ---
    f("tts_enabled", "Enable TTS", "Enable text-to-speech output",
      "tts", "bool")
    f("tts_engine", "TTS Engine", "Speech synthesis engine",
      "tts", "choice", choices=[("piper", "Piper (Neural)"), ("chatterbox", "Chatterbox (Voice Cloning)"),
                                 ("kokoro", "Kokoro (Neural)"),
                                 ("cloud", "Cloud chain (opt-in)")])
    f("tts_local_fallback_engine", "Local Fallback",
      "Local engine used after every cloud provider fails",
      "tts", "choice", choices=[("piper", "Piper (Neural)"),
                                  ("chatterbox", "Chatterbox (Voice Cloning)"),
                                  ("kokoro", "Kokoro (Neural)")])
    f("tts_cloud_providers", "Cloud Provider Chain",
      "Ordered providers used by the cloud engine. Credentials stay in the named environment variables.",
      "tts", "object_list", item_fields=CLOUD_TTS_PROVIDER_FIELD_METADATA)
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

    # --- Kokoro TTS ---
    f("tts_kokoro_voice", "Voice",
      "Kokoro voice name (its first letter selects the language, e.g. 'a' American English, 'b' British English)",
      "kokoro", "str")
    f("tts_kokoro_speed", "Speed",
      "Speaking rate multiplier: <1.0 slower, >1.0 faster",
      "kokoro", "float", min_val=0.5, max_val=2.0, step=0.05)

    # --- Voice Input ---
    f("tts_output_device", "Output Device",
      "Speaker device for Jarvis's voice (name or index). Leave empty for system default.",
      "tts", "device")
    f("voice_device", "Input Device",
      "Microphone device (name or index). Leave empty for system default.",
      "speech_input", "device")
    f("sample_rate", "Sample Rate",
      "Audio sample rate in Hz",
      "speech_input", "choice",
      choices=[("16000", "16000 Hz"), ("44100", "44100 Hz"), ("48000", "48000 Hz")])
    f("voice_min_energy", "Min Energy",
      "Minimum audio energy to register voice",
      "speech_input", "float", min_val=0.0, max_val=1.0, step=0.005)

    # --- Wake Word ---
    f("wake_word", "Wake Word",
      "Primary wake word to activate Jarvis",
      "speech_input", "str")
    f("wake_fuzzy_ratio", "Fuzzy Match Ratio",
      "How loosely to match the wake word (0.0–1.0)",
      "speech_input", "float", min_val=0.5, max_val=1.0, step=0.01)
    # --- Whisper ---
    f("whisper_model", "Model Size",
      "Whisper model size (tiny/base/small/medium/large)",
      "speech_input", "choice",
      choices=[("tiny", "Tiny"), ("base", "Base"), ("small", "Small"),
               ("medium", "Medium"), ("large-v3", "Large v3")])
    f("whisper_backend", "Backend",
      "Speech recognition backend",
      "speech_input", "choice",
      choices=[("auto", "Auto"), ("mlx", "MLX (Apple Silicon)"),
               ("faster-whisper", "Faster Whisper")])
    f("whisper_device", "Compute Device",
      "Device for Whisper inference",
      "speech_input", "choice",
      choices=[("auto", "Auto"), ("cuda", "CUDA (GPU)"), ("cpu", "CPU")])
    f("whisper_compute_type", "Compute Type",
      "Quantisation level for inference",
      "speech_input", "choice",
      choices=[("int8", "INT8 (Fast)"), ("float16", "Float16"), ("float32", "Float32")])
    f("whisper_vad", "Use VAD Filter",
      "Filter audio with VAD before transcription",
      "speech_input", "bool")
    f("whisper_min_confidence", "Min Confidence",
      "Filter low-confidence segments (hallucination guard)",
      "speech_input", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_no_speech_threshold", "No-Speech Threshold",
      "Reject segments where no_speech_prob is at or above this value (filters hallucinations during silence)",
      "speech_input", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_min_language_probability", "Min Language Confidence",
      "Reject an utterance when Whisper is unsure which language it heard (0 disables; ignored when a language is set below)",
      "speech_input", "float", min_val=0.0, max_val=1.0, step=0.05)
    f("whisper_language", "Spoken Language",
      "ISO-639-1 code of the language you speak, e.g. de or ja. Empty identifies the language per utterance",
      "speech_input", "str")

    # --- VAD ---
    f("vad_enabled", "Enable VAD",
      "Use Voice Activity Detection",
      "speech_input", "bool")
    f("vad_aggressiveness", "Aggressiveness",
      "VAD aggressiveness (0=least, 3=most aggressive)",
      "speech_input", "int", min_val=0, max_val=3)
    f("endpoint_silence_ms", "Endpoint Silence",
      "Silence duration to end an utterance",
      "speech_input", "int", min_val=100, max_val=5000, step=50, suffix="ms")
    f("max_utterance_ms", "Max Utterance",
      "Maximum single utterance duration",
      "speech_input", "int", min_val=1000, max_val=60000, step=1000, suffix="ms")
    # --- Timing & Windows ---
    f("hot_window_enabled", "Hot Window",
      "Enable wake-word-free follow-up after responses",
      "timing", "bool")
    f("hot_window_seconds", "Hot Window Duration",
      "Duration of follow-up window",
      "timing", "float", min_val=1.0, max_val=30.0, step=0.5, suffix="s")
    f("wake_command_timeout_seconds", "Wake Request Timeout",
      "Time to wait for one request after a standalone wake word",
      "timing", "float", min_val=1.0, max_val=60.0, step=0.5, suffix="s")
    f("wake_acknowledgement", "Wake Acknowledgement",
      "Spoken acknowledgement after a standalone wake word",
      "timing", "str")
    f("conversation_mode_acknowledgement", "Conversation Acknowledgement",
      "Spoken acknowledgement when continuous conversation starts",
      "timing", "str")
    f("transcript_buffer_duration_sec", "Transcript Buffer",
      "Duration of rolling transcript history for intent judging",
      "timing", "float", min_val=10, max_val=600, step=10, suffix="s")
    f("simple_reply_first_audio_sec", "Reply First Audio Budget",
      "Shared deadline for a reply that does not require long-term memory",
      "timing", "float", min_val=0.5, max_val=60, step=0.5, suffix="s")
    f("memory_reply_first_audio_sec", "Memory Reply First Audio Budget",
      "Shared deadline after the planner requests long-term memory",
      "timing", "float", min_val=1, max_val=120, step=0.5, suffix="s")
    f("memory_lookup_acknowledgement", "Memory Lookup Acknowledgement",
      "Optional phrase spoken before long-term memory retrieval; empty stays silent",
      "timing", "str")

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
    f("remio_memory_enabled", "Remio Memory",
      "Search the local Remio knowledge base during planner-directed memory retrieval",
      "memory", "bool")
    f("obsidian_vault_path", "Obsidian Vault Path",
      "Absolute path to your Obsidian vault. Empty disables both reading and writing to it",
      "memory", "str", nullable=True)
    f("obsidian_memory_folder", "Obsidian Memory Folder",
      "Vault-relative folder Jarvis mirrors its knowledge graph into; the only path it may write to",
      "memory", "str")
    f("obsidian_write_mode", "Obsidian Write Mode",
      "off never writes, dry_run computes and logs the plan without writing, on applies it",
      "memory", "choice", choices=[("off", "Off"), ("dry_run", "Dry run (plan only)"), ("on", "On")])
    f("obsidian_read_enabled", "Obsidian Reading",
      "Index the vault as a third memory-enrichment source alongside diary and graph",
      "memory", "bool")
    f("obsidian_read_max_results", "Obsidian Results",
      "Vault snippets injected per enrichment pass",
      "memory", "int", min_val=1, max_val=20)
    f("obsidian_index_max_file_kb", "Obsidian File Size Limit",
      "Files larger than this are skipped by the vault indexer",
      "memory", "int", min_val=16, max_val=8192, step=16, suffix="KB")
    f("tool_carryover_max_turns", "Tool Carryover Turns",
      "How many prior replies' tool results to keep visible for follow-up questions",
      "memory", "int", min_val=0, max_val=10)
    f("tool_carryover_per_entry_chars", "Tool Carryover Length",
      "Chars kept per carried-over tool result (UNTRUSTED fence markers preserved)",
      "memory", "int", min_val=200, max_val=8000, step=100)
    f("agentic_max_turns", "Agentic Max Turns",
      "Maximum turns in agentic tool-use loops",
      "memory", "int", min_val=1, max_val=30)

    # --- School ---
    f("morning_briefing_enabled", "Morning Briefing",
      "Speak one short School memory summary after the configured time each day. Off by default",
      "school", "bool")
    f("morning_briefing_time", "Briefing Time",
      "Local time after which today's school briefing may be spoken, in 24-hour HH:MM format",
      "school", "str")

    # --- Passive Capture ---
    f("passive_capture_enabled", "Enable Passive Capture",
      "Keep a text-only record of speech already transcribed near the microphone. Off by default",
      "passive", "bool")
    f("passive_capture_retention_days", "Retention",
      "Days to keep passive transcript lines. Zero keeps them until manual deletion",
      "passive", "int", min_val=0, max_val=3650, suffix="days")
    f("passive_capture_min_words", "Minimum Words",
      "Utterances shorter than this are not written to the passive record",
      "passive", "int", min_val=0, max_val=100)
    f("passive_digest_interval_min", "Digest Interval",
      "Minutes between passes that fold useful overheard speech into memory",
      "passive", "float", min_val=0.01, max_val=1440, step=1, suffix="min")
    f("passive_digest_max_lines", "Lines per Digest",
      "Maximum passive transcript lines sent to one ambient digest pass",
      "passive", "int", min_val=1, max_val=1000)

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
    f("telegram_chat_enabled", "Telegram Conversation",
      "Let the configured chat talk to Jarvis, not just approve actions; a message runs tools on this machine",
      "security", "bool")

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
    f("location_manual_city", "Manual City Override",
      "Set your real city to skip IP geolocation (fixes ISPs that register your address under the wrong town)",
      "location", "str", nullable=True)
    f("location_manual_region", "Manual Region Override",
      "Optional state/region shown alongside the manual city",
      "location", "str", nullable=True)
    f("location_manual_country", "Manual Country Override",
      "Set your real country to skip IP geolocation",
      "location", "str", nullable=True)
    f("location_manual_timezone", "Manual Timezone Override",
      "IANA timezone (e.g. Asia/Bangkok) to use alongside the manual location",
      "location", "str", nullable=True)

    # --- Features ---
    f("web_search_enabled", "Web Search",
      "Enable web search tool",
      "features", "bool")
    f("brave_search_api_key", "Brave Search API Key",
      "Optional. When set, Brave is used as the primary fallback if DuckDuckGo "
      "is blocked. Free tier: 2,000 queries/month at api.search.brave.com.",
      "features", "password", nullable=True)
    f("wikipedia_fallback_enabled", "Wikipedia Fallback",
      "Use Wikipedia as a last-resort source when other search engines fail. "
      "No key, no account, privacy-light.",
      "features", "bool")
    f("low_power_mode", "Low Power Mode",
      "Reduce background LLM residency and skip LLM startup warmup",
      "features", "bool")
    f("computer_interaction_enabled", "Computer Interaction",
      "Opt in to bounded semantic browser and Windows application control. "
      "Consequential actions still require individual confirmation",
      "features", "bool")
    f("system_management_enabled", "System Management",
      "Opt in to structured package, broader file and named Windows settings management. "
      "Mutating actions still require individual confirmation",
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

    # --- Mission Control ---
    f("crew_api_url", "Crew API URL",
      "Base URL of the NAS-hosted crew activity endpoint, e.g. "
      "http://192.168.178.113:8643. Empty hides the Mission Control view",
      "crew", "str", nullable=True)
    f("crew_api_key", "Crew API Key",
      "Shared key the NAS endpoint expects in its X-Crew-Key header",
      "crew", "password", nullable=True)
    f("crew_agents", "Crew Roster",
      "The agents Mission Control shows, one per line, in display order. An "
      "agent with nothing in the activity log is shown as quiet rather than "
      "hidden. Agents that log work without being listed are still shown",
      "crew", "list")
    f("crew_telegram_chat_id", "Crew Telegram Chat ID",
      "Chat ID of the crew's Telegram group, used by askCrew to delegate a "
      "task. Sent with the bot configured under Security → Telegram, which "
      "must also be a member of that group. Empty disables askCrew",
      "crew", "str", nullable=True)
    f("crew_handoff_enabled", "Automatic Crew Handoff",
      "Hand a slow local reply to the crew on its own, once it has run past "
      "the deadline, instead of only when askCrew is explicitly requested. "
      "Still asks for confirmation like any askCrew call, and that wait is "
      "not yet bounded to the deadline, so a delegation with nobody free to "
      "confirm can sit at the full confirmation timeout before it gives up",
      "crew", "bool")
    f("crew_chat_agent", "Crew Chat Agent",
      "Which crew specialist answers a turn routed to the crew_chat backend "
      "(the \"crew_chat\" route provider and chat_backend_override value). "
      "Empty leaves that route unable to answer rather than guessing an "
      "agent",
      "crew", "choice", choices=_crew_chat_agent_choices())

    # --- Advanced ---
    f("echo_tolerance", "Echo Tolerance",
      "Time tolerance for echo detection",
      "advanced", "float", min_val=0.0, max_val=2.0, step=0.05, suffix="s")

    return fields


FIELD_METADATA = _build_field_metadata()
