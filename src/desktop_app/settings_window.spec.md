# Settings Window Specification

Auto-generated settings UI that dynamically builds its interface from config field metadata.

## Overview

The Settings Window provides a graphical interface for editing `config.json` without requiring users to manually edit JSON. It reads the current config, presents categorised fields with appropriate input widgets, and saves changes back.

## Design Principles

1. **Metadata-driven**: All fields are defined in the `FIELD_METADATA` registry in `src/jarvis/config_metadata.py`, shared with the control centre's settings view. Adding a new config parameter to both settings interfaces requires only adding a `FieldMeta` entry — no widget code changes.
2. **Minimal config files**: Only non-default values are written to `config.json`. Removing a field from the config reverts it to the default.
3. **Preserves unknown keys**: Keys not managed by the UI (e.g. `mcps`, `_config_version`, future additions) are preserved when saving.
4. **Theme-consistent**: Uses the shared Jarvis theme from `themes.py`.

## Architecture

```
FieldMeta (dataclass, src/jarvis/config_metadata.py)
  ├── key: str           # config.json key name
  ├── label: str         # Human-readable label
  ├── description: str   # Tooltip text
  ├── category: str      # Tab grouping key
  ├── field_type: str    # "bool" | "int" | "float" | "str" | "choice" | "device" | "list" | "object_list"
  ├── choices            # For "choice"/"device": [(value, display), ...]
  ├── min_val / max_val  # Numeric bounds
  ├── step               # Increment step
  ├── suffix             # Unit label (e.g. "s", "ms", "WPM")
  ├── nullable           # Whether None is valid (shows placeholder)
  ├── item_fields        # Nested FieldMeta tuple for "object_list"
  ├── default_value      # Safe initial value when a structured item is added
  └── section            # Optional heading inside a category
```

## Widget Mapping

| field_type | Widget | Notes |
|-----------|--------|-------|
| `bool` | QCheckBox | |
| `int` | QSpinBox | With bounds, step, suffix |
| `int` (nullable) | QCheckBox + QSpinBox | Checkbox enables/disables the spinbox |
| `float` | QDoubleSpinBox | With bounds, step, suffix |
| `str` | QLineEdit | Placeholder if nullable |
| `password` | QLineEdit (EchoMode.Password) | Masked input for API keys; same value extraction as `str` |
| `choice` | QComboBox | Pre-defined options, plus the configured value |
| `device` | QComboBox | Dynamically populated from sounddevice |
| `list` | QListWidget + Add/Edit/Remove buttons | Stores as JSON array in config |
| `object_list` | QTableWidget + Add/Remove/Move buttons | One typed column per nested metadata field; stores a JSON object array |

`choices_for(meta, value)` in `jarvis.config_metadata` decides what a
`choice` field offers, and both this window and the control centre build
their selects from it. A configured value that is not on the curated list is
offered under its own name, at the top. Lists such as the supported chat
models are a shortlist rather than the set of values a local runtime can
serve, and a select that cannot show what is configured reports a different
value than the one in the file. This window reads every widget back on save,
so that misreport would also overwrite the real one. Fields with no choices
at all are left alone: their value is not a choice, and echoing it would put
a credential into a form that is otherwise careful never to show one.

## Layout

The settings window uses a sidebar navigation pattern: a fixed-width `QListWidget` on the left lists categories, and a `QStackedWidget` on the right shows the selected category's form. This avoids horizontal overflow from too many tabs.

## Categories (Sidebar Order)

The order is `CATEGORIES` in `jarvis.config_metadata`, and both settings
surfaces walk it unchanged: it runs from what answers a question, through what
carries it, to what the assistant knows and the machine around it.

1. Providers
2. Speech Input
3. Speech Recognition
4. Speech Output
5. Memory & Dialogue
6. School
7. Passive Capture
8. Security
9. Channels
10. Mission Control
11. MCP Servers
12. Features
13. Location
14. Timing & Windows
15. Control Centre

A category with no fields and no page of its own is skipped rather than shown
empty. Nothing is filed under a category named for how difficult it is: that
says how hard a setting was thought to be rather than what it is about, and the
reader looking for the Ollama URL has no way to know it was considered hard.

### Providers

**Timeouts** and **Thinking and behaviour** are what is true of a reply
whichever route produced it. **Local Ollama** is the local endpoint:
`ollama_chat_model` is the PRIVATE model, which writes memory and tidies
dictation, `ollama_embed_model` handles local embeddings, and
`ollama_base_url` is the server both reach. Neither answers a conversation:
FAST and CHAT are served by configured routes alone, so no local model is
offered here as a reply model.

Effective FAST/CHAT providers, endpoint credentials, route models,
`chat_backend_override`, and `crew_chat_agent` are not duplicated here. The
control centre's route editor is the one place they are edited. Legacy
single-endpoint keys remain supported by config loading and are preserved when
already present, but a general-settings save cannot silently reconstruct or
overwrite them.

### Security and Channels

Security is the gate: which level is in force, whether an approval is
remembered, which channels may ask, and how long a request waits. Channels is
what each of those ways of reaching Jarvis needs in order to work, which today
is the **Telegram** bot token, chat, host, and whether that chat may talk to
Jarvis rather than only approve actions. The gate uses Telegram; that does not
make Telegram part of the confirmation policy, and filing its credentials
under Security hides them from anyone setting up a chat.

### Passive Capture

Whether the record is running is not a field here. That switch names the model
backend the room's text will reach and asks before it starts; a checkbox beside
it would be the same switch with the question taken out, and whichever of the
two was used last is the one the other misreports. It lives on the passive
record itself in the control centre. What this category holds is how the record
behaves once it is on.

### Speech pipeline

Speech Input contains **Microphone**, **Wake word**, and **Voice activity and
endpointing** sections. Speech Recognition owns the labelled **Whisper**
section. Speech Output combines **Common output**, **Cloud chain**, **Piper**,
**Chatterbox**, and **Kokoro**, rather than scattering one output pipeline over
five sidebar entries.

The Cloud chain section exposes `tts_cloud_providers` as a structured ordered table. Each row edits the provider
name, vendor id, credential environment-variable name, voice id, model,
enabled state, and timeout. Add, remove, enable/disable, and move controls are
available without editing JSON. The vendor is selected from the supported
Fish Audio and ElevenLabs clients while an older unrecognised configured value
remains visible. Credential values are never resolved from the environment and
never enter either settings form or `config.json`.

### Features

The Features category exposes user-facing runtime toggles that do not need a
dedicated page, in four sections: **The web** (search and its fallbacks),
**This computer** (the two opt-ins for acting on the machine), **Dictation**,
and **This machine** (`low_power_mode`, `tune_enabled`).

`low_power_mode` is a boolean toggle. When enabled, the voice listener skips
LLM startup warmup and the Ollama keep-alive windows used by warmup and the
intent judge are short. The setting is saved only when it differs from the
default, like every other metadata-managed field.

## Hardware Device Selection

The Speech Input category includes a microphone dropdown populated at window open time via `sounddevice.query_devices()`. Speech Output contains the matching output-device dropdown. Each lists the relevant devices with their index and name. The stored value is the device index as a string, or empty string for system default.

## Save Behaviour

- Only keys that differ from `get_default_config()` are written.
- Existing keys not managed by the UI are preserved (e.g. `mcps`, `active_profiles`, `wake_aliases`, `allowlist_bundles`).
- After save, a dialog confirms success and reminds the user to restart.
- If the daemon is running when save completes, the tray app offers to restart it.

## Reset to Defaults

- Prompts for confirmation.
- Resets all widget values to `get_default_config()` values.
- Does NOT immediately save — user must still click Save.

## Integration

- Accessed via "⚙️ Settings" in the system tray menu.
- Opens as a modal QDialog.
- Lazy-imported to avoid loading sounddevice at startup.

## MCP Servers Section

The MCP Servers category is **not** metadata-driven — it uses a custom page because `mcps` is a complex dict structure.

### Layout

- Description label explaining what MCP servers are
- List widget showing configured servers (display name from catalogue if recognised, otherwise `🔌 {name}`)
- Buttons: **Add from Catalogue**, **Add Custom**, **Edit**, **Remove**
- Detail panel showing the selected server's name, command, args, and env vars

### Add from Catalogue

Opens `_MCPCatalogueDialog` showing all entries from `mcp_catalogue.CATALOGUE`. Already-configured servers appear checked and disabled. Servers that require an API key show a 🔑 badge. When the user confirms, they're prompted for any needed API keys.

### Add Custom

Opens `_MCPEditDialog` with fields for name, command, args (space-separated), and env vars (KEY=VALUE pairs). Validates that name and command are non-empty.

### Edit

Opens `_MCPEditDialog` pre-filled with the selected server's config. Name is read-only during edit.

### Remove

Prompts for confirmation, then removes the server from the in-memory dict.

### Save Behaviour

On save, the `mcps` dict is written to config.json if non-empty, or removed entirely if empty. On reset, all MCPs are cleared.

## Fields NOT Exposed in UI

These fields are managed elsewhere or are too complex for a simple form:

- `llm_routes`, `chat_backend_override`, `crew_chat_agent`, and legacy
  provider/embedding connection keys — authoritative LLM Routes view
- `db_path` / `sqlite_vss_path` — internal storage paths
- `active_profiles` — list managed by setup wizard
- `allowlist_bundles` — list of bundle IDs
- `wake_aliases` — list of strings (complex editing)
- `use_stdin` — developer/CLI flag
- `voice_debug` — environment variable only
- `whisper_min_audio_duration` / `whisper_min_word_length` — rarely changed advanced params
- `vad_frame_ms` / `vad_pre_roll_ms` — low-level VAD timing
