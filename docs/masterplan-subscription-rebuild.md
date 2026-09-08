# Masterplan: Umbau von JARVIS auf Subscription-/Cloud-Agenten

Status: **Planungsdokument (read-only erstellt)**. Es wurde keine Produktdatei verändert.
Basis-Commit: `d22ed8b` (Branch `fg618455-droid/sandperch`), Workspace `C:\Users\User\orca\workspaces\JARVIS\sandperch`.
Erstellt: 2026-09-08.

---

## 1. Executive Summary

### 1.1 Die eine Architekturentscheidung

**JARVIS hört auf, ein Agent zu sein, und wird ein Agenten-Client.**

Der komplette Agenten-Loop — Planung, Tool-Auswahl, Tool-Ausführung, Nachfassen, Synthese — wandert
aus `src/jarvis/reply/` heraus in Claude Code, Codex und Hermes. JARVIS behält und baut aus:

| bleibt bei JARVIS | wandert zum Provider |
|---|---|
| Voice-I/O (Mikro, VAD, Wake-Word, STT-Fallback, TTS) | Reply-Loop (`engine.py`) |
| Tool-**Bereitstellung** als lokaler MCP-Server | Tool-**Auswahl** und -Aufruf |
| Memory-Store + Memory-Tools | Planung, Zerlegung, Synthese |
| Security-/Capability-Policy, Approvals, Audit | Reasoning jeder Art |
| Operator-UI (Sessions, Runs, Usage, Specs) | — |
| Cron-Verwaltung (delegiert an Hermes) | Cron-Ausführung (Hermes) |

Der Grund ist nicht Geschmack, sondern ein verifizierter technischer Befund: **keiner der drei
erlaubten Zugänge stellt einen rohen `chat(messages, tools) → tool_calls`-Endpunkt bereit.**
Claude Code und Codex sind agentische Harnesses mit eigenem Tool-Loop, eigener Permission-Engine
und eigenem Session-Store. Der einzige saubere, dokumentierte Weg, ihnen JARVIS-Tools zu geben,
ist **MCP** (`claude --mcp-config`, `codex mcp`, `hermes mcp`). Wer versucht, diese CLIs in das
bestehende `LLMBackend`-Interface (`direct/streaming/chat/embed/list_models/warm_up`) zu pressen,
baut eine Fassade, die bei jedem Tool-Call, jeder Approval-Rückfrage und jeder Quota-Grenze bricht.

### 1.2 Was das konkret auflöst

Von den **16 LLM-Kontexten** in `docs/llm_contexts.md` verschwinden **10 vollständig** — sie
existieren ausschließlich, um kleine lokale Modelle arbeitsfähig zu machen (Planner, Step-Resolver,
Tool-Router-LLM, Intent-Judge-LLM, drei Digest-Pässe, Enrichment-Extractor, Capability-Probe,
Warmup). Das ist kein Verlust, sondern der eigentliche Gewinn des Umbaus: rund **8.000 Zeilen
Kompensationslogik** für schwache Modelle werden gegenstandslos.

### 1.3 Die fünf Dinge, die weh tun

1. **Embeddings sind weg.** Kein erlaubter Zugang liefert einen Embedding-Endpunkt (verifiziert,
   §7.5). Semantische Memory-Suche wird durch FTS5/BM25 + Metadatenfilter ersetzt. Das ist eine
   echte Qualitätsregression und muss so kommuniziert werden.
2. **Der Intent-Judge verliert sein LLM.** Ein Cloud-Roundtrip pro Sprachsegment ist weder
   latenz- noch quotentauglich. Wake-Word + VAD + Fuzzy + Echo-Erkennung bleiben — der heutige
   Fallback wird zum Normalbetrieb.
3. **Latenz steigt.** Heute ~4,5 s p50 auf lokalem `gemma4:e2b`. Ein `claude -p`-Turn mit
   Prozessstart, Kontextaufbau und Tool-Runde liegt realistisch bei 6–20 s. Für Sprachdialog ist
   das grenzwertig. Gegenmaßnahme: persistente Sessions (`--input-format stream-json`,
   Codex `app-server`) statt Prozess-pro-Turn, plus sofortige Quittungs-TTS.
4. **Claude bietet keine Modell-Liste.** Verifiziert: die CLI hat keinen Listing-Befehl. Das ist
   eine **offene Capability** mit Fail-Closed-Entwurf (§7.4).
5. **Das Produktversprechen kippt.** README behauptet heute wörtlich „100% local processing.
   No cloud, no subscriptions." Das wird in sein Gegenteil verkehrt. Das ist eine Positionierungs-
   und Lizenzfrage, keine Codefrage (§26).

---

## 2. Verifizierter Ist-Zustand

### 2.1 Repository

| Kennzahl | Wert |
|---|---|
| Getrackte Dateien | 317 |
| `src/**/*.py` | 42.693 LOC |
| `src/jarvis` | 87 Dateien |
| `src/desktop_app` | 38 Dateien |
| Testdateien `tests/` | ~110 |
| Eval-Dateien `evals/` | 29 |
| Spec-Dateien `*.spec.md` | 14 |
| Dateien mit Ollama-Bezug | **104** |

Größte Module (Zeilen): `setup_wizard.py` 3.955 · `memory_viewer.py` 3.815 · `app.py` 2.915 ·
`reply/engine.py` 2.548 · `listening/listener.py` 2.536 · `memory/conversation.py` 1.793 ·
`settings_window.py` 1.264 · `memory/graph_ops.py` 1.208 · `dictation_engine.py` 1.173.

### 2.2 LLM-Schicht

`src/jarvis/llm/` implementiert eine saubere Provider-Abstraktion (`llm.spec.md`):

- `backend.py:77` — `LLMBackend` ABC mit `direct / streaming / chat / embed / list_models / warm_up`.
- `ollama.py` (391 Z.), `openai_compatible.py` (498 Z.) — zwei HTTP-Backends.
- `factory.py:66` — `get_llm_backend(cfg)` / `get_embedding_backend(cfg)`, Dispatch auf `cfg.llm_provider`.
- `tiers.py` — `Tier.FAST` / `Tier.CHAT`, `resolve_model(cfg, tier)`.

**Befund:** Die Abstraktion ist gut gebaut, aber ihr Vertrag ist HTTP-Chat-Completions-förmig.
Sie ist der richtige Ort für einen Schnitt — nicht für eine Erweiterung.

### 2.3 Hardcodierte Modell-Listen

`src/jarvis/config.py:17-42` — `SUPPORTED_CHAT_MODELS` mit vier festverdrahteten Ollama-Pull-Namen
(`gemma4:e2b`, `gemma4:e4b`, `gpt-oss:20b`, `qwen3.5:0.8b`) inkl. Größe und VRAM-Bedarf.
`config.py:45` `DEFAULT_CHAT_MODEL`, `config.py:50` `DEFAULT_FAST_MODEL`.
Zusätzlich `listener.py:281-292` — MLX-Whisper-Modellmap; `setup_wizard.py` Whisper-Größenliste.

### 2.4 Speicher / Datenbank

Eine SQLite-Datei (`cfg.db_path`, Default `~/.local/share/jarvis/jarvis.db`) trägt **zwei
unabhängige Schemata**:

- `memory/db.py:11-63` — `meals`, `conversation_summaries`, `summaries_fts` (FTS5, Porter-Tokenizer)
  plus Trigger. Optional `embeddings`/`summary_vec` via sqlite-vss (`db.py:65-75`, 768 Dim).
- `memory/graph.py:216-236` — `memory_nodes` (Baum, `parent_id`, Access-Counting, Token-Count).

**Verstoß gegen „nur eine Komponente schreibt":** `GraphMemoryStore(...)` wird an **fünf** Stellen
konstruiert — `daemon.py:425`, `reply/engine.py:1127`, `reply/engine.py:1257`,
`memory/conversation.py:1724`, `desktop_app/memory_viewer.py:315`. Der Memory-Viewer läuft im
Entwicklungsmodus als **eigener Prozess** (Flask, `memory_viewer.py:3795-3810`, `127.0.0.1:5050`).
Damit schreiben heute zwei Prozesse auf dieselbe Datei.

### 2.5 Vektor-Speicher

`utils/vector_store.py` (142 Z.) + `utils/fast_vector_store.py` (238 Z., FAISS) + optional
sqlite-vss. Embedding-Aufrufstellen: `memory/conversation.py:42`, `tools/selection.py:172`,
`tools/selection.py:194`, `listening/listener.py:1619-1628`, `reply/engine.py:925`,
`tools/builtin/tool_search.py:91`. Dimension überall 768 (`nomic-embed-text`).

### 2.6 Voice

- **STT:** `listener.py:253-264` — MLX-Whisper auf Apple Silicon, sonst `faster-whisper`
  (`WhisperModel`). Konfig: `whisper_model` (Default `medium`), `whisper_backend`, `whisper_device`,
  `whisper_compute_type`. CUDA-Beschleunigung über den Installer (`installer/windows/install_cuda.ps1`,
  cuBLAS + cuDNN, ~1,1 GB, SHA-verifiziert, Marker `.cuda_installed`).
- **TTS:** `output/tts.py` (1.025 Z.) — `PiperTTS` (Default, Auto-Download von
  `huggingface.co/rhasspy/piper-voices`) und `ChatterboxTTS` (`chatterbox-tts==0.1.2`, CUDA,
  Voice-Cloning über `tts_chatterbox_audio_prompt`).
- **Deterministisch:** `listening/wake_detection.py`, `echo_detection.py` (567 Z.),
  `transcript_buffer.py`, `state_manager.py` (503 Z.), `utils/audio_lock.py`, WebRTC-VAD.
- **Dictation:** `dictation/dictation_engine.py` (1.173 Z.) — teilt sich das Whisper-Modell,
  pausiert den Listener, fügt über Zwischenablage ein.

### 2.7 Tools

`tools/registry.py:29-41` — elf Builtins: `screenshot`, `webSearch`, `localFiles`, `fetchWebPage`,
`logMeal`, `fetchMeals`, `deleteMeal`, `refreshMCPTools`, `getWeather`, `stop`, `toolSearchTool`.
MCP: `tools/external/mcp_client.py` (344 Z.) + `mcp_runtime.py` (557 Z., persistente Stdio-Sessions,
ein Worker pro Server, `MCPServerSessionError`).
`tools/selection.py` (437 Z.) — vier Strategien: `all`, `keyword`, `embedding`, `llm` (Default).

### 2.8 Desktop

PyQt6-Tray-App. `app.py` (2.915 Z.) enthält Startup-Flow, Fenster, Daemon-Thread, Ollama-Gating
(`_ollama_runtime_flags`), OpenAI-kompatible Erreichbarkeitsprüfung, Crash-Detection,
Single-Instance-Lock, Update-Check. `desktop_app.spec.md` sagt zwar „jarvis has no knowledge of
desktop_app", aber `jarvis/output/tts.py:594` importiert das Face-Widget — die Trennung ist bereits
verletzt.

### 2.9 CI / Packaging

`.github/workflows/`: `tests.yml`, `build-desktop.yml`, `release.yml`, `release-smoke.yml`.
Der Release-Text nennt Ollama/LM Studio/vLLM (`release.yml:145`, `:291`).
Installer: Inno Setup (`installer/windows/jarvis_setup.iss`) + `install_cuda.ps1`.
`requirements.txt`: 41 Zeilen, u. a. `faster-whisper==1.0.3`, `chatterbox-tts==0.1.2`,
`piper-tts>=1.3.0`, `faiss-cpu`, `mlx-whisper` (nur darwin/arm64), `nvidia-cublas-cu12`,
`nvidia-cudnn-cu12` (nur win32), `PyQt6`, `playwright`, `pytesseract`.

### 2.10 Evals

`EVALS.md`: 340/354 bestanden, gemessen gegen **`gemma4:e2b`** und **`gpt-oss:20b`** — beides lokale
Modelle. Die gesamte Eval-Baseline ist an lokale Modelle gebunden und nach dem Umbau bedeutungslos.

---

## 3. Inventar aller lokalen Modellpfade

### 3.1 Chat-/Reasoning-Modelle (zu entfernen)

| Ort | Was |
|---|---|
| `src/jarvis/llm/ollama.py` | komplettes Backend (391 Z.) |
| `src/jarvis/llm/openai_compatible.py` | komplettes Backend (498 Z.) |
| `src/jarvis/llm/factory.py` | Dispatch auf `ollama`/`openai_compatible` |
| `src/jarvis/llm/tiers.py` | FAST/CHAT-Tier-Auflösung |
| `src/jarvis/config.py:17-50` | `SUPPORTED_CHAT_MODELS`, Defaults |
| `src/jarvis/config.py:91-114, 186-246` | `llm_*`, `ollama_*`, `embedding_*`, `fast_model`, Timeouts, Digest-/Planner-Flags |
| `src/jarvis/config.py:337-404` | Migrationen v1–v3 (Ollama-Promotion, Tier-Faltung) |
| `src/jarvis/reply/planner.py` (851 Z.) | Planner + Step-Resolver |
| `src/jarvis/reply/evaluator.py` (410 Z.) | bereits deprecated |
| `src/jarvis/reply/enrichment.py` (885 Z.) | Extractor + 3 Digest-Pässe |
| `src/jarvis/reply/compound_query.py` (169 Z.) | Legacy-Zerlegung |
| `src/jarvis/reply/prompts/model_variants.py` (296 Z.) | modellgrößenabhängige Prompts |
| `src/jarvis/listening/intent_judge.py` (539 Z.) | LLM-Intent-Klassifikation |
| `src/jarvis/tools/selection.py:260-437` | LLM-Router + Embedding-Strategie |
| `src/jarvis/reply/engine.py` | Text-Tool-Call-Parser, Größen-Branching, Digest-Verdrahtung |
| `src/jarvis/memory/graph_ops.py` | 4 LLM-Pässe (Extraktion, Best-Child, Merge) |
| `src/jarvis/memory/conversation.py` | Summariser + 2 Bulk-Rewrites |
| `src/jarvis/tools/builtin/weather.py:60` | LLM-Ortsextraktion |
| `src/jarvis/tools/builtin/nutrition/log_meal.py:48,136` | Nährwert-Extraktor + Follow-up |
| `src/jarvis/utils/vram.py` (344 Z.) | VRAM-Erkennung für Modellwahl |

### 3.2 Embedding-Pfade (zu entfernen)

`utils/vector_store.py`, `utils/fast_vector_store.py`, `db.py:65-75` (`_VSS_SCHEMA_SQL`),
`db.py:416-435` (`upsert_summary_embedding`), `db.py:142-241` (Hybrid-Suche, Vektoranteil),
`config.sqlite_vss_path`, `embedding_*`-Konfigfelder, `faiss-cpu` in `requirements.txt`,
alle sechs `embed()`-Aufrufstellen aus §2.5.

### 3.3 UI / Setup / Installer (zu entfernen)

| Ort | Was |
|---|---|
| `setup_wizard.py:105-321` | `OllamaStatus`, CLI-/Server-/Modell-Prüfungen |
| `setup_wizard.py:869-1008` | `ProviderChoicePage` (Ollama vs. OpenAI-kompatibel) |
| `setup_wizard.py:1010-1530` | `OpenAICompatiblePage` + 3 Worker |
| `setup_wizard.py:1532-1673` | `OllamaInstallPage` |
| `setup_wizard.py:1675-1869` | `OllamaServerPage` |
| `setup_wizard.py:1871-2362` | `ModelsPage` (VRAM-Budget, `ollama pull`) |
| `settings_window.py:56-71` | Kategorien `llm`, `llm_provider` |
| `app.py` | `_ollama_runtime_flags`, Ollama-Autostart, Modellverifikation, `_check_openai_compat_reachable` |
| `desktop_app/mcp_catalogue.py` | bleibt, aber Einträge prüfen |

### 3.4 Zu **behaltende** lokale Modelle (isolierter Voice-Fallback)

| Ort | Was | Begründung |
|---|---|---|
| `listening/listener.py:253-292, 1482+` | faster-whisper / MLX-Whisper | STT-Fallback, ausdrücklich erlaubt |
| `dictation/dictation_engine.py` | teilt Whisper-Modell | Dictation ist reines Audio→Text |
| `output/tts.py:611-970` | Piper | TTS-Fallback, Standard |
| `output/tts.py:350-608` | Chatterbox | **Entscheidung E-7 offen** (§36) |
| `installer/windows/install_cuda.ps1` | cuBLAS/cuDNN | ausschließlich für STT/TTS |
| `requirements.txt:9,23,24,29,30,33` | faster-whisper, chatterbox, piper, nvidia-*, mlx-whisper | dito |

### 3.5 Gemeinsam genutzte Komponenten (**nicht** automatisch löschen)

| Pfad | Status |
|---|---|
| `C:\Users\User\AppData\Local\Programs\Ollama` | **Fremd.** Hermes' `config.yaml` referenziert `ollama-launch` als Fallback-Provider. Nicht deinstallieren. |
| `~\.ollama\models` | **Fremd/geteilt.** Enthält 20+ Modelle, die Hermes nutzt. Nicht löschen. |
| `C:\Users\User\.lmstudio` | **Fremd.** Auf PATH, JARVIS-unabhängig. |
| `C:\Users\User\AppData\Local\hermes` | **Fremd**, wird integriert, nicht verwaltet. |
| `~/.local/share/jarvis/models/piper` | **JARVIS-eigen**, bleibt. |
| Whisper-/HF-Cache (`~/.cache/huggingface`) | **Geteilt.** Nicht löschen. |

---

## 4. Keep / Remove / Replace-Matrix

Legende: **K** behalten · **R** entfernen · **E** ersetzen · **U** umbauen

| Modul | Ent. | Ziel |
|---|---|---|
| `jarvis/llm/backend.py` | E | `providers/base.py` — `ProviderAdapter` (Run-orientiert, nicht Completion-orientiert) |
| `jarvis/llm/ollama.py` | R | — |
| `jarvis/llm/openai_compatible.py` | R | — |
| `jarvis/llm/factory.py` | E | `providers/registry.py` |
| `jarvis/llm/tiers.py` | R | Modellwahl pro Run, nicht pro Kontext |
| `jarvis/reply/engine.py` | E | `runtime/run_manager.py` + `providers/*` |
| `jarvis/reply/planner.py` | R | Provider plant |
| `jarvis/reply/evaluator.py` | R | bereits tot |
| `jarvis/reply/enrichment.py` | E | `memory/retrieval.py` (FTS5/BM25, kein LLM) |
| `jarvis/reply/compound_query.py` | R | — |
| `jarvis/reply/prompts/` | U | ein System-Prompt-Fragment pro Provider, keine Größenvarianten |
| `jarvis/system_prompt.py` | U | wird Provider-Systemprompt-Anhang |
| `jarvis/listening/intent_judge.py` | E | `listening/intent_rules.py` (deterministisch) |
| `jarvis/listening/listener.py` | U | Voice-Zustandsmaschine, STT-Route protokolliert |
| `jarvis/listening/wake_detection.py` | K | — |
| `jarvis/listening/echo_detection.py` | K | — |
| `jarvis/listening/state_manager.py` | U | um Provider-/Route-Status erweitert |
| `jarvis/listening/transcript_buffer.py` | K | — |
| `jarvis/dictation/*` | K | unverändert |
| `jarvis/output/tts.py` | U | Cloud-TTS-Primärpfad + Piper-Fallback, Safe-Reply-Grenze |
| `jarvis/output/tune_player.py` | K | — |
| `jarvis/memory/db.py` | U | FTS5 bleibt, VSS raus, Provenienz-/Retention-Spalten rein |
| `jarvis/memory/graph.py` | K | Schema bleibt, Single-Writer erzwingen |
| `jarvis/memory/graph_ops.py` | U | LLM-Pässe an Provider, gebündelt, Hintergrund |
| `jarvis/memory/conversation.py` | U | Summariser an Provider; Hot-Window deterministisch |
| `jarvis/memory/recall_gate.py` | K | kein LLM |
| `jarvis/tools/registry.py` | U | wird MCP-Tool-Katalog |
| `jarvis/tools/selection.py` | U | nur `all` / `keyword` / `capability`; LLM+Embedding raus |
| `jarvis/tools/builtin/*` | K | bis auf LLM-Pässe in `weather`/`log_meal` |
| `jarvis/tools/external/mcp_*` | K | wird zusätzlich Server-seitig gespiegelt |
| `jarvis/utils/vector_store.py` | R | — |
| `jarvis/utils/fast_vector_store.py` | R | — |
| `jarvis/utils/vram.py` | R | keine Modellwahl mehr |
| `jarvis/utils/fuzzy_search.py` | K | FTS-Query-Generierung wird wichtiger |
| `jarvis/utils/redact.py` | K | wird sicherheitskritischer (Cloud!) |
| `jarvis/utils/location.py` | K | GeoLite2 lokal |
| `jarvis/daemon.py` | U | wird Core-Service-Bootstrap |
| `desktop_app/app.py` | U | zerlegen (§13) |
| `desktop_app/setup_wizard.py` | E | neuer Wizard: 3 Provider-Logins statt Ollama-Kette |
| `desktop_app/settings_window.py` | U | Metadaten-getrieben, LLM-Kategorien ersetzt |
| `desktop_app/memory_viewer.py` | U | kein eigener DB-Writer mehr → API-Client |
| `desktop_app/updater.py` | K | Signaturprüfung ergänzen |
| `desktop_app/cuda_recovery.py` | K | STT-Fallback |
| **neu** | + | `providers/`, `operator/`, `security/`, `runtime/`, `api/`, `hermes/` |

---

## 5. Zielarchitektur

```
┌──────────────────────────────────────────────────────────────────────┐
│  Clients                                                             │
│   ├── Windows: PyQt6 (kurzfristig)  ──┐                              │
│   └── macOS:   SwiftUI (Stufe B)   ───┤ HTTP+SSE, lokal, tokenbasiert│
└───────────────────────────────────────┼──────────────────────────────┘
                                        ▼
┌──────────────────────────────────────────────────────────────────────┐
│  jarvis-core (Python, ein Prozess, ein DB-Writer)                    │
│                                                                      │
│  api/          versionierte lokale HTTP+SSE-API (127.0.0.1)          │
│  runtime/      EventBus · RunManager · SessionMonitor                │
│  providers/    ProviderAdapter ABC                                   │
│                ├── ClaudeAdapter   (claude CLI, stream-json)         │
│                ├── CodexAdapter    (codex app-server, JSON-RPC v2)   │
│                └── HermesAdapter   (hermes CLI + gateway)            │
│                ModelCatalog · CapabilityRegistry · AuthManager       │
│  voice/        VoiceRouter (STT/TTS-Zustandsmaschine)                │
│  memory/       SQLite (FTS5/BM25 + Graph) — einziger Writer          │
│  tools/        MCP-Server (stdio) exponiert JARVIS-Tools             │
│  security/     CapabilityPolicy · TaintTracker · ApprovalManager     │
│  operator/     ProjectRegistry · SpecManager · RunStore · UsageColl. │
│  hermes/       CronBridge · GatewayLifecycle · DeliveryRouter        │
│  ports/        AudioIn/Out · Hotkey · Clipboard · Screen · OCR · …   │
└──────────────────────────────────────────────────────────────────────┘
     │ stdio/MCP            │ stdio/JSON-RPC        │ CLI/HTTP
     ▼                      ▼                       ▼
  claude CLI            codex app-server        hermes gateway + cron
  (Claude-Abo)          (ChatGPT/Codex-Abo)     (Nous/Codex-Abo)
```

**Nichtverhandelbare Invarianten:**

1. Genau **ein** Prozess öffnet `jarvis.db` schreibend (der Core). Alle anderen lesen über die API.
2. Genau **ein** lokaler HTTP-Server (die Core-API). Der Memory-Viewer wird eine Route darin,
   kein zweiter Flask-Prozess.
3. Kein Codepfad in `src/jarvis/` importiert `desktop_app`. Die heutige Verletzung
   (`output/tts.py:594`) wird über einen `Notifications`-Port aufgelöst.
4. Kein LLM-Aufruf außerhalb von `providers/`.
5. Kein Netzwerkaufruf mit Nutzerdaten außerhalb von `providers/` und explizit freigegebenen Tools.

---

## 6. Provider- und Modellarchitektur

### 6.1 `ProviderAdapter` (neuer Kernvertrag)

Run-orientiert statt Completion-orientiert — das ist der eigentliche Schnitt gegenüber `LLMBackend`:

| Methode | Rückgabe | Vertrag |
|---|---|---|
| `id()` | `str` | `"claude"` / `"codex"` / `"hermes"` |
| `auth_status()` | `AuthStatus` | `{logged_in, method, account, plan, expires_at?}`; **fail closed** |
| `list_models()` | `ModelCatalog` | dynamisch; `source` = `"api"` / `"config"` / `"unknown"` |
| `capabilities(model)` | `Capabilities` | Tools, MCP, Streaming, Bilder, Struktur-Output, Steering |
| `start_run(spec)` | `RunHandle` | erzeugt/erneuert Session; pinnt Modell |
| `stream(run)` | `Iterator[RunEvent]` | normalisierte Events (§6.3) |
| `steer(run, text)` | `bool` | Mid-Run-Nachricht |
| `interrupt(run)` | `bool` | Abbruch |
| `resume(session_id)` | `RunHandle` | Wiederaufnahme |
| `fork(session_id)` | `RunHandle` | Verzweigung |
| `list_sessions()` | `list[SessionInfo]` | inkl. fremder Sessions (read-only) |
| `usage()` | `UsageSnapshot` | Quota/Rate-Limits, `available: bool` |
| `health()` | `HealthReport` | Erreichbarkeit + Latenz |

Nicht unterstützte Fähigkeiten geben `NotSupported` zurück — **niemals** eine simulierte Antwort.

### 6.2 Adapter-Transporte (verifiziert)

| Provider | Transport | Beleg |
|---|---|---|
| Claude | `claude -p --input-format stream-json --output-format stream-json --include-partial-messages`, persistenter Prozess pro Session | `claude --help`, v2.1.220 |
| Codex | `codex app-server` (JSON-RPC v2 über stdio); Fallback `codex exec --json` | Schema-Dump v0.153.4 |
| Hermes | `hermes -z <prompt>` einmalig; `hermes gateway` + `hermes cron` für 24/7 | `hermes --help` |

### 6.3 Normalisiertes Event-Modell

Jeder Adapter mappt seinen Stream auf:

```
run.started   {run_id, provider, model, session_id, cwd, ts}
turn.started  {turn_id}
text.delta    {text}
thinking      {text}          # nur wenn Provider liefert
tool.call     {name, args_redacted, tool_id}
tool.result   {tool_id, ok, summary, bytes}
approval.needed {kind, detail, options}     → ApprovalManager
needs_you     {question}                    → UI + Zustellkanal
usage.delta   {input, output, cached, cost_hint?}
run.finished  {status: ok|error|cancelled|quota|timeout, reason}
```

`run.finished(status=quota)` ist ein **eigener** Zustand — kein stiller Provider-Wechsel.

### 6.4 Modellkatalog

| Provider | Quelle | Verfügbarkeit |
|---|---|---|
| **Codex** | `app-server` → `Model/list`, `ModelProvider/capabilities/read` | ✅ vollständig dynamisch |
| **Hermes** | `hermes model --refresh` (holt `/v1/models` je Provider) + `cache/model_catalog.json` | ✅ dynamisch |
| **Claude** | **keine Listing-Schnittstelle in der CLI** | ⚠️ offene Capability |

**Fail-Closed-Entwurf für Claude (§7.4):** Der Katalog startet leer. Es gibt genau zwei Wege,
Einträge zu erhalten: (a) der Nutzer trägt einen Modellnamen ein, und JARVIS **verifiziert** ihn
mit einem 1-Token-Probe-Run (`claude -p --model X --output-format json "ping"`) und schreibt ihn
mit `source="verified-probe"` in den Katalog; (b) ein Run läuft erfolgreich, dann wird das
tatsächlich benutzte Modell aus dem `init`-Event übernommen. **Es wird nie eine Modellliste
geraten oder aus dem Trainingswissen erfunden.** Der leere Katalog blockiert nicht: ohne Angabe
läuft `claude` mit seinem eigenen Default, und JARVIS protokolliert `model_source="provider-default"`.

### 6.5 Modellwechsel-Regeln

- Ein laufender Run behält sein Modell. Immer.
- Eine globale Änderung wirkt nur auf **neue** Runs.
- Jeder Wechsel erzeugt `audit.model_changed {from, to, scope, actor}` und ist in der UI
  am Run-Header sichtbar.
- Cronjobs pinnen Provider **und** Modell zum Anlegezeitpunkt; ein globaler Wechsel lässt sie
  unberührt (§8.4).

---

## 7. Authentifizierungs- und Subscription-Architektur

### 7.1 Verifizierte Fakten (live geprüft, nicht aus Doku)

| Provider | Befehl | Ergebnis |
|---|---|---|
| Claude | `claude auth status` | `{"loggedIn":true,"authMethod":"claude.ai","apiProvider":"firstParty","subscriptionType":"pro"}` |
| Claude | `claude --version` | `2.1.220 (Claude Code)` |
| Codex | `codex login status` | `Logged in using ChatGPT` |
| Codex | `codex doctor` | `stored auth mode: chatgpt` · `stored ChatGPT tokens: true` · `stored API key: false` |
| Codex | `codex --version` | `codex-cli 0.153.4` |
| Hermes | `config.yaml:1-4` | `provider: openai-codex`, `base_url: https://chatgpt.com/backend-api/codex`, `default: gpt-5.6-terra` |
| Hermes | `hermes proxy providers` | nur **`nous`** (Nous Portal) und **`xai`** — beide „not logged in" |

### 7.2 Daraus folgende harte Aussagen

1. **`hermes proxy` kann Claude und Codex nicht als OpenAI-kompatible HTTP-Quelle bereitstellen.**
   Nur Nous und xAI. Eine Architektur, die alles über den Hermes-Proxy leitet, wäre eine Fantasie.
2. **Hermes fährt heute selbst auf dem ChatGPT-/Codex-Abo.** Codex- und Hermes-Nutzung teilen
   sich damit dasselbe Kontingent. Das muss der UsageCollector zusammenführen, sonst zeigt die UI
   zwei unabhängige Budgets, die es nicht gibt.
3. **Compliance-Warnung:** Hermes' `agent/anthropic_adapter.py` setzt gezielt
   `user-agent: claude-code/<version>` und `x-app: cli`, um mit Anthropic-OAuth-Credentials zu
   sprechen (Zeilen 402, 915, 953, 1545-1556, 3004). Das ist Client-Impersonation eines
   Consumer-Abos durch eine Drittanwendung. **Dieser Weg wird für JARVIS nicht eingeplant.**
   Der Claude-Zugang läuft ausschließlich über die offizielle `claude`-CLI.

### 7.3 AuthenticationManager

- Kein eigener Token-Store für Claude und Codex. Beide CLIs verwalten ihre OAuth-Credentials
  selbst (Codex: `auth.json` unter `CODEX_HOME`; Claude: OS-Keychain/Credential Manager).
  JARVIS **liest sie nicht** und kopiert sie nicht.
- Statuserhebung ausschließlich über die Statusbefehle aus §7.1, gecacht 60 s.
- Login-Auslösung: JARVIS startet `claude auth login` bzw. `codex login` bzw. `hermes login`
  in einem sichtbaren Terminal. Kein Headless-Credential-Handling, keine Passworteingabe in JARVIS.
- Eigene Secrets (Brave-Key, MCP-Env-Vars) landen im **Windows Credential Manager** bzw.
  **macOS Keychain**, nie in `config.json`, nie in SQLite, nie im Log.
- `claude setup-token` (langlebiges Abo-Token) wird **nur** verwendet, wenn der Nutzer es
  ausdrücklich einrichtet — Standard ist der interaktive Login.

### 7.4 Quotas, Fair Use, offene Capabilities

| Frage | Antwort | Quelle |
|---|---|---|
| Nutzungslimits Claude Pro | existieren (5-h- und Wochenfenster), **nicht** per CLI abfragbar | keine CLI-Schnittstelle gefunden |
| Nutzungslimits Codex | `Account/rateLimits/read`, `Account/usage/read`, `Account/rateLimits/updated` | app-server v2 Schema |
| Nutzungslimits Hermes | `hermes status`, `agent/account_usage.py` | CLI |
| Modell-Liste Claude | **nicht verfügbar** | CLI-Hilfe |
| Embeddings | **bei keinem der drei verfügbar** | CLI-Hilfen + Schema |
| Unbegrenzte Nutzung | **nein**, bei keinem | — |

**Fail-Closed-Verhalten pro offener Capability:**

- *Claude-Quota unbekannt:* Die UI zeigt „Kontingent: unbekannt (Provider stellt keine Auskunft
  bereit)" — nie eine geschätzte Zahl. Ein `run.finished(status=quota)` schaltet Claude für ein
  konfigurierbares Fenster in `degraded` und **fragt den Nutzer**, ob auf Codex/Hermes umgeschaltet
  werden soll. Automatischer Providerwechsel ist standardmäßig **aus**.
- *Claude-Modellliste fehlt:* siehe §6.4.
- *Embeddings fehlen:* Memory läuft FTS5-only. Es gibt keinen versteckten vierten Provider und
  keinen lokalen Embedder als Ersatz.

### 7.5 Kein stiller API-Key-Fallback

`ANTHROPIC_API_KEY` und `OPENAI_API_KEY` werden aus der Provider-Umgebung **aktiv entfernt**,
bevor ein Adapter-Prozess startet, damit kein Abrechnungspfad versehentlich einspringt. Der
Adapter prüft nach dem Start `auth_status().method` und bricht ab, wenn es nicht der erwartete
Abo-Modus ist (`claude.ai` bzw. `chatgpt`). Zusätzlich `--bare` **nicht** verwenden, da es laut
CLI-Hilfe genau auf `ANTHROPIC_API_KEY` umschaltet.

---

## 8. Hermes-24/7-Architektur

### 8.1 Vorhandenes (verifiziert)

`hermes` liegt unter `%LOCALAPPDATA%\hermes` mit eigenem venv (`hermes-agent\venv`), gestartet über
`bin\hermes.cmd`. Relevante Subcommands: `gateway`, `cron`, `proxy`, `send`, `serve`, `auth`,
`login`, `status`, `sessions`, `monitoring`, `mcp`, `memory`, `memory-graph`, `computer-use`,
`kanban`, `project`, `approvals`, `security`, `doctor`, `pause`, `resume`, `webhook`, `peer`.

`hermes cron` bietet bereits: `list, create/add, edit, pause, resume, run, remove, status,
runs/history, notepad, tick`. Der `notepad` ist ein durabler KV-Speicher pro Job über Läufe hinweg.
Cron-Interna liegen in `hermes-agent/cron/`: `scheduler.py`, `executions.py`, `jobs.py`,
`lifecycle_guard.py`, `monitor.py`, `notepad.py`, `blueprint_catalog.py`, `suggestions.py`.

**Konsequenz: JARVIS baut keinen eigenen Scheduler.** Es baut eine `CronBridge` auf `hermes cron`.
Alles andere wäre ein zweiter, konkurrierender Scheduler.

### 8.2 Gateway als überwachter Dienst

| Plattform | Mechanismus | Details |
|---|---|---|
| Windows (Phase 7) | **Task Scheduler**, Trigger „At log on", Ausführung als angemeldeter Nutzer | kein `LocalSystem` — Hermes braucht das Nutzerprofil für OAuth-Tokens |
| Windows (später) | optional Dienst via `nssm`/`sc` | nur wenn tokenloser Betrieb nachweisbar |
| macOS (Stufe B) | `SMAppService` (launchd, `LaunchAgent`) | Nutzerkontext, Keychain-Zugriff |

JARVIS überwacht den Gateway per `hermes status` (Poll 30 s), startet ihn bei Bedarf neu
(exponentieller Backoff, max. 5 Versuche/Stunde) und zeigt den Zustand in der Tray-UI.
**Kill Switch:** `hermes pause` (dokumentierter Notaus) plus ein JARVIS-eigenes Flag, das jede
neue Job-Dispatch-Anforderung ablehnt.

### 8.3 Job-Modell (JARVIS-Seite)

```
CronJob {
  id, name, enabled,
  schedule:  {kind: "once"|"recurring", cron_expr, tz: "Europe/Berlin", natural_text}
  provider:  "claude"|"codex"|"hermes"       # gepinnt
  model:     str|null                        # gepinnt, null = provider default
  skills:    [str]        # hermes --skills
  toolsets:  [str]        # hermes -t
  cwd:       str          # Arbeitsverzeichnis, muss in Workspace-Allowlist liegen
  capability_profile: str # Security-Profil (§15)
  delivery:  [{channel, target}]             # desktop | hermes-send | file
  retry:     {max_attempts, backoff_sec, on: [error, timeout]}
  missed_run_policy: "skip"|"run_once_on_wake"|"run_all"
  overlap:   "skip"|"queue"|"kill_previous"
  limits:    {max_runtime_sec, max_output_bytes, max_tool_calls, max_child_jobs: 0}
  approval:  "none"|"before_side_effects"|"before_run"
}
```

### 8.4 Zeitzonen und DST

`Europe/Berlin` wird **explizit** gespeichert, nie „lokale Zeit". Regeln:
- Ein Job um 07:30 läuft im Sommer und Winter um 07:30 Ortszeit.
- Nicht existierende Zeiten (Frühjahrsumstellung 02:00–03:00) → Ausführung um 03:00.
- Doppelte Zeiten (Herbst) → genau **eine** Ausführung, die erste.
- `tzdata` ist bereits in `requirements.txt:18` (win32) vorhanden.

### 8.5 Sicherheitsregeln für Jobs

- `max_child_jobs: 0` als Default — **ein Job darf keine neuen Jobs anlegen**, außer der Nutzer
  hebt das pro Job auf.
- Jobs mit `approval != "none"` laufen bis zum Gate und warten; Zeitüberschreitung → Abbruch,
  nicht Autofreigabe.
- Externe Inhalte (Web, Mail, MCP-Ausgaben) sind grundsätzlich `tainted` (§15.3). Ein Job, der
  taintete Daten gelesen hat, verliert für den Rest des Laufs Schreib-, Shell- und Sende-Rechte,
  sofern nicht explizit erlaubt.
- Kosten-/Quota-Grenze pro Job und global; bei Überschreitung `status=quota`, keine Retries.
- Maximale Parallelität global (Default 2), pro Provider (Default 1).

### 8.6 Beispiel-Workflows (Blueprints)

| Blueprint | Provider | Zeitplan | Zustellung |
|---|---|---|---|
| Morgenbriefing (Kalender, Wetter, Tasks) | hermes | `30 6 * * 1-5` | Desktop-Notification + TTS-Ansage |
| Git-/Build-Status der aktiven Projekte | codex | `0 8,17 * * *` | Desktop |
| Wochen-Dependency-/Security-Report | codex | `0 9 * * 1` | Datei + Desktop |
| Nachrichtenrecherche zu Interessen | hermes | `0 7 * * *` | Desktop |
| Projektstatus aus Spec-Checkboxen | claude | `0 18 * * 5` | Desktop |
| Erinnerungen (einmalig) | hermes | `once` | TTS + Notification |
| Überwachter Langlauf-Coding-Run | codex | manuell | `needs_you` → sofortige Notification |
| „Claude/Codex braucht dich" | intern | ereignisgesteuert | Notification + optional `hermes send` |

---

## 9. Voice-Primär- und Fallbackarchitektur

### 9.1 Grundsatzentscheidung (Abweichung von der Vorgabe — bitte bestätigen)

Die Vorgabe sagt „Cloud-STT ist primär". **Empfehlung: STT bleibt lokal-primär.**

Begründung: (a) Keiner der drei erlaubten Zugänge bietet einen STT-Endpunkt — Cloud-STT bräuchte
einen **vierten** Provider, was die Vorgabe an anderer Stelle verbietet. (b) Lokales STT ist
ausdrücklich erlaubt und läuft heute mit CUDA in Echtzeit. (c) Rohes Mikrofonaudio in die Cloud zu
schicken ist der invasivste denkbare Datenfluss. Das Gleiche gilt für TTS: Piper ist lokal, schnell
und erlaubt.

Der Router ist trotzdem so gebaut, dass eine Cloud-Route jederzeit eingehängt werden kann
(`stt_route_preference`, `tts_route_preference`). Standard: `local_first`. **Entscheidung E-1 (§36).**

### 9.2 Zustandsmaschine (Annex D)

Siehe Annex D. Kernregeln:

1. Transkribierter Text geht **ausschließlich** an Claude, Codex oder Hermes — nie an ein lokales
   Modell zur Interpretation.
2. Lokales TTS erhält **ausschließlich** den finalen, freigegebenen Antworttext (`SafeReply`).
   Der `SafeReply` ist ein eigener Typ; die TTS-Schicht akzeptiert nichts anderes.
3. Ohne Cloud-Reasoning-Verbindung: transkribieren erlaubt, **statische** Fehlermeldung sprechen
   erlaubt, intelligente Antwort simulieren **verboten**.
4. Pro Turn werden protokolliert und in der UI angezeigt: `provider`, `model`, `stt_route`,
   `tts_route`, `latency_ms{stt, provider_first_token, provider_total, tts}`, `fallback_reason`.
5. Circuit Breaker pro Route: 3 Fehler in 60 s → Route 5 min `open`, danach ein Half-Open-Probe.
6. **Fallback-Schleifen-Sperre:** Jeder Turn hat ein `fallback_budget = 1`. Fällt der Fallback auch
   aus, endet der Turn mit statischer Fehleransage. Kein Ringen zwischen Routen.

### 9.3 Isolationsgrenze für lokale KI

```
┌─ Voice-Fallback-Grenze (lokale KI erlaubt) ─────────────────┐
│  faster-whisper / MLX-Whisper   →  nur str                  │
│  Piper / (Chatterbox)           ←  nur SafeReply            │
│  KEIN Tool-Zugriff · KEIN Secret-Zugriff · KEIN Memory      │
│  KEIN Provider-Zugang · KEINE Klassifikation · KEIN Embed   │
└─────────────────────────────────────────────────────────────┘
```

Durchgesetzt durch: eigenes Python-Package `jarvis.voice.local` ohne Imports aus `tools/`,
`memory/`, `providers/`, `security/`; ein Import-Linter-Test (`tests/test_voice_isolation.py`)
lässt den Build scheitern, wenn diese Grenze verletzt wird.

Deterministisch erlaubt bleiben: Aufnahme, Resampling, WebRTC-VAD, Wake-Word-Fuzzy-Matching,
Echo-Erkennung, Barge-in, Audio-Queue, Wiedergabe, Lautstärke-/Gerätesteuerung.

---

## 10. Memory ohne lokale Embeddings

### 10.1 Suchschicht

Ersatz für den heutigen 60/40-Hybrid (`db.py:142-268`):

```
score = 0.55 · bm25_norm(summaries_fts)
      + 0.20 · recency_decay(ts_utc, half_life=30d)
      + 0.15 · metadata_match(project, person, topic, time_range)
      + 0.10 · graph_access_score(node)
```

- FTS5 bleibt (`porter`-Tokenizer). Zusätzlich eine `unicode61`-Variante als zweite FTS-Tabelle,
  damit nicht-englische Sprachen nicht durch Porter-Stemming beschädigt werden — das ist die
  konkrete Umsetzung von „keine hardcodierten Sprachmuster".
- `utils/fuzzy_search.py` liefert bereits flexible FTS-Queries und wird ausgebaut (Trigramm-
  Fallback bei 0 Treffern).
- Der `recall_gate` (`memory/recall_gate.py`) bleibt unverändert — er ist bereits LLM-frei.

### 10.2 Strukturierte Metadaten (neue Spalten)

`conversation_summaries` und `memory_nodes` erhalten:

| Spalte | Zweck |
|---|---|
| `provenance` | `user` \| `agent_inference` \| `external_untrusted` |
| `source_provider` | `claude` \| `codex` \| `hermes` \| `null` |
| `source_model` | Modell-ID zum Erzeugungszeitpunkt |
| `source_run_id` | Rückverweis in den RunStore |
| `confidence` | 0..1, vom erzeugenden Provider |
| `project` | Projekt-Slug oder `null` |
| `people` | JSON-Array normalisierter Personennamen |
| `retention_class` | `permanent` \| `rolling_365d` \| `rolling_90d` \| `session` |
| `expires_at` | berechnet aus `retention_class` |

### 10.3 Trennung der Vertrauensstufen

- `provenance='external_untrusted'` wird **nie** in den System-Prompt-Kontext gehoben, sondern nur
  als eingezäunter Datenblock (`UNTRUSTED …` Fence, wie heute in `web_search.spec.md`).
- `agent_inference` wird visuell und im Prompt als Ableitung markiert („abgeleitet, nicht gesagt").
- Nur `user`-Fakten dürfen in das Warm-Profile (`graph_ops.build_warm_profile`).

### 10.4 Cloud-gestützte Memory-Verarbeitung

Summariser, Graph-Extraktion, Best-Child-Platzierung und Node-Merge laufen weiter — aber:
gebündelt (ein Run pro Tag statt vier Aufrufe pro Sitzung), im Hintergrund, über **einen**
konfigurierten Provider (`memory_provider`, Default `hermes` weil quotenschonend und ohnehin 24/7).
Jeder erzeugte Eintrag trägt volle Provenienz. Fällt der Provider aus, unterbleibt die
Verarbeitung — es gibt keinen lokalen Ersatz und keine erfundene Zusammenfassung.

### 10.5 Löschbarkeit, Export, Retention

- `DELETE /api/memory/{id}` mit Kaskade in FTS + Graph.
- `GET /api/memory/export?format=jsonl|md` — vollständig, inkl. Provenienz.
- Nächtlicher Retention-Sweep löscht `expires_at < now`. Löschungen erzeugen Audit-Events.
- Ein „Alles vergessen"-Knopf mit Zweifach-Bestätigung und Export-Angebot davor.

---

## 11. Tool- und MCP-Architektur

### 11.1 JARVIS als MCP-Server

Neu: `src/jarvis/tools/server/` — ein Stdio-MCP-Server, der die elf Builtins plus Memory-Tools
exponiert. Angebunden über:

- Claude: `claude --mcp-config <json> --strict-mcp-config --allowedTools "mcp__jarvis__*"`
- Codex: `codex mcp add` bzw. `Config/mcpServer/reload` im app-server
- Hermes: `hermes mcp`

Damit sind JARVIS-Tools für alle drei Provider verfügbar, ohne dass JARVIS den Tool-Loop fährt.

### 11.2 Deterministische Vorauswahl bleibt

`tools/selection.py` behält `all`, `keyword` und bekommt `capability` (Filter nach Plattform,
Projekt, Capability-Profil, Provider-Fähigkeit). **`llm` und `embedding` entfallen.**
`toolSearchTool` bleibt als MCP-Tool, arbeitet aber deterministisch (BM25 über Tool-Beschreibungen)
statt über den LLM-Router.

KI-basierte Tool-Auswahl findet ausschließlich innerhalb von Claude/Codex/Hermes statt.

### 11.3 Bestehende MCP-Runtime

`tools/external/mcp_runtime.py` (persistente Sessions, ein Worker pro Server, Retry bei
Session-Verlust) bleibt unverändert — sie ist für JARVIS-als-MCP-**Client** weiterhin nötig
(eigene Tools, die selbst MCP-Server aufrufen) und ist bereits spezifiziert.

---

## 12. Claude-/Codex-Operator

### 12.1 Verifizierte Codex-Fähigkeiten (`app-server` v2, 92 Methoden)

Vollständig relevant für den Operator:

| Bereich | Methoden |
|---|---|
| Sessions | `Thread/start`, `list`, `read`, `resume`, `fork`, `archive`, `unarchive`, `delete`, `revert`, `rollback`, `injectItems`, `metadata/update`, `name/set`, `items/list`, `turns/list`, `loaded/list`, `compact/start`, `goal/{set,get,clear}` |
| Runs | `Turn/start`, `Turn/interrupt`, `Turn/steer` |
| Modelle | `Model/list`, `ModelProvider/capabilities/read` |
| Konto | `Account/read`, `Account/usage/read`, `Account/rateLimits/read`, `Account/login/{start,cancel}`, `Account/logout` |
| Sicherheit | `PermissionProfile/list`, `Thread/approveGuardianDeniedAction` |
| Review | `Review/start` |
| Umfeld | `Skills/list`, `McpServerStatus/list`, `Config/read`, `Config/value/write`, `Hooks/list`, `App/list` |
| Dateien | `Fs/{readFile,writeFile,readDirectory,getMetadata,copy,remove,watch,unwatch}` |
| Prozesse | `Command/exec`, `exec/write`, `exec/resize`, `exec/terminate` |

Das deckt praktisch die gesamte geforderte Operator-Funktionsliste ab — **ohne** AppleScript und
ohne Tastendruck-Simulation.

### 12.2 Verifizierte Claude-Fähigkeiten

| Bedarf | Weg |
|---|---|
| Session-Liste | `claude agents --json` (aktive + Hintergrund, TTY-frei) |
| Session-Details | `~/.claude/projects/<slug>/<uuid>.jsonl` lesen (54 Projekte vorhanden) |
| Run starten | `claude -p --output-format stream-json --session-id <uuid>` |
| Steering | `--input-format stream-json` → weitere User-Nachrichten auf stdin |
| Resume / Fork | `--resume <id>` / `--continue`; Fork durch Resume in neue `--session-id` |
| Cancel | Prozesssignal + `Turn`-Abbruch im Stream |
| Hintergrundlauf | `--bg` |
| Permissions | `--permission-mode`, `--allowedTools`, `--disallowedTools` |
| Struktur-Output | `--json-schema` |
| MCP | `--mcp-config`, `--strict-mcp-config`, `claude mcp` |
| Auth | `claude auth status` (JSON) |
| Health | `claude doctor` |
| **Modell-Liste** | **nicht verfügbar** (§6.4) |
| **Usage/Quota** | **nicht per CLI verfügbar** (§7.4) |

### 12.3 Fremde vs. eigene Sessions

- **Fremde Sessions** (vom Nutzer im Terminal gestartet): erscheinen in der Liste, sind
  **read-only**. Kein Steering, kein Interrupt, kein Fork ohne ausdrückliche Übernahme
  („Session übernehmen" → erzeugt eine Fork unter JARVIS-Kontrolle).
- **Eigene Sessions**: voll steuerbar; JARVIS hält den Prozess bzw. die app-server-Verbindung.

### 12.4 Operator-Komponenten

`ClaudeAdapter`, `CodexAdapter`, `HermesAdapter`, `ProviderCapabilityRegistry`, `ModelCatalog`,
`AuthenticationManager`, `SessionMonitor`, `RunManager`, `ProjectRegistry`, `SpecManager`,
`ApprovalManager`, `UsageCollector`, `EventStore` — alle in `src/jarvis/operator/` bzw.
`src/jarvis/providers/`, alle plattformneutral, alle ohne Qt-Import.

---
## 13. Windows-Plan

### 13.1 Kurzfristig: PyQt-App behalten, `app.py` zerlegen

`src/desktop_app/app.py` (2.915 Z.) wird in sechs Module geschnitten — reine Bewegung, keine
Verhaltensänderung, damit der Schritt einzeln testbar ist:

| Neu | Inhalt (heutige Fundstellen) |
|---|---|
| `startup/bootstrap.py` | Single-Instance-Lock, Crash-Marker, Splash-Steuerung |
| `startup/preflight.py` | Provider-Health statt `_ollama_runtime_flags` / `_check_openai_compat_reachable` |
| `tray/tray_controller.py` | `JarvisSystemTray`, Menü, Icon-Zustände |
| `windows/` | Log-Viewer, Face, Dictation-History, Diary |
| `core_client.py` | HTTP+SSE-Client gegen die Core-API |
| `platform_win.py` | `RegisterHotKey`, Win32-Clipboard, `SendInput`, ConPTY, Notifications |

Gleichzeitig: **jeder Import von `desktop_app` innerhalb `src/jarvis/` verschwindet** (heute
`output/tts.py:594` → Face-Widget). Ersatz: `ports/notifications.py` mit einem Null-Adapter im
Headless-Betrieb. Ein Import-Guard-Test verhindert Rückfälle.

### 13.2 Pfade

| Zweck | Ziel (Windows) |
|---|---|
| Config | `%APPDATA%\jarvis\config.json` |
| Datenbank | `%LOCALAPPDATA%\jarvis\jarvis.db` |
| Logs | `%LOCALAPPDATA%\Jarvis\logs\` |
| Piper-Stimmen | `%LOCALAPPDATA%\jarvis\models\piper\` |
| Secrets | Windows Credential Manager |

Heute liefert `config.py:76` hart `Path.home()/".local"/"share"/"jarvis"` — also einen
POSIX-Pfad auch unter Windows, während `desktop_app/paths.py` und die Spec bereits
`%LOCALAPPDATA%` nennen. Diese Inkonsistenz wird in Phase 0 mit einer **reversiblen** Migration
aufgelöst (kopieren, nicht verschieben; alter Pfad bleibt 30 Tage bestehen; Rückschalter im
Settings-Dialog).

### 13.3 Plattformfähigkeiten

| Fähigkeit | Umsetzung |
|---|---|
| Bildschirmaufnahme | `QScreen::grabWindow` kurzfristig; `Windows.Graphics.Capture` mittelfristig |
| OCR | `pytesseract` bleibt (deterministisch, kein KI-Modell im verbotenen Sinn); Windows.Media.Ocr als Alternative prüfen |
| Vision | Screenshot → **nur** an erlaubten Cloud-Provider, nur nach Approval, nie automatisch |
| Globaler Hotkey | `RegisterHotKey` statt `pynput`-Polling (robuster, kein Keylogger-Verdacht) |
| Clipboard / Einfügen | Win32-Clipboard + `SendInput` |
| Terminal | ConPTY für Operator-Terminals |
| Notifications | Windows Toast |
| Autostart | Task Scheduler „At log on" für JARVIS **und** Hermes-Gateway |
| CUDA | bleibt, **ausschließlich** für STT/TTS-Fallback |

### 13.4 Installer

Inno Setup bleibt. Ergänzungen: **Code-Signing** (Authenticode; bisher unsigniert), Upgrade-Test
(alt → neu, Konfig bleibt erhalten), Rollback-Test, `VerifyCudaInstall`-Hook bleibt. Die Task
„Download NVIDIA CUDA libraries" wird umbenannt auf „GPU-Beschleunigung für Spracherkennung",
damit klar ist, wofür sie noch da ist.

---

## 14. macOS-Plan

### 14.1 Stufe A — bestehende ARM64-PyQt-App lauffähig

| Punkt | Umsetzung |
|---|---|
| STT | MLX-Whisper **nur** als Fallback-Engine, Isolationsgrenze wie §9.3 |
| TTS | Piper; Chatterbox auf macOS deaktiviert (CUDA-abhängig) |
| Pfade | `~/Library/Application Support/Jarvis`, `~/Library/Logs/Jarvis`, `~/.config/jarvis` |
| Signierung | Developer ID Application, Hardened Runtime |
| Notarisierung | `notarytool` + Stapling |
| Verpackung | DMG statt ZIP; der bestehende `ditto`-Updatepfad bleibt |
| Berechtigungen | Mikrofon, Bildschirmaufnahme, Bedienungshilfen — Diagnose-Panel |
| Upgrade | ein Generationen-Rollback wie heute |

### 14.2 Stufe B — native SwiftUI-Menüleisten-App

`MenuBarExtra` · `AVAudioEngine` · `ScreenCaptureKit` · `Vision`-OCR · `NSPasteboard` · `CGEvent` ·
`UserNotifications` · `SMAppService` · Keychain · native Berechtigungsdiagnose · Sparkle-Updater.

Der Python-Core läuft als **signierter, überwachter Helper** (eigenes Bundle in
`Contents/Library/LoginItems` bzw. `Helpers/`), gestartet und überwacht von der Swift-App, verbunden
über dieselbe lokale HTTP+SSE-API wie der Windows-Client. **Die Fachlogik wird nicht nach Swift
portiert** — nur die Präsentations- und Plattformschicht.

---

## 15. Security Threat Model

### 15.1 Vertrauensgrenzen (Annex G)

| Zone | Vertrauen |
|---|---|
| Z0 Nutzer (Mikro, Tastatur, UI) | vertraut |
| Z1 jarvis-core | vertraut, durchsetzend |
| Z2 lokale Voice-Modelle | **isoliert**, keine Rechte |
| Z3 Provider-Prozesse (claude/codex/hermes) | halbvertraut — führen fremden Modell-Output aus |
| Z4 MCP-Server | halbvertraut, pro Server konfiguriert |
| Z5 Web/Mail/Repo-Inhalte | **untrusted, tainted** |
| Z6 Dateisystem außerhalb Workspace | geschützt |

### 15.2 Default-Deny-Capability-Policy

```
CapabilityProfile {
  fs_read:   [Pfad-Globs]      # Default: nur aktives Projekt
  fs_write:  [Pfad-Globs]      # Default: leer
  shell:     "none"|"allowlist"|"sandboxed"
  network:   "none"|"allowlist"|"full"
  side_effects: {git_push, install, deploy, send_message, delete} = alle false
}
```

Vier Profile: `read_only` (Default für Sprachdialog), `project_dev`, `automation`, `unrestricted`
(nur manuell, pro Session, mit Ablauf). Jede Erweiterung ist ein Audit-Event.

### 15.3 Speicher- und Pfadregeln (hart kodiert)

| Regel | Durchsetzung |
|---|---|
| `C:\Users\User\OneDrive` **niemals** schreiben | Denylist vor jedem Schreib-/Lösch-Tool, auch für Provider-Prozesse via `--add-dir`-Kontrolle |
| Autoritative Daten `\\DS723plus\home` / `C:\Users\User\SynologyDrive` | Schreiben nur nach Approval |
| Repository-Workspace | Allowlist-Standard |
| Unsichere Löschkandidaten | nach `N:\99 - Quarantine`, mind. 30 Tage |
| Secrets | Denylist: `.env*`, `*.pem`, `id_*`, `~/.ssh`, `~/.aws`, `~/.codex/auth.json`, `~/.claude*`, Browserprofile, `hermes/auth.json` — Lesen wie Schreiben verboten |

### 15.4 Taint-Tracking

Jeder Datenblock trägt `taint: bool` + `origin`. Regeln:
- Web-, MCP-, Repository- und Nachrichteninhalte sind `tainted`.
- Ein Run, der tainted Daten konsumiert hat, verliert `side_effects` (Approval nötig).
- Tainted Inhalte werden im Prompt eingezäunt und nie als Instruktion präsentiert
  (bestehendes Muster aus `web_search.spec.md` wird generalisiert).
- Indirekte Prompt-Injection: Der Fence bleibt bei Kürzung erhalten (heute bereits umgesetzt) und
  gilt künftig für **alle** Quellen, nicht nur Websuche.

### 15.5 Netzwerk

SSRF-Schutz erweitert: DNS-Auflösung vor Verbindung, Ablehnung privater/Link-Local-/CGNAT-Bereiche,
**Redirect-Kette komplett prüfen** (jeder Hop), Schutz gegen DNS-Rebinding durch Pinning der
aufgelösten IP für die Dauer der Verbindung. Gilt für `webSearch`, `fetchWebPage` und jeden
MCP-Server mit HTTP-Transport.

### 15.6 Lokale API

- Bind ausschließlich `127.0.0.1`, zufälliger Port, in Datei mit `0600` hinterlegt.
- Bearer-Token pro Start, im Credential Store, nie im Log.
- `Origin`-Prüfung + CSRF-Token für alle mutierenden Routen.
- IPC zum Provider-Prozess: nur stdio, keine offenen Ports.
- Rate-Limit pro Route.

### 15.7 Prozess- und Ausgabengrenzen

Zeitlimit, CPU-/Speicherlimit (Job Objects unter Windows), maximale Ausgabegröße, harte
Abbruchlogik (Prozessbaum killen, nicht nur den Elternprozess), Zombie-Erkennung beim Start.

### 15.8 Updates

Signierte Installer (Windows: Authenticode; macOS: Developer ID + Notarisierung), SHA-256-Prüfung
jedes Downloads gegen die Release-Metadaten, Ablehnung bei Abweichung. Der bestehende
Commit-basierte Update-Erkennungsmechanismus bleibt.

---

## 16. Daten- und Konfigurationsmigration

### 16.1 Konfiguration

Neue Migration **v4** in `_migrate_config`:

1. **Backup zuerst:** `config.json` → `config.json.pre-v4.bak` (einmalig, nie überschreiben).
2. Alle Ollama-/OpenAI-kompatiblen Schlüssel werden **nicht gelöscht**, sondern nach
   `_legacy_local_llm` verschoben (ein Objekt): `llm_provider`, `llm_base_url`, `llm_api_key`,
   `llm_chat_model`, die vier `embedding_*`, die drei `ollama_*`, `fast_model`. Damit ist ein
   Downgrade möglich und nichts geht verloren.
3. Neue Schlüssel mit Defaults: `execution_mode`, `default_provider`, `provider_models`,
   `memory_provider`, `stt_route_preference`, `tts_route_preference`, `capability_profile`.
4. `_config_version = 4`.

Solange `execution_mode == "local"` gilt, liest `merge_config()` den `_legacy_local_llm`-Block
zwischen Defaults und Live-Konfiguration wieder ein. Nur so ist die Phase-0-Zusage „Verhalten
identisch" einlösbar und der Rückweg bleibt ein Konfigwert. Jede Stelle, die Konfiguration liest
(Loader, Settings-Fenster, Provider-Seiten des Wizards), geht durch `merge_config()`.

Das **Entfernen** von Schlüsseln (`planner_*`, `evaluator_*`, `*_digest_enabled`,
`tool_selection_strategy` mit Werten `llm`/`embedding` → `keyword`, `sqlite_vss_path`) ändert
Verhalten und gehört deshalb zu **Config v5** in Phase 6, zusammen mit dem Wegfall des
Feature-Flags. In Phase 0 wird nichts entfernt.

### 16.2 Datenbank

Migration **v2** des Speichers:

1. `VACUUM INTO` Backup nach `jarvis.db.pre-v2.bak`.
2. Neue Spalten aus §10.2 mit `ALTER TABLE ADD COLUMN` (rückwärtskompatibel).
3. Bestandsdaten: `provenance='agent_inference'` für alle bestehenden Summaries und Graph-Knoten
   (ehrlich: wir wissen es nicht besser), `retention_class='permanent'`.
4. `embeddings` / `summary_vec` werden **nicht** gedroppt, sondern in
   `jarvis.db.embeddings.bak` ausgelagert und in der Hauptdatei gelöscht. FAISS-Index-Dateien
   werden nach `<db_dir>/legacy_vectors/` verschoben, nicht entfernt.
5. Zweite FTS-Tabelle `summaries_fts_uni` (`unicode61`) anlegen und aus Bestand füllen.

### 16.3 Pfadmigration Windows

Kopieren statt Verschieben, Marker-Datei im alten Pfad, Rückschalter in den Einstellungen,
Aufräumen erst nach 30 Tagen und nur nach Bestätigung.

---

## 17. Exakte Lösch- und Cleanup-Liste

### 17.1 Sicher löschbar (JARVIS-eigen, im Repo)

```
src/jarvis/llm/ollama.py
src/jarvis/llm/openai_compatible.py
src/jarvis/llm/tiers.py
src/jarvis/reply/planner.py            + planner.spec.md
src/jarvis/reply/evaluator.py          + evaluator.spec.md
src/jarvis/reply/compound_query.py
src/jarvis/reply/prompts/model_variants.py
src/jarvis/listening/intent_judge.py
src/jarvis/utils/vector_store.py
src/jarvis/utils/fast_vector_store.py
src/jarvis/utils/vram.py
tests/test_llm_backend.py  test_llm_factory.py  test_llm_openai_compatible.py
tests/test_llm_thinking.py  test_llm_arguments_encoding.py  test_openai_compatible_e2e.py
tests/test_model_tiers.py  test_provider_model_resolution.py  test_config_models.py
tests/test_planner.py  test_engine_planner_integration.py  test_evaluator.py
tests/test_intent_judge.py  test_enrichment.py  test_enrichment_model_routing.py
tests/test_tool_router_resolution.py  test_tool_selection.py  test_vram.py
tests/test_setup_wizard.py  test_setup_wizard_install_chain.py
tests/test_factory_dispatch_wiring.py  test_text_tool_call_parser.py
tests/test_engine_kv_cache.py  test_install_cuda.py (nur Ollama-Teile)
evals/  (29 Dateien — vollständig neu zu schreiben, §23)
```

### 17.2 Sicher löschbar (JARVIS-eigene Dateien auf Platte, nach Bestätigung)

```
~/.local/share/jarvis/*.faiss           # FAISS-Index (nach Backup)
<db_dir>/legacy_vectors/                # nach 30 Tagen
%LOCALAPPDATA%\jarvis\*.embcache        # falls vorhanden
```

### 17.3 **Nicht** anfassen (fremd oder geteilt)

```
C:\Users\User\AppData\Local\Programs\Ollama    # Hermes-Fallback nutzt es
~\.ollama\models                               # 20+ Modelle, Hermes-Referenz
C:\Users\User\.lmstudio
C:\Users\User\AppData\Local\hermes             # eigenständiges Produkt
~\.cache\huggingface                           # geteilt (Whisper braucht es)
~\.codex, ~\.claude                            # Provider-Credentials
C:\Users\User\OneDrive                         # read-only, absolut
```

### 17.4 Cleanup-Schritt (bestätigungspflichtig, eigenes Ticket)

Ein Tray-Menüpunkt „🧹 Aufräumen nach Cloud-Umstellung" zeigt eine Tabelle mit Pfad, Größe,
Kategorie (`jarvis-only` / `shared` / `foreign`) und Vorschlag. Nur `jarvis-only` ist
vorausgewählt. Geschätzt freigegeben: FAISS/Embeddings ~50–300 MB. Ollama-Modelle werden
**angezeigt mit dem Hinweis „wird von Hermes genutzt — nicht löschen"**, aber nicht antippbar.

---

## 18. Datenbankschema und Migrationen

Siehe Annex I für die Gesamtübersicht. Neue Tabellen:

```sql
CREATE TABLE runs (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT,
  model_source TEXT NOT NULL,          -- api|verified-probe|provider-default|pinned
  session_id TEXT, parent_run_id TEXT REFERENCES runs(id),
  cwd TEXT, project TEXT, capability_profile TEXT NOT NULL,
  status TEXT NOT NULL,                -- running|ok|error|cancelled|quota|timeout|needs_you
  started_at TEXT NOT NULL, finished_at TEXT,
  input_tokens INTEGER, output_tokens INTEGER, cached_tokens INTEGER,
  origin TEXT NOT NULL,                -- voice|ui|cron|api
  cron_job_id TEXT
);
CREATE TABLE run_events (
  id INTEGER PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  seq INTEGER NOT NULL, ts TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
  UNIQUE(run_id, seq)
);
CREATE TABLE audit_events (          -- append-only, kein UPDATE/DELETE (Trigger erzwingt)
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL,
  action TEXT NOT NULL, subject TEXT, detail TEXT, run_id TEXT, prev_hash TEXT, hash TEXT
);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, run_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL,
  requested_at TEXT NOT NULL, decided_at TEXT, decision TEXT, decided_by TEXT,
  artifact_hash TEXT                   -- hashgebundene Freigabe (Spec/Plan-Review)
);
CREATE TABLE projects (
  slug TEXT PRIMARY KEY, name TEXT NOT NULL, root_path TEXT NOT NULL,
  default_provider TEXT, default_model TEXT, capability_profile TEXT NOT NULL,
  created_at TEXT NOT NULL, last_used TEXT
);
CREATE TABLE specs (
  id TEXT PRIMARY KEY, project TEXT NOT NULL REFERENCES projects(slug),
  path TEXT NOT NULL, content_hash TEXT NOT NULL, status TEXT NOT NULL,
  approved_hash TEXT, approved_at TEXT
);
CREATE TABLE cron_jobs (             -- Spiegel von `hermes cron`, Hermes bleibt Wahrheit
  id TEXT PRIMARY KEY, hermes_job_id TEXT, name TEXT NOT NULL, spec_json TEXT NOT NULL,
  enabled INTEGER NOT NULL, last_sync TEXT NOT NULL
);
CREATE TABLE usage_snapshots (
  id INTEGER PRIMARY KEY, ts TEXT NOT NULL, provider TEXT NOT NULL,
  available INTEGER NOT NULL,        -- 0 = Provider stellt keine Auskunft bereit
  payload TEXT
);
```

`audit_events` bildet eine Hash-Kette (`prev_hash` → `hash`), damit nachträgliche Manipulation
auffällt. Ein `BEFORE UPDATE`/`BEFORE DELETE`-Trigger mit `RAISE(ABORT)` macht die Tabelle
append-only.

---

## 19. Lokale API / IPC-Verträge

Basis `http://127.0.0.1:<port>/api/v1`, Bearer-Token, SSE für Streams.

| Methode | Route | Zweck |
|---|---|---|
| GET | `/health` | Core + Provider-Health |
| GET | `/providers` | Auth-Status, Fähigkeiten, Modelle, Usage |
| POST | `/providers/{id}/login` | startet interaktiven Login |
| GET | `/models?provider=` | Katalog inkl. `source` |
| POST | `/runs` | Run starten (`provider, model?, prompt, project?, profile`) |
| GET | `/runs` · `/runs/{id}` | Liste / Details |
| GET | `/runs/{id}/events` | **SSE** |
| POST | `/runs/{id}/steer` · `/interrupt` · `/resume` · `/fork` | Steuerung |
| GET | `/sessions?provider=` | inkl. fremder (read-only markiert) |
| GET/POST | `/approvals` · `/approvals/{id}` | Freigaben |
| GET | `/memory/search?q=&filters=` | FTS5/BM25 |
| GET | `/memory/graph` | Knotenbaum |
| DELETE | `/memory/{id}` · GET `/memory/export` | Löschen/Export |
| GET/POST/PATCH/DELETE | `/cron/jobs` | CronBridge |
| POST | `/cron/jobs/{id}/run` · `/pause` · `/resume` | Steuerung |
| GET | `/usage` | aggregiert, mit `available`-Flag je Provider |
| GET | `/audit?since=` | Audit-Log |
| GET | `/projects` · `/specs` | Operator |
| POST | `/voice/say` | `SafeReply` sprechen (nur Core-intern erlaubt) |

Ereignisse zum Client (SSE `/events`): `run.*`, `approval.needed`, `needs_you`, `provider.health`,
`usage.updated`, `cron.run.*`, `voice.state`.

---

## 20. UI-Informationsarchitektur

```
Tray
 ├── 🎙️ Sprachassistent    (Status, Provider, Modell, Route, Stummschaltung)
 ├── 💬 Sessions           Liste · Details · Steering · Inbox (needs_you)
 ├── ▶️ Runs               laufend/verlauf · Cancel/Retry/Resume/Fork · Journal
 ├── 📁 Projekte           Registry · Repo-Suche · sichere Dateiansicht · Editor/Terminal/Browser
 ├── 📋 Specs & Pläne      Spec-Workflow · Dokumentreview · hashgebundene Freigabe · Plan-Review
 ├── ⏰ Automation         Cronjobs · Blueprints · Läufe · Notepad
 ├── 🧠 Memory             Diary · Graph · Provenienz-Filter · Export/Löschen
 ├── 📊 Usage              pro Provider · „unbekannt" wo unbekannt
 ├── 🔌 Verbindungen       Provider-Auth · MCP-Server · Skills · Health
 ├── 🛡️ Sicherheit         Profile · Approvals · Audit-Log
 └── ⚙️ Einstellungen      metadatengeneriert
```

Immer sichtbar im Run-Header: **Provider · Modell · Capability-Profil · Taint-Status**.
Sprachausgabe: priorisiert (Fehler > `needs_you` > Antwort > Hintergrundmeldung), satzweise,
`🔁 Wiederholen` spricht den gespeicherten `SafeReply` erneut **ohne** neuen LLM-Turn.

---

## 21. Observability, Health und Audit

- **Strukturierte Logs** (JSONL) zusätzlich zum heutigen `debug_log`. Kategorien bleiben.
  Jede Zeile trägt `run_id`, `provider`, `model`, `route`.
- **Health-Checks** je Provider alle 60 s: Auth, Erreichbarkeit, Latenz eines 1-Token-Probes
  (nur wenn seit 10 min kein echter Run lief — kein Quota-Verbrennen).
- **Latenzmessung** pro Turn wie in §9.2; Perzentile im Usage-Dashboard.
- **Audit** für: Modellwechsel, Profilwechsel, Approval-Entscheidungen, Secret-Zugriffsversuche,
  Löschungen, Cron-Anlage/-Änderung, Provider-Login/-Logout, Fallback-Auslösungen.
- **Redaction** (`utils/redact.py`) läuft künftig **vor** jedem Provider-Aufruf, nicht nur vor dem
  Speichern. Das ist der wichtigste Einzelunterschied zum lokalen Betrieb.

---

## 22. Teststrategie

| Ebene | Inhalt |
|---|---|
| Unit | Adapter-Event-Normalisierung, Policy-Entscheidungen, FTS-Ranking, Zeitzonen/DST, Migrationen |
| Kontrakt | **Aufgezeichnete** Provider-Streams (Golden Files) → Adapter-Parser. Verhindert, dass ein CLI-Update unbemerkt bricht |
| Integration | Core-API end-to-end mit Fake-Adaptern; MCP-Server gegen echten `mcp`-Client |
| Live-Smoke (nightly, opt-in) | je Provider ein echter 1-Token-Run + `auth_status` + `usage`. Markiert `@pytest.mark.live` |
| Isolation | Import-Linter: `jarvis.voice.local` ohne verbotene Imports; `jarvis` ohne `desktop_app` |
| Sicherheit | OneDrive-Schreibversuch, Secret-Pfad-Lesen, SSRF (Redirect zu 169.254.169.254), Prompt-Injection-Fixtures, Taint-Degradation |
| Single-Writer | Test, dass genau ein Prozess `jarvis.db` schreibend öffnet |
| Visuell | Qt-Screenshots der neuen Panels (Regressionsvergleich) |
| Performance | `tests/performance/` neu kalibriert auf Provider-Latenz statt Ollama |

**Wichtiger Grundsatz aus CLAUDE.md, hier besonders relevant:** Fake-Subprozess-Tests beweisen bei
CLI-Adaptern fast nichts. Deshalb Golden-File-Kontrakttests aus **echten** Läufen plus ein
nächtlicher Live-Smoke.

---

## 23. Evals pro Provider und Modell

Die bestehende Eval-Suite ist an `gemma4:e2b` / `gpt-oss:20b` gebunden und wird ersetzt.
Neue Struktur:

```
evals/
  conftest.py            # Provider-Matrix-Fixture (claude, codex, hermes)
  behaviour/             # Antwortqualität, Tool-Nutzung über MCP, Sprachtreue
  memory/                # Retrieval-Qualität FTS5 vs. alter Hybrid (Regressionsmessung!)
  routing/               # deterministische Tool-Vorauswahl
  voice/                 # Intent-Regeln ohne LLM (Wake, Echo, Stop, Hot-Window)
  security/              # Injection, Taint, Approval-Gates
```

Zwei Baselines sind zwingend **vor** dem Umbau zu erfassen, damit die Regression messbar ist:

1. **Memory-Retrieval-Baseline**: aktuelle Hybrid-Suche (Embedding+FTS) gegen einen festen
   Fragenkatalog → Recall@3, plus derselbe Katalog ohne Vektoren. Gemessen in T-002
   (`evals/baselines/memory_recall.py`, Ergebnis in `docs/baselines/memory_recall_baseline.json`):
   Hybrid 33,3 %, FTS-only 77,8 %. Referenz für T-031 ist der **FTS-only-Wert**, nicht der Blend.
2. **Voice-Intent-Baseline**: die bestehenden Intent-Judge-Evals (**42 Fälle**, nicht 48: 20
   Einzel- + 22 Mehrsegment-Fälle) gegen die deterministischen Regeln. Gemessen in T-003
   (`evals/baselines/voice_intent.py`, Ergebnis in `docs/baselines/voice_intent_baseline.json`):
   Intent-Judge 97,6 %, Regeln 66,7 %, Lücke **30,95 Prozentpunkte**. Die Regeln scheitern fast
   ausschließlich an Mehrsegment-Fällen (45,5 % gegenüber 90 % bei Einzelsegmenten). Ziel für
   T-037 bleibt ein Verlust unter 15 Prozentpunkten, das sind rund 16 Punkte Arbeit.

Eval-Läufe gegen Abo-Provider verbrauchen Kontingent. Deshalb: kleine Kernsuite (≤ 30 Fälle) im
Nightly, volle Suite manuell mit ausgewiesenem Kostenhinweis.

---

## 24. CI/CD

| Workflow | Änderung |
|---|---|
| `tests.yml` | Provider-Adapter-Kontrakttests gegen Golden Files (keine Netzwerkzugriffe). Neue Gates: Import-Isolation, Single-Writer, „kein Ollama-String im Produktionscode" |
| `build-desktop.yml` | `faiss-cpu` entfällt; Filterliste anpassen; Windows-Signierung einbauen |
| `release.yml` | Release-Text ohne Ollama/LM Studio; Voraussetzungen: Claude-CLI / Codex-CLI / Hermes |
| `release-smoke.yml` | Smoke prüft zusätzlich: Core-API startet, Provider-Health meldet ehrlich „not logged in" statt zu crashen |
| **neu** `live-smoke.yml` | manuell/nightly, self-hosted Runner mit echten Logins |

Neuer CI-Wächter: ein Test, der `git grep -iE "ollama|lm ?studio|llama\.cpp|localai|vllm"` über
`src/` laufen lässt und bei Treffern außerhalb der Migrations-Legacy-Blöcke fehlschlägt.

---

## 25. Packaging, Signing und Updates

- **Windows:** PyInstaller → Inno Setup → **Authenticode-Signatur** (heute unsigniert). Der
  Updater prüft künftig SHA-256 gegen die Release-Metadaten und verweigert bei Abweichung.
- **macOS:** Developer ID + `notarytool` + Stapling + DMG. Der bekannte Konflikt „Ad-hoc-Signierung
  bricht Qt-WebEngine-Symlinks" (in `release.yml` dokumentiert) verschwindet mit echter Signierung.
- **Provider-CLIs werden nicht gebündelt.** Sie sind eigenständige Produkte mit eigenen Updates.
  JARVIS erkennt sie über PATH, prüft Mindestversionen und bietet einen Hinweis mit
  Installationsanleitung, wenn sie fehlen. Bündeln wäre ein Lizenz- und Update-Alptraum.
- Mindestversionen (heute vorhanden): `claude` ≥ 2.1.0, `codex` ≥ 0.153.0, `hermes` (lokal
  installiert, Versionsprüfung über `hermes --version`).

---

## 26. Lizenz- und Clean-Room-Strategie

### 26.1 Ausgangslage

- Dieses Repo: **„Jarvis AI Assistant License", Copyright (c) 2025 Baris Sencan** — eine eigene,
  nicht-standardisierte Lizenz. **Vor dem Umbau ist zu klären, ob sie Umbau, Umbenennung und
  Weitergabe des Ergebnisses erlaubt.** Das ist Entscheidung **E-9** (§36) und blockiert
  streng genommen eine Veröffentlichung, nicht die private Nutzung.
- Ethans Repo (`.research/ethanplusai-jarvis/`): eigene MIT-ähnliche Lizenz,
  Copyright (c) 2026 Ethan Rogers.

### 26.2 Clean-Room-Regeln (verbindlich)

1. Aus Ethans Repo wird **kein** Quellcode, **kein** Prompt, **keine** UI-Komponente und
   **keine** Datei kopiert — auch nicht paraphrasiert.
2. Zulässige Grundlage: die vom Nutzer formulierte Anforderungsliste und öffentlich beobachtbares
   Produktverhalten (Screenshots, README, Bedienung).
3. Wer implementiert, liest den fremden Quellcode nicht. Die Anforderungen werden vorher in
   `docs/operator-requirements.md` als lizenzfreie Spezifikation festgehalten; nur dieses Dokument
   geht in die Implementierungstickets ein.
4. Das Verzeichnis `.research/` ist bereits untracked und bleibt es; es kommt in `.gitignore`
   und wird nach Abschluss der Anforderungsextraktion gelöscht.
5. Namensgleichheit („JARVIS") ist unabhängig davon zu prüfen — beide Projekte tragen den Namen.

### 26.3 Anbieterbedingungen

- Der Betrieb von Claude Code und Codex CLI als von JARVIS gestartete Prozesse mit dem eigenen
  Abo des Nutzers ist die vorgesehene Nutzung dieser Werkzeuge (beide bieten `-p`/`exec` explizit
  für Skripting an). Kein Reverse Engineering, keine UA-Fälschung, keine Weitergabe von Tokens.
- **Ausdrücklich ausgeschlossen:** der Hermes-Weg über gefälschte `claude-code`-User-Agents
  (§7.2.3). Wenn Hermes ihn intern nutzt, ist das Hermes' Sache; JARVIS konfiguriert Hermes nicht
  darauf und dokumentiert das Risiko.

---

## 27. Risikoanalyse

| # | Risiko | W | A | Gegenmaßnahme |
|---|---|---|---|---|
| R1 | Sprachdialog wird zu langsam (6–20 s statt 4,5 s) | hoch | hoch | Persistente Sessions, sofortige Quittungs-TTS, Streaming satzweise, `hermes` als schnellster Pfad für Kurzantworten |
| R2 | Abo-Kontingent im Alltag erschöpft | hoch | hoch | Usage-Dashboard, Warnschwellen, Provider-Wahl pro Aufgabentyp, kein Auto-Fallback |
| R3 | Memory-Qualität bricht ohne Embeddings ein | hoch | mittel | Baseline vorher messen (§23), BM25+Metadaten+Recency-Tuning, ehrliche Kommunikation |
| R4 | CLI-Update bricht Adapter (kein stabiler Vertrag) | hoch | hoch | Golden-File-Kontrakttests, Versionspinning, Nightly-Live-Smoke, defensives Parsen |
| R5 | Wegfall des Intent-Judge macht Wake-Erkennung schlechter | hoch | **hoch** (gemessen: 30,95 Punkte Lücke) | Regeln aus den 42 Eval-Fällen hart nachbauen, Schwerpunkt Mehrsegment-Kontext, Barge-in beibehalten |
| R6 | Datenabfluss: Redaction greift nicht vor Cloud-Aufruf | mittel | **kritisch** | Redaction als Pflicht-Middleware im Adapter, Test mit Fixture-Secrets, Audit |
| R7 | Provider-Prozess schreibt außerhalb Workspace | mittel | hoch | `--add-dir`-Kontrolle, `--permission-mode`, `PermissionProfile`, Denylist, Approval |
| R8 | Doppelter DB-Writer bleibt bestehen | mittel | hoch | Single-Writer-Test als CI-Gate, Memory-Viewer wird API-Client |
| R9 | Lizenz erlaubt den Umbau nicht | mittel | hoch | E-9 vor Phase 1 klären |
| R10 | Hermes und Codex teilen dasselbe Kontingent unbemerkt | hoch | mittel | UsageCollector führt sie zusammen, UI zeigt „gemeinsames Budget" |
| R11 | Großer Refactor bricht funktionierende Voice-/Dictation-Features | mittel | hoch | Voice und Dictation werden in **keiner** Phase vor Phase 6 angefasst; Regressionstests |
| R12 | Lokales Modell überlebt versteckt als Fallback | mittel | hoch | CI-Grep-Gate, Import-Linter, Code-Review-Checkliste |

---

## 28. Rollbackstrategie

- **Pro Phase ein eigener Branch** gegen `develop`, ein PR, ein Squash-Merge. Rollback = Revert
  des einen Commits.
- **Feature-Flag `execution_mode`** (`local` | `subscription`) trägt die Phasen 1–4. Solange es
  `local` gibt, ist der Rückweg ein Konfigwert. Es wird erst in Phase 5 entfernt — dem Punkt, ab
  dem Rollback nur noch per Git geht.
- **Datenrollback:** `config.json.pre-v4.bak`, `jarvis.db.pre-v2.bak`,
  `jarvis.db.embeddings.bak`, `legacy_vectors/` — alle bleiben mindestens eine Release-Generation.
- **Release-Rollback:** Windows über Inno-Setup-Uninstaller + vorheriger Installer;
  macOS/Linux über den bestehenden Ein-Generationen-`.backup`-Mechanismus.
- **Kill Switch:** Ein Schalter „Alle Automationen anhalten" (`hermes pause` + interne Sperre)
  ist ab Phase 7 verfügbar und in der Tray-Wurzel erreichbar.

---
## 29. Priorisierte Phasen

Jede Phase ist ein PR gegen `develop`, einzeln testbar und einzeln revertierbar.
Komplexität: **S** ≤ 1 Tag · **M** 2–4 Tage · **L** 1–2 Wochen · **XL** > 2 Wochen (Solo-Entwickler-Maß).

---

### Phase 0 — Fundament, Baseline, Sicherungsnetz · **M**

- **Ziel:** Messbare Ausgangslage, Feature-Flag, Backups, CI-Wächter. Kein Verhaltenswechsel.
- **Begründung:** Ohne Vorher-Baseline lässt sich die Memory- und Voice-Regression später nicht
  beziffern, und ohne Flag gibt es in den Phasen 1–5 keinen Rückweg.
- **Betroffene Dateien:** `config.py` (Flag + v4-Migration), `.github/workflows/tests.yml`,
  `pytest.ini`, `.gitignore` (`.research/`).
- **Neu:** `docs/operator-requirements.md` (lizenzfreie Anforderungsspezifikation, §26.2),
  `docs/baselines/memory_recall_baseline.json`, `docs/baselines/voice_intent_baseline.json`,
  `tests/test_single_writer.py`, `tests/test_import_isolation.py`,
  `tests/test_no_local_llm_strings.py` (zunächst als `xfail`).
- **Entfernen:** nichts.
- **Datenmigration:** Config v4 legt `config.json.pre-v4.bak` an, verschiebt Ollama-Schlüssel nach
  `_legacy_local_llm`, setzt `execution_mode="local"`. **Verhalten identisch.**
- **Security:** keine.
- **Plattform:** keine.
- **Tests:** Migration hin (v3→v4) inkl. Backupdatei; Import-Isolation zeigt die bekannte
  Verletzung `output/tts.py:594` als `xfail`; Single-Writer-Test zeigt die bekannten fünf
  `GraphMemoryStore`-Konstruktionen als `xfail`.
- **Evals:** Baseline-Läufe erzeugen (Memory-Recall@3, Voice-Intent 42 Fälle).
- **Abnahme:** App startet unverändert; beide Baseline-Dateien existieren; alle bisherigen
  Tests grün.
- **Rollback:** Revert; `config.json.pre-v4.bak` zurückkopieren.
- **Abhängigkeiten:** keine.
- **Parallel:** Anforderungsspezifikation und Baselines können gleichzeitig entstehen.
- **Nicht-Ziele:** kein Provider-Code, keine Löschungen, keine UI.

---

### Phase 1 — Provider-Layer, headless · **L**

- **Ziel:** `ProviderAdapter` + drei Adapter + `AuthenticationManager` + `ModelCatalog` +
  `CapabilityRegistry`, vollständig ohne UI und ohne Anbindung an den Reply-Pfad.
- **Begründung:** Der riskanteste Teil (fremde CLI-Verträge) wird isoliert gebaut und bewiesen,
  bevor irgendetwas davon abhängt.
- **Betroffene Dateien:** keine bestehenden (rein additiv).
- **Neu:** `src/jarvis/providers/{__init__,base,claude,codex,hermes,registry,models,capabilities,auth}.py`,
  `src/jarvis/providers/providers.spec.md`, `tests/providers/golden/*.jsonl`.
- **Entfernen:** nichts.
- **Datenmigration:** keine.
- **Security:** Env-Scrubbing (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`) vor Prozessstart;
  Redaction-Middleware-Hook vorgesehen, noch ohne Nutzdaten.
- **Plattform:** Prozessstart Windows/macOS/Linux; Codex-app-server über stdio.
- **Tests:** Kontrakttests gegen Golden-Streams je Adapter; `auth_status()`-Parsing;
  `Model/list`-Parsing (Codex); Claude-Katalog bleibt leer ohne verifizierten Probe.
- **Evals:** keine (kein Nutzerverhalten betroffen).
- **Abnahme:** `python -m jarvis.providers.cli status` zeigt für alle drei ehrlich Auth, Modelle
  (bzw. „unbekannt"), Usage (bzw. „nicht verfügbar"); ein 1-Token-Probe-Run je Provider läuft
  manuell durch.
- **Rollback:** Revert (nichts hängt daran).
- **Abhängigkeiten:** Phase 0.
- **Parallel:** die drei Adapter sind unabhängig → drei parallele Arbeitspakete.
- **Nicht-Ziele:** keine Voice-Anbindung, kein Tool-Loop, keine UI, kein Entfernen von Ollama.

---

### Phase 2 — Core-Service, lokale API, UI-Entkopplung · **L**

- **Ziel:** `jarvis-core` als eigenständiger Prozess mit versionierter lokaler HTTP+SSE-API;
  `jarvis` importiert `desktop_app` nicht mehr; genau ein DB-Writer.
- **Begründung:** Strukturvoraussetzung für Operator-UI, macOS-Client und Single-Writer-Regel.
- **Betroffene Dateien:** `daemon.py`, `desktop_app/app.py` (Zerlegung §13.1),
  `desktop_app/memory_viewer.py` (Flask-Server → Routen im Core), `output/tts.py:594`
  (Face-Widget-Import), alle `GraphMemoryStore(...)`-Stellen.
- **Neu:** `src/jarvis/api/{server,routes,auth,events}.py`, `src/jarvis/ports/*.py`,
  `src/jarvis/runtime/event_bus.py`, `src/desktop_app/core_client.py`,
  `src/jarvis/api/api.spec.md`.
- **Entfernen:** eigenständiger Flask-Prozess des Memory-Viewers.
- **Datenmigration:** keine (nur Zugriffsweg ändert sich).
- **Security:** Loopback-Bind, zufälliger Port, Bearer-Token im Credential Store,
  Origin-/CSRF-Prüfung, Rate-Limit.
- **Plattform:** Windows-Pfadmigration (§16.3) hier, weil sie den Core betrifft.
- **Tests:** `test_single_writer.py` wird grün; `test_import_isolation.py` wird grün;
  API-Contract-Tests; Memory-Viewer-Regressionstests gegen die neuen Routen.
- **Evals:** keine.
- **Abnahme:** Memory-Viewer funktioniert unverändert über die Core-API; nur ein Prozess hält
  einen Schreib-Handle auf `jarvis.db`; Desktop startet.
- **Rollback:** Revert; alter Flask-Pfad ist im selben Commit entfernt worden, daher Revert
  vollständig.
- **Abhängigkeiten:** Phase 0.
- **Parallel:** API-Skelett, `app.py`-Zerlegung und Memory-Viewer-Umbau sind drei Pakete.
- **Nicht-Ziele:** keine neuen Features, keine Provider-Anbindung.

---

### Phase 3 — RunManager und Umschaltung des Antwortpfads · **XL**

- **Ziel:** `execution_mode="subscription"` liefert echte Antworten über Provider-Runs; der lokale
  Pfad bleibt unter `execution_mode="local"` unverändert erhalten.
- **Begründung:** Der Kern des Umbaus. Ab hier ist das Produkt funktional cloudbasiert.
- **Betroffene Dateien:** `reply/engine.py` (Weiche an genau einer Stelle),
  `listening/listener.py` (Dispatch), `daemon.py`.
- **Neu:** `src/jarvis/runtime/{run_manager,run_store,session_monitor}.py`,
  `src/jarvis/runtime/runtime.spec.md`.
- **Entfernen:** nichts (das kommt in Phase 6).
- **Datenmigration:** DB-v2-Teil 1 — `runs`, `run_events`, `audit_events`.
- **Security:** Redaction **verpflichtend** vor jedem Provider-Aufruf; Audit-Kette aktiv;
  Default-Capability-Profil `read_only`.
- **Plattform:** Prozesslebenszyklus (Windows Job Objects für harte Abbrüche).
- **Tests:** Weiche-Tests beider Modi; Redaction-Fixture mit E-Mail/Token/Pfad;
  Abbruch tötet den Prozessbaum; `status=quota` erzeugt keinen stillen Providerwechsel.
- **Evals:** Kernsuite `behaviour/` gegen alle drei Provider (klein, ≤ 30 Fälle).
- **Abnahme:** Eine gesprochene Frage wird über Claude beantwortet und gesprochen; Run erscheint im
  RunStore mit Provider, Modell, Tokens; Umschalten auf `local` funktioniert weiterhin.
- **Rollback:** `execution_mode="local"` — sofort, ohne Deployment.
- **Abhängigkeiten:** Phasen 1, 2.
- **Parallel:** RunStore/Audit und RunManager/Adapterverdrahtung sind trennbar.
- **Nicht-Ziele:** noch keine Tools über MCP (Provider nutzt in dieser Phase nur seine eigenen),
  kein Memory-Umbau, keine Löschungen.

---

### Phase 4 — JARVIS-Tools als MCP-Server, Security-Kern · **L**

- **Ziel:** Die elf Builtins + Memory-Tools stehen allen drei Providern über MCP zur Verfügung;
  Capability-Policy, Taint-Tracking und Approvals sind scharf.
- **Begründung:** Ohne diesen Schritt verliert JARVIS beim Umschalten seine eigenen Fähigkeiten.
- **Betroffene Dateien:** `tools/registry.py`, `tools/selection.py`, `tools/builtin/*`,
  `providers/*` (MCP-Verdrahtung).
- **Neu:** `src/jarvis/tools/server/{__init__,mcp_server,schema}.py`,
  `src/jarvis/security/{policy,taint,approvals,denylist}.py`,
  `src/jarvis/security/security.spec.md`.
- **Entfernen:** `tool_selection_strategy` Werte `llm` und `embedding` (Code bleibt bis Phase 6).
- **Datenmigration:** `approvals`-Tabelle.
- **Security:** Kernstück — Default-Deny, Workspace-Allowlist, OneDrive-Denylist,
  Secret-Denylist, SSRF-Härtung inkl. Redirect-Kette und DNS-Pinning, Taint-Degradation.
- **Plattform:** Pfadnormalisierung Windows (UNC, `\\?\`, Groß-/Kleinschreibung, 8.3-Kurznamen)
  — sonst ist jede Denylist umgehbar.
- **Tests:** OneDrive-Schreibversuch über jeden Weg (Tool, Provider-`--add-dir`, Shell);
  Secret-Pfad-Lesen; SSRF gegen `169.254.169.254` per Redirect; Prompt-Injection-Fixtures;
  Approval-Gate blockiert `git push`.
- **Evals:** `security/`-Suite neu.
- **Abnahme:** Claude ruft `mcp__jarvis__getWeather` erfolgreich auf; ein Schreibversuch nach
  OneDrive wird geblockt und auditiert; ein `git push` verlangt Freigabe.
- **Rollback:** Revert; MCP-Konfiguration wird nicht mehr an Provider übergeben.
- **Abhängigkeiten:** Phase 3.
- **Parallel:** MCP-Server und Security-Kern sind zwei Pakete mit klarer Schnittstelle.
- **Nicht-Ziele:** keine neuen Tools, kein Memory-Umbau.

---

### Phase 5 — Memory ohne lokale Embeddings · **L**

- **Ziel:** Retrieval vollständig deterministisch; Provenienz, Retention, Export, Löschung.
- **Begründung:** Der letzte Pflichtnutzer lokaler Modelle außerhalb von Voice.
- **Betroffene Dateien:** `memory/db.py`, `memory/conversation.py`, `memory/graph_ops.py`,
  `reply/enrichment.py`, `tools/selection.py`, `listening/listener.py:1619-1628`,
  `reply/engine.py:925`, `tools/builtin/tool_search.py:91`.
- **Neu:** `src/jarvis/memory/retrieval.py`, `src/jarvis/memory/retention.py`,
  `src/jarvis/memory/memory.spec.md` (ersetzt Teile von `summariser.spec.md`).
- **Entfernen:** `utils/vector_store.py`, `utils/fast_vector_store.py`, `_VSS_SCHEMA_SQL`,
  `upsert_summary_embedding`, `faiss-cpu` aus `requirements.txt`.
- **Datenmigration:** DB-v2-Teil 2 — Provenienz-/Retention-Spalten, `summaries_fts_uni`,
  Auslagerung von `embeddings`/`summary_vec` nach `jarvis.db.embeddings.bak`,
  FAISS-Index nach `legacy_vectors/`.
- **Security:** `external_untrusted` nie im Systemprompt; Export enthält Provenienz.
- **Plattform:** keine.
- **Tests:** Migration mit Bestandsdaten; Retrieval-Regression gegen die Phase-0-Baseline;
  Retention-Sweep; Export/Import-Roundtrip; Löschkaskade FTS+Graph.
- **Evals:** `memory/`-Suite; **Recall@3 wird gegen die Baseline berichtet** — auch wenn er sinkt.
- **Abnahme:** Kein `embed()`-Aufruf mehr im Code; Diary und Graph funktionieren; Recall@3-Delta
  ist dokumentiert.
- **Rollback:** Revert + `jarvis.db.pre-v2.bak`.
- **Abhängigkeiten:** Phase 3 (Provider für Summariser/Graph-Extraktion).
- **Parallel:** Retrieval-Umbau und Provenienz/Retention sind trennbar.
- **Nicht-Ziele:** kein Graph-Schema-Umbau, keine UI-Änderung.

---

### Phase 6 — Lokale Chat-Modelle vollständig entfernen · **L**

- **Ziel:** Es gibt keinen Codepfad, keine Konfiguration, keinen Test, keine Doku und keine
  Zeichenkette mehr, die ein lokales Chat-/Reasoning-/Embedding-Modell ansteuert.
- **Begründung:** Der ausdrückliche Kern der Vorgabe. Erst jetzt möglich, weil alles Ersetzende steht.
- **Betroffene Dateien:** die vollständige Liste aus §3.1–3.3 und §17.1.
- **Neu:** —
- **Entfernen:** `llm/ollama.py`, `llm/openai_compatible.py`, `llm/tiers.py`, `reply/planner.py`,
  `reply/evaluator.py`, `reply/compound_query.py`, `reply/prompts/model_variants.py`,
  `listening/intent_judge.py`, `utils/vram.py`, alle zugehörigen Tests, die Wizard-Seiten
  `ProviderChoice`/`OpenAICompatible`/`OllamaInstall`/`OllamaServer`/`Models`, die
  Settings-Kategorien `llm`/`llm_provider`, `_ollama_runtime_flags`, `_check_openai_compat_reachable`,
  `SUPPORTED_CHAT_MODELS`, `DEFAULT_CHAT_MODEL`, `DEFAULT_FAST_MODEL`, das Feature-Flag
  `execution_mode`.
- **Datenmigration:** Config v5 — `_legacy_local_llm` bleibt auf Platte (Downgrade-Pfad), wird aber
  nicht mehr gelesen.
- **Security:** Angriffsfläche schrumpft (kein lokaler HTTP-Client zu 127.0.0.1:11434 mehr).
- **Plattform:** `install_cuda.ps1`-Task umbenennen (nur noch STT).
- **Tests:** `test_no_local_llm_strings.py` von `xfail` auf `strict` schalten; Wizard- und
  Settings-Tests neu; Startup ohne Ollama auf dem System.
- **Evals:** vollständige neue Suite einmal komplett laufen lassen.
- **Abnahme:** Ollama deinstalliert-simuliert (PATH-Eintrag entfernt) → JARVIS startet und
  funktioniert vollständig; CI-Grep-Gate grün; README/EVALS.md/`llm_contexts.md` aktualisiert.
- **Rollback:** Revert (der letzte Punkt, an dem Rollback noch Code-Revert statt Flag ist).
- **Abhängigkeiten:** Phasen 3, 4, 5, 7 (Intent-Regeln müssen vorher stehen — siehe Reihenfolge-
  hinweis in §30).
- **Parallel:** Code-Entfernung, Wizard-Neubau, Doku-Sweep sind drei Pakete.
- **Nicht-Ziele:** **kein** Anfassen von Whisper, Piper, Chatterbox, CUDA.

---

### Phase 7 — Voice-Router und Isolationsgrenze · **M**

- **Ziel:** Zustandsmaschine aus Annex D, deterministische Intent-Regeln, harte Isolation der
  lokalen Sprachmodelle, Protokollierung von Route und Latenz pro Turn.
- **Begründung:** Muss **vor** Phase 6 fertig sein, sonst fehlt der Ersatz für den Intent-Judge.
- **Betroffene Dateien:** `listening/listener.py`, `listening/state_manager.py`, `output/tts.py`.
- **Neu:** `src/jarvis/voice/{router,intent_rules,safe_reply}.py`,
  `src/jarvis/voice/local/` (Whisper/Piper-Wrapper hinter der Grenze),
  `src/jarvis/voice/voice.spec.md`; `tests/test_voice_isolation.py`.
- **Entfernen:** LLM-Aufrufe aus dem Voice-Pfad (die Datei `intent_judge.py` selbst erst in Phase 6).
- **Datenmigration:** keine.
- **Security:** `SafeReply`-Typ; TTS akzeptiert nichts anderes; Import-Linter.
- **Plattform:** DirectSound-Gerätewahl unter Windows bleibt beachtet (bekanntes MME-Problem).
- **Tests:** Zustandsmaschine (alle Übergänge), Circuit Breaker, Fallback-Budget = 1,
  „ohne Cloud → statische Ansage, keine simulierte Antwort", Isolationstest.
- **Evals:** `voice/`-Suite gegen die Phase-0-Intent-Baseline; Zielkorridor ≥ 85 % der Baseline.
- **Abnahme:** Netzwerk trennen → JARVIS transkribiert, sagt die statische Fehlermeldung, erfindet
  nichts; Route und Latenz sind pro Turn in der UI sichtbar.
- **Rollback:** Revert.
- **Abhängigkeiten:** Phase 3.
- **Parallel:** Intent-Regeln und Router/Isolation sind zwei Pakete.
- **Nicht-Ziele:** kein Cloud-STT/TTS (siehe E-1), keine Dictation-Änderung.

---

### Phase 8 — Hermes als 24/7-Schicht · **L**

- **Ziel:** Gateway überwacht, Cron über `hermes cron` verwaltbar, Ergebnisse werden zugestellt.
- **Begründung:** Der Automationsteil des Produktziels; unabhängig vom Sprachdialog.
- **Betroffene Dateien:** `providers/hermes.py`.
- **Neu:** `src/jarvis/hermes/{gateway,cron_bridge,delivery,blueprints}.py`,
  `src/jarvis/hermes/hermes.spec.md`, Task-Scheduler-Anlage im Installer.
- **Entfernen:** nichts.
- **Datenmigration:** `cron_jobs`-Spiegeltabelle.
- **Security:** Job-Capability-Profile, `max_child_jobs=0`, Overlap-Sperre, Laufzeit-/Ausgabelimits,
  Kill Switch, Approval-Gates, Taint-Regel für externe Inhalte.
- **Plattform:** Windows Task Scheduler („At log on", Nutzerkontext); macOS `SMAppService` (Stufe B).
- **Tests:** DST-Grenzfälle (Frühjahr/Herbst), Missed-Run-Policies, Overlap, Retry-Backoff,
  Gateway-Neustart-Backoff, Kill Switch stoppt alles, rekursive Job-Erzeugung wird abgelehnt.
- **Evals:** Blueprint-Trockenlauf je Workflow aus §8.6.
- **Abnahme:** Morgenbriefing läuft nach Neustart des Rechners; ein pausierter Job läuft nicht;
  `needs_you` erzeugt eine Notification.
- **Rollback:** Revert + `hermes cron remove` der von JARVIS angelegten Jobs (ID-Präfix `jarvis-`).
- **Abhängigkeiten:** Phasen 1, 2, 4.
- **Parallel:** Gateway-Lifecycle, CronBridge und Delivery sind drei Pakete.
- **Nicht-Ziele:** kein eigener Scheduler, keine WhatsApp-/Slack-Kanäle in dieser Phase.

---

### Phase 9 — Operator-UI · **XL**

- **Ziel:** Sessions, Runs, Projekte, Specs, Approvals, Usage, Verbindungen, Audit als Oberfläche.
- **Begründung:** Macht den Umbau bedienbar; bis hierher ist alles nur API.
- **Betroffene Dateien:** `desktop_app/*`.
- **Neu:** `src/desktop_app/panels/{sessions,runs,projects,specs,approvals,usage,connections,audit}.py`,
  `src/jarvis/operator/{project_registry,spec_manager,usage_collector}.py`,
  `docs/operator-requirements.md` wird hier umgesetzt (Clean-Room, §26.2).
- **Entfernen:** nichts.
- **Datenmigration:** `projects`, `specs`, `usage_snapshots`.
- **Security:** Approval-UI ist der einzige Weg, Capabilities zu erweitern; hashgebundene
  Spec-/Plan-Freigaben (`approved_hash` muss zum aktuellen `content_hash` passen, sonst erlischt
  die Freigabe).
- **Plattform:** Editor/Terminal/Browser öffnen über `ProcessLauncher`-Port.
- **Tests:** Panel-Smoke, hashgebundene Freigabe erlischt bei Dateiänderung, fremde Sessions sind
  read-only, Usage zeigt „unbekannt" statt Null.
- **Evals:** keine.
- **Abnahme:** Ein Codex-Run lässt sich aus der UI starten, live verfolgen, steuern, abbrechen,
  fortsetzen und forken; Usage zeigt Codex-Rate-Limits echt und Claude ehrlich als unbekannt.
- **Rollback:** Revert.
- **Abhängigkeiten:** Phasen 2, 3, 4, 8.
- **Parallel:** jedes Panel ist ein eigenes Arbeitspaket (8 Pakete).
- **Nicht-Ziele:** kein Web-Client, keine Mobile-Ansicht.

---

### Phase 10 — Windows-Härtung, Packaging, Signierung · **M**

- **Ziel:** Signierter Installer, Autostart für JARVIS + Hermes, Upgrade-/Rollbacktests,
  `RegisterHotKey`, ConPTY, Windows-Notifications, Screen-Capture-Pfad.
- **Betroffene Dateien:** `installer/windows/*`, `.github/workflows/build-desktop.yml`,
  `release.yml`, `desktop_app/platform_win.py`.
- **Neu:** Signierschritt in CI, `docs/windows.md`.
- **Entfernen:** Ollama-Erwähnungen aus Release-Texten.
- **Datenmigration:** Abschluss der Pfadmigration (§16.3), alter Pfad nach Bestätigung entfernt.
- **Security:** Authenticode; Update-Hashprüfung; Credential-Manager-Nutzung.
- **Tests:** Upgrade alt→neu mit erhaltener Konfiguration; Rollback; Autostart nach Neustart;
  Smoke-Test des gebündelten Binaries.
- **Evals:** keine.
- **Abnahme:** Installer ist signiert, SmartScreen meckert nicht, Update funktioniert, Hermes-
  Gateway startet mit.
- **Rollback:** vorherigen Installer ausführen.
- **Abhängigkeiten:** Phasen 6, 8.
- **Parallel:** Signierung, Autostart, Plattform-APIs sind drei Pakete.
- **Nicht-Ziele:** kein macOS.

---

### Phase 11 — macOS Stufe A · **L**

- **Ziel:** Bestehende PyQt-App auf ARM64 lauffähig, signiert, notarisiert, als DMG.
- **Betroffene Dateien:** `build-desktop.yml`, `release.yml`, `desktop_app/paths.py`,
  `listening/listener.py` (MLX-Pfad).
- **Neu:** `docs/macos.md`, Entitlements-Plist, `platform_mac.py`.
- **Entfernen:** Chatterbox auf macOS deaktivieren (CUDA-abhängig).
- **Datenmigration:** Pfade nach `~/Library/Application Support/Jarvis`.
- **Security:** Hardened Runtime, Notarisierung, Keychain.
- **Tests:** Berechtigungsdiagnose, Update über `ditto`-Pfad, MLX-Whisper-Fallback.
- **Abnahme:** DMG installiert, App startet ohne Gatekeeper-Warnung, Sprachdialog läuft.
- **Rollback:** vorheriges DMG.
- **Abhängigkeiten:** Phasen 6, 7, 10.
- **Parallel:** Signierung/Notarisierung vs. Plattform-Ports.
- **Nicht-Ziele:** kein SwiftUI.

---

### Phase 12 — macOS Stufe B (SwiftUI) · **XL**

- **Ziel:** Native `MenuBarExtra`-App als zweiter Client der Core-API; Python-Core als signierter
  Helper.
- **Neu:** `clients/macos/` (Xcode-Projekt), `AVAudioEngine`, `ScreenCaptureKit`, `Vision`-OCR,
  `NSPasteboard`, `CGEvent`, `UserNotifications`, `SMAppService`, Keychain, Sparkle.
- **Datenmigration:** keine (gleiche DB, gleicher Core).
- **Security:** Helper-Signierung, XPC/HTTP-Token, Berechtigungsdiagnose.
- **Tests:** Client-Contract-Tests gegen die API; Berechtigungsflüsse.
- **Abnahme:** Beide Clients (PyQt Windows, SwiftUI macOS) laufen gegen denselben Core mit
  identischer Funktionalität; **keine** Fachlogik in Swift dupliziert.
- **Rollback:** Stufe-A-App bleibt parallel auslieferbar.
- **Abhängigkeiten:** Phasen 2, 9, 11.
- **Parallel:** UI-Panels in Swift sind unabhängig voneinander.
- **Nicht-Ziele:** keine Portierung der Fachlogik.

---

## 30. Abhängigkeiten zwischen den Phasen

```
P0 ──┬─▶ P1 ──┬────────────────▶ P3 ──┬──▶ P4 ──┬──▶ P6*
     └─▶ P2 ──┘                       │         │
                                      ├──▶ P5 ──┤
                                      └──▶ P7 ──┘
                     P1,P2,P4 ─────────────▶ P8 ──▶ P9
                                   P6,P8 ──▶ P10 ──▶ P11 ──▶ P12
```

**\* Wichtige Reihenfolge-Korrektur:** Phase 6 (Entfernen) darf erst starten, wenn **P4, P5 und P7**
fertig sind. Sonst verliert das Produkt zwischenzeitlich Tools, Memory-Suche oder Wake-Erkennung.
Das ist der häufigste Fehler bei solchen Umbauten und wird hier explizit ausgeschlossen.

---

## 31. Definition of Done pro Phase

Für **jede** Phase gilt zusätzlich zu den phasenspezifischen Abnahmekriterien:

1. Alle Tests grün, keine neuen `xfail` ohne Ticket.
2. Betroffene `*.spec.md` aktualisiert; `docs/llm_contexts.md` aktualisiert, wenn ein LLM-Kontext
   sich ändert (CLAUDE.md-Pflicht).
3. README aktualisiert, wenn nutzersichtbares Verhalten sich ändert.
4. Kein `TODO(PR X)`, kein Kompatibilitäts-Shim, keine halb konvertierten Aufrufstellen
   (CLAUDE.md: „Refactor completely or not at all").
5. Kein Text in Code/Doku, der Historie erzählt („früher", „migriert von", „jetzt nutzt").
6. Britisches Englisch im Code/Doku, keine Gedankenstriche in nutzersichtbaren Texten.
7. Conventional-Commit-Titel; PR-Beschreibung deckt den gesamten Changeset ab; `/review-pr` gelaufen.
8. Manuell gestartet und ausprobiert, nicht nur getestet.
9. Rollbackweg einmal tatsächlich durchgeführt (nicht nur beschrieben).

---

## 32. Konkrete Tickets und Arbeitspakete

| ID | Titel | Phase | Größe | Parallel-Gruppe |
|---|---|---|---|---|
| T-001 | Config-Migration v4 + `execution_mode`-Flag + Backup | 0 | S | A |
| T-002 | Baseline: Memory-Recall@3 messen und einfrieren | 0 | S | A |
| T-003 | Baseline: Voice-Intent 42 Fälle gegen Regeln messen | 0 | S | A |
| T-004 | CI-Wächter: Single-Writer, Import-Isolation, Local-LLM-Grep (xfail) | 0 | S | A |
| T-005 | `docs/operator-requirements.md` (Clean-Room-Spezifikation) | 0 | M | A |
| T-006 | `ProviderAdapter` ABC + Event-Modell + `providers.spec.md` | 1 | M | B |
| T-007 | `ClaudeAdapter` (stream-json, Session, Steering, Interrupt) | 1 | L | B |
| T-008 | `CodexAdapter` (app-server JSON-RPC v2, Thread/Turn/Model/Account) | 1 | L | B |
| T-009 | `HermesAdapter` (CLI, `-z`, Toolsets, Skills) | 1 | M | B |
| T-010 | `AuthenticationManager` + Statusparser aller drei | 1 | M | B |
| T-011 | `ModelCatalog` + verifizierte Claude-Probe (fail closed) | 1 | M | B |
| T-012 | `ProviderCapabilityRegistry` | 1 | S | B |
| T-013 | Golden-Stream-Kontrakttests je Adapter | 1 | M | B |
| T-014 | Core-API-Skelett (Loopback, Token, SSE, Origin/CSRF) | 2 | M | C |
| T-015 | `app.py` in sechs Module zerlegen | 2 | L | C |
| T-016 | Memory-Viewer → Core-API-Routen, Flask-Prozess entfernen | 2 | L | C |
| T-017 | `ports/` + Face-Widget-Import auflösen | 2 | M | C |
| T-018 | Windows-Pfadmigration (kopieren, reversibel) | 2 | M | C |
| T-019 | `RunManager` + `RunStore` + `run_events` | 3 | L | D |
| T-020 | Audit-Kette (append-only, Hash) | 3 | M | D |
| T-021 | Redaction als Pflicht-Middleware vor Provider-Aufruf | 3 | M | D |
| T-022 | Antwortpfad-Weiche `local`/`subscription` in `engine.py` | 3 | M | D |
| T-023 | Harter Abbruch inkl. Prozessbaum (Windows Job Objects) | 3 | S | D |
| T-024 | JARVIS-MCP-Server (11 Builtins + Memory-Tools) | 4 | L | E |
| T-025 | MCP-Verdrahtung je Provider (`--mcp-config`, `codex mcp`, `hermes mcp`) | 4 | M | E |
| T-026 | `CapabilityPolicy` + vier Profile | 4 | M | E |
| T-027 | Pfad-Denylist (OneDrive, Secrets) mit Windows-Normalisierung | 4 | M | E |
| T-028 | `TaintTracker` + Degradation der Seiteneffekte | 4 | M | E |
| T-029 | SSRF-Härtung: Redirect-Kette + DNS-Pinning | 4 | M | E |
| T-030 | `ApprovalManager` + `approvals`-Tabelle | 4 | M | E |
| T-031 | `memory/retrieval.py` (BM25 + Recency + Metadaten + Graph-Score) | 5 | L | F |
| T-032 | Provenienz-/Retention-Spalten + Migration | 5 | M | F |
| T-033 | Zweite FTS-Tabelle `unicode61` (Mehrsprachigkeit) | 5 | S | F |
| T-034 | Embedding-Pfade und FAISS entfernen, Daten auslagern | 5 | M | F |
| T-035 | Summariser/Graph-Extraktion auf `memory_provider` umstellen | 5 | M | F |
| T-036 | Export/Löschung/Retention-Sweep | 5 | M | F |
| T-037 | `voice/intent_rules.py` (deterministisch, sprachagnostisch) | 7 | L | G |
| T-038 | `VoiceRouter` + Zustandsmaschine + Circuit Breaker | 7 | M | G |
| T-039 | `SafeReply`-Typ + TTS-Grenze + Isolationstest | 7 | M | G |
| T-040 | Route-/Latenz-Telemetrie pro Turn + UI-Anzeige | 7 | S | G |
| T-041 | Lokale LLM-Module löschen (§17.1) | 6 | L | H |
| T-042 | Setup-Wizard neu: drei Provider-Logins statt Ollama-Kette | 6 | L | H |
| T-043 | Settings-Window: LLM-Kategorien ersetzen | 6 | M | H |
| T-044 | Doku-Sweep: README, `llm_contexts.md`, alle `*.spec.md`, EVALS.md | 6 | M | H |
| T-045 | Config v5 + Grep-Gate auf `strict` | 6 | S | H |
| T-046 | Hermes-Gateway-Lifecycle + Task Scheduler | 8 | M | I |
| T-047 | `CronBridge` auf `hermes cron` (CRUD, Pin, Limits) | 8 | L | I |
| T-048 | DST-/Missed-Run-/Overlap-Semantik + Tests | 8 | M | I |
| T-049 | `DeliveryRouter` (Desktop, TTS, `hermes send`, Datei) | 8 | M | I |
| T-050 | Blueprint-Katalog (8 Workflows aus §8.6) | 8 | M | I |
| T-051 | Kill Switch + globale Automations-Sperre | 8 | S | I |
| T-052 | Panel: Sessions + Inbox (`needs_you`) | 9 | L | J |
| T-053 | Panel: Runs (Journal, Cancel/Retry/Resume/Fork) | 9 | L | J |
| T-054 | Panel: Projekte + Repo-Suche + sichere Dateiansicht | 9 | L | J |
| T-055 | Panel: Specs, Plan-Review, hashgebundene Freigaben | 9 | L | J |
| T-056 | Panel: Approvals + Audit-Log | 9 | M | J |
| T-057 | Panel: Usage (mit ehrlichem „unbekannt") | 9 | M | J |
| T-058 | Panel: Verbindungen (Provider, MCP, Skills, Health) | 9 | M | J |
| T-059 | Sprachausgabe: Priorisierung, satzweise, Repeat ohne LLM-Turn | 9 | M | J |
| T-060 | Windows-Signierung in CI | 10 | M | K |
| T-061 | `RegisterHotKey`, ConPTY, Toasts, Screen-Capture | 10 | L | K |
| T-062 | Upgrade-/Rollback-Tests + Update-Hashprüfung | 10 | M | K |
| T-063 | macOS Signierung, Notarisierung, DMG | 11 | L | L |
| T-064 | macOS Berechtigungsdiagnose + MLX-Fallback | 11 | M | L |
| T-065 | SwiftUI-Client Grundgerüst + API-Anbindung | 12 | XL | M |
| T-066 | SwiftUI-Plattformdienste (Audio, Capture, OCR, Hotkey) | 12 | XL | M |
| T-067 | Python-Core als signierter macOS-Helper | 12 | L | M |
| T-068 | Sparkle-Updater | 12 | M | M |

---

## 33. Geschätzte Komplexität pro Ticket

Siehe Spalte „Größe" in §32. Verteilung: **S** 12 · **M** 32 · **L** 20 · **XL** 4.
Gesamtaufwand grob: 5–7 Monate für eine Person mit Agentenunterstützung; die Phasen 0–7 (das
eigentliche Produktziel) liegen bei etwa 3–4 Monaten.

---

## 34. Parallelisierungsmöglichkeiten

| Gruppe | Tickets | Gleichzeitig möglich |
|---|---|---|
| A | T-001…T-005 | 3 Agenten |
| B | T-006…T-013 | 3 Agenten (je ein Adapter), danach 2 |
| C | T-014…T-018 | 3 Agenten |
| D | T-019…T-023 | 2 Agenten |
| E | T-024…T-030 | 2 Agenten (MCP vs. Security) |
| F | T-031…T-036 | 2 Agenten |
| G | T-037…T-040 | 2 Agenten |
| H | T-041…T-045 | 3 Agenten (Code / Wizard / Doku) |
| I | T-046…T-051 | 3 Agenten |
| J | T-052…T-059 | bis 8 Agenten (Panels sind unabhängig) |
| K–M | T-060…T-068 | 2–3 Agenten |

Regel für parallele Agenten: **ein Agent pro Datei.** `engine.py`, `listener.py`, `app.py` und
`setup_wizard.py` sind Engpässe und dürfen nie von zwei Paketen gleichzeitig angefasst werden.

---

## 35. Kritischer Pfad

```
T-001 → T-006 → T-007/T-008 → T-013 → T-014 → T-019 → T-022 → T-024 → T-026
      → T-031 → T-037 → T-038 → T-041 → T-044 → T-046 → T-052 → T-060
```

Kürzeste Kette bis „funktional cloudbasiert, lokale Modelle raus":
**T-001 → T-006 → T-007 → T-013 → T-014 → T-019 → T-022 → T-024 → T-031 → T-037 → T-041.**
Alles andere kann darum herum parallelisiert werden. Die zwei riskantesten Glieder sind **T-007/
T-008** (fremder CLI-Vertrag) und **T-037** (Ersatz für den Intent-Judge).

---

## 36. Offene Entscheidungen

| # | Entscheidung | Empfehlung | Blockiert |
|---|---|---|---|
| **E-1** | STT/TTS: Cloud primär (Vorgabe) oder lokal primär (Analyse §9.1)? | **lokal primär** — Cloud-STT bräuchte einen vierten Provider, den die Vorgabe verbietet | P7 |
| **E-2** | Welcher Provider ist Default für den Sprachdialog? | **Hermes** (schnellste Runde, eigenes Gateway); Claude für Planung, Codex für Code | P3 |
| **E-3** | Auto-Fallback zwischen Providern bei Quota? | **aus** per Default, opt-in mit sichtbarem Hinweis | P3 |
| **E-4** | Wie tief darf ein Sprach-Run Seiteneffekte haben? | Default `read_only`; Schreiben nur nach Approval | P4 |
| **E-5** | Wird `memory_provider` fest verdrahtet oder wählbar? | wählbar, Default `hermes` (quotenschonend) | P5 |
| **E-6** | Recall-Verlust nach Embedding-Wegfall akzeptabel? | T-002 gemessen (`docs/baselines/memory_recall_baseline.json`): Hybrid 33,3 %, FTS-only 77,8 % Recall@3. Der heutige Blend ist **schlechter** als sein eigener FTS-Zweig, weil ein FTS-Treffer als `1/(1+bm25)` gewertet wird und `bm25()` negativ ist. Der Embedding-Wegfall kostet nach dieser Messung nichts; T-031 misst gegen `fts_only` | P5 |
| **E-7** | Chatterbox behalten (Voice-Cloning) oder entfernen? | **behalten**, aber nur Windows/CUDA; auf macOS deaktiviert. Es ist reines TTS und verletzt die Vorgabe nicht | P7 |
| **E-8** | Wird der `pytesseract`-OCR als „lokales Modell" gewertet? | **nein** — deterministische Zeichenerkennung, keine Entscheidungsinstanz. Bleibt | P4 |
| **E-9** | Erlaubt die „Jarvis AI Assistant License" den Umbau und eine spätere Weitergabe? | **muss vor P1 geklärt werden** — private Nutzung unkritisch, Veröffentlichung nicht | P1 (Veröffentlichung) |
| **E-10** | Projektname und Positionierung („100 % lokal" ist tot) | Neupositionierung als „Sprach-Frontend für deine Abos, Daten bleiben bei dir" | P6 (README) |
| **E-11** | Wird Hermes als Abhängigkeit vorausgesetzt oder optional? | **optional aber empfohlen**; ohne Hermes keine 24/7-Schicht, JARVIS läuft trotzdem | P8 |
| **E-12** | Alter Ollama-Bestand: bleibt liegen oder wird angeboten zu löschen? | liegen lassen, weil Hermes ihn als Fallback nutzt (§17.3) | P6 |

---

# Annexe

## Annex A — Provider-Capability-Matrix

| Fähigkeit | Claude (CLI 2.1.220) | Codex (app-server 0.153.4) | Hermes (lokal) |
|---|---|---|---|
| Abo-Auth | ✅ `claude.ai`, Pro (verifiziert) | ✅ ChatGPT (verifiziert) | ✅ via openai-codex |
| Auth-Status maschinenlesbar | ✅ JSON | ✅ `codex doctor` / `Account/read` | ⚠️ Textausgabe |
| Modell-Liste dynamisch | ❌ **keine Schnittstelle** | ✅ `Model/list` | ✅ `hermes model --refresh` |
| Modell-Fähigkeiten abfragbar | ❌ | ✅ `ModelProvider/capabilities/read` | ⚠️ teilweise |
| Modell pro Run pinnbar | ✅ `--model` | ✅ `Turn/start` | ✅ `-m` |
| Streaming | ✅ `stream-json` + Partials | ✅ Notifications | ✅ |
| Mid-Run-Steering | ✅ stdin `stream-json` | ✅ `Turn/steer` | ⚠️ nur über Gateway |
| Interrupt | ✅ Signal/Stream | ✅ `Turn/interrupt` | ⚠️ Prozess |
| Resume | ✅ `--resume` | ✅ `Thread/resume` | ✅ `--resume` |
| Fork | ⚠️ über Resume+neue ID | ✅ `Thread/fork` | ✅ `--continue` |
| Session-Liste | ✅ `agents --json` | ✅ `Thread/list` | ✅ `hermes sessions` |
| Eigene Tools via MCP | ✅ `--mcp-config` | ✅ `codex mcp` | ✅ `hermes mcp` |
| Tool-Berechtigungen | ✅ `--permission-mode`, allow/deny | ✅ `PermissionProfile/list` | ✅ `approvals`, `--yolo` |
| Struktur-Output | ✅ `--json-schema` | ✅ `--output-schema` | ⚠️ |
| Usage / Rate-Limits | ❌ **nicht verfügbar** | ✅ `Account/{usage,rateLimits}/read` | ⚠️ `hermes status` |
| Bilder als Eingabe | ✅ | ✅ `-i` | ✅ `image_input_mode` |
| Embeddings | ❌ | ❌ | ❌ |
| Cron / Zeitplan | ❌ | ❌ | ✅ `hermes cron` |
| Zustellkanäle | ❌ | ❌ | ✅ `hermes send` |
| Hintergrundlauf | ✅ `--bg` | ✅ daemon | ✅ gateway |
| Code-Review | ✅ `ultrareview` | ✅ `Review/start` | ⚠️ |
| Skills | ✅ | ✅ `Skills/list` | ✅ `--skills` |

## Annex B — Feature-Paritätsmatrix alt ↔ neu

| Feature | heute | nachher | Bewertung |
|---|---|---|---|
| Wake-Word-Dialog | lokales LLM + Regeln | nur Regeln | ⚠️ **schlechter** |
| Antwortqualität | gemma4:e2b / gpt-oss:20b | Claude/Codex/Hermes | ✅ **deutlich besser** |
| Antwortlatenz | ~4,5 s | 6–20 s | ⚠️ **schlechter** |
| Tool-Nutzung | eigener Loop + Router-LLM | Provider-Loop über MCP | ✅ besser |
| Mehrschrittige Aufgaben | Planner (5 Schritte) | echter Agent | ✅ deutlich besser |
| Memory-Suche | Embedding+FTS hybrid | FTS5/BM25+Metadaten | ⚠️ **schlechter** |
| Memory-Verdichtung | lokales LLM je Sitzung | Cloud, gebündelt, mit Provenienz | ✅ besser |
| Dictation | lokal Whisper | unverändert | ➡️ gleich |
| TTS | Piper/Chatterbox | unverändert (+Priorisierung) | ✅ leicht besser |
| Offline-Betrieb | ✅ vollständig | ❌ nur Transkription + Fehleransage | ⚠️ **verloren** |
| Datenschutz | alles lokal | Prompts gehen zu Anbietern | ⚠️ **verloren** |
| Kosten | 0 € (Strom) | Abos | ➡️ vorhanden |
| Automation 24/7 | ❌ | ✅ Hermes-Cron | ✅ **neu** |
| Session-/Run-Verwaltung | ❌ | ✅ Operator | ✅ **neu** |
| Approvals / Audit | ❌ | ✅ | ✅ **neu** |
| Usage-Transparenz | n/a | ✅ (Codex/Hermes), ⚠️ Claude | ✅ neu |
| Projekt-/Spec-Workflow | ❌ | ✅ | ✅ **neu** |
| Setup-Aufwand | Ollama + 2 Modelle laden | drei CLI-Logins | ✅ einfacher |
| Hardwarebedarf | 8–24 GB VRAM | GPU nur für Whisper | ✅ deutlich geringer |

## Annex C — Lokale-Modell-Entfernungsmatrix

| Kategorie | Fundstellen | Aktion | Phase |
|---|---|---|---|
| Chatmodell | `llm/ollama.py`, `llm/openai_compatible.py`, `llm/factory.py`, `llm/tiers.py` | löschen/ersetzen | 6 |
| Fast-Modell | `config.fast_model`, `tiers.Tier.FAST`, 7 Aufrufstellen | löschen | 6 |
| Reasoning-Modell | `llm_thinking_enabled`, `intent_judge_thinking_enabled`, `dictation_thinking_enabled` | löschen | 6 |
| Planner-Modell | `reply/planner.py` (851 Z.) + Spec + Tests | löschen | 6 |
| Evaluator-Modell | `reply/evaluator.py` (410 Z.) + Spec + Tests | löschen | 6 |
| Intent-LLM | `listening/intent_judge.py` (539 Z.) | ersetzen durch Regeln | 7→6 |
| Tool-Router-LLM | `tools/selection.py:260-437` | ersetzen durch Capability-Filter | 4→6 |
| Memory-Summariser | `memory/conversation.py` (3 Pässe) | auf Cloud-Provider | 5 |
| Knowledge-Extraktion | `memory/graph_ops.py` (4 Pässe) | auf Cloud-Provider | 5 |
| Embedding-Modell | 6 `embed()`-Stellen, `nomic-embed-text`, 768-Dim | löschen | 5 |
| Vektor-Speicher | `utils/vector_store.py`, `utils/fast_vector_store.py`, sqlite-vss, faiss-cpu | löschen + Daten auslagern | 5 |
| Multimodal | `tools/builtin/screenshot.py` an lokales Modell | an Cloud-Provider, nach Approval | 4 |
| Modell-Download | `setup_wizard.ModelsPage`, `ollama pull`, `get_required_models` | löschen | 6 |
| Modell-Verwaltung | `SUPPORTED_CHAT_MODELS`, `utils/vram.py`, VRAM-Budget | löschen | 6 |
| Wizard-Seiten | 5 Seiten (`ProviderChoice`, `OpenAICompatible`, `OllamaInstall`, `OllamaServer`, `Models`) | ersetzen durch 3 Provider-Logins | 6 |
| Settings | Kategorien `llm`, `llm_provider` | ersetzen | 6 |
| Env-Variablen | keine Ollama-Env gefunden; `JARVIS_WHISPER_BACKEND` bleibt | — | — |
| Build-Hooks | `build-desktop.yml` faiss/chatterbox-Filter | anpassen | 6/10 |
| CI | Grep-Gate neu | ergänzen | 6 |
| Tests | 24 Dateien (§17.1) | löschen/ersetzen | 6 |
| Doku | README, `llm_contexts.md`, 6 `*.spec.md`, EVALS.md, `release.yml` | umschreiben | 6 |
| Telemetrie/Fehlertexte | „Ollama not running", „Model not supported", Splash-Texte | umschreiben | 6 |
| Provider-Aliase | `ollama_*`-Konfigschlüssel | nach `_legacy_local_llm` | 0/6 |
| Cache-Verzeichnisse | nur `legacy_vectors/`, `*.faiss` sind JARVIS-eigen | bestätigungspflichtig löschen | 6 |
| **Nicht entfernen** | Ollama-Installation, `~/.ollama/models`, LM Studio, HF-Cache | unangetastet | — |

## Annex D — Voice-Fallback-Zustandsmaschine

```
                    ┌──────────┐
                    │   IDLE   │◀─────────────────────────────┐
                    └────┬─────┘                              │
              Sprache + VAD                                   │
                    ┌────▼──────────┐                         │
                    │   CAPTURING   │  Barge-in bricht TTS ab │
                    └────┬──────────┘                         │
                 Endpoint erkannt                             │
                    ┌────▼──────────┐                         │
                    │  STT_ROUTING  │                         │
                    └────┬──────────┘                         │
       ┌─────────────────┴───────────────┐                    │
  [local_first]                    [cloud_first, E-1]         │
       ▼                                 ▼                    │
  STT_LOCAL ──Fehler──▶ STT_CLOUD   STT_CLOUD ──T/O,429,down──▶ STT_LOCAL
       │                                 │      (Budget 1)     │
       └──────────────┬──────────────────┘                     │
                      ▼                                        │
              ┌───────────────┐  leer/zu kurz/Echo             │
              │ INTENT_RULES  │────────────────────────────────┤
              └───────┬───────┘  (deterministisch, kein LLM)   │
                 gerichtet                                     │
              ┌───────▼───────┐                                │
              │ PROVIDER_RUN  │ Provider+Modell gepinnt        │
              └───────┬───────┘                                │
     ┌────────────────┼──────────────────┬─────────────┐       │
   ok │           quota│            error│      needs_you      │
     ▼                ▼                  ▼             ▼       │
 SAFE_REPLY   STATIC_QUOTA_MSG   STATIC_ERROR_MSG  ASK_USER    │
     │                │                  │             │       │
     └────────────────┴───────┬──────────┴─────────────┘       │
                       ┌──────▼──────┐                         │
                       │ TTS_ROUTING │                         │
                       └──────┬──────┘                         │
                  [cloud verfügbar?]                           │
                  ja ▼            nein ▼                       │
              TTS_CLOUD ──Fehler──▶ TTS_LOCAL (Piper)          │
                  └──────────┬──────────┘                      │
                       ┌─────▼─────┐                           │
                       │ SPEAKING  │──Ende / Stop / Barge-in───┘
                       └───────────┘
```

**Invarianten:**
- `PROVIDER_RUN` ist der **einzige** Zustand, der eine intelligente Antwort erzeugen darf.
- `STATIC_*`-Zustände sprechen vorformulierte Sätze aus einer Ressourcendatei — nie generiert.
- `fallback_budget = 1` pro Turn und pro Route. Danach `STATIC_ERROR_MSG`.
- Circuit Breaker: 3 Fehler / 60 s → Route 5 min `open`, dann ein Half-Open-Probe.
- Jeder Turn schreibt: `provider, model, stt_route, tts_route, latency{stt, first_token, total, tts}, fallback_reason`.

## Annex E — Hermes-Cron-Zustandsmaschine

```
   DRAFT ──create──▶ SCHEDULED ◀──resume── PAUSED
                        │  ▲                  ▲
              Trigger   │  │ finish           │ pause
                        ▼  │                  │
                     DUE ──┴──▶ ELIGIBLE ─────┘
                                  │
              ┌───────────────────┼──────────────────┐
      overlap=skip      approval nötig          frei
              ▼                   ▼                  ▼
          SKIPPED           AWAITING_APPROVAL ───▶ RUNNING
                                  │ timeout           │
                                  ▼                   │
                              EXPIRED      ┌──────────┼─────────┬────────┐
                                           ▼          ▼         ▼        ▼
                                       SUCCESS     FAILED   TIMEOUT   QUOTA
                                           │          │         │        │
                                           │      retry<max?    │     kein Retry
                                           │          ▼         ▼        │
                                           │      BACKOFF ──▶ ELIGIBLE   │
                                           └──────────┴─────────┴────────┘
                                                      ▼
                                                  DELIVERED ──▶ SCHEDULED
```

Rechner war aus → `MISSED`. Auflösung nach `missed_run_policy`:
`skip` → verwerfen · `run_once_on_wake` → einmal nachholen · `run_all` → alle nachholen
(gedeckelt auf `max_catchup`, Default 3).

## Annex F — Datenflussdiagramm

```
🎤 Mikro ──▶ VAD/Wake (deterministisch) ──▶ Whisper (lokal, isoliert) ──▶ str
                                                                          │
                                                              redact() ◀──┘   ← PFLICHT
                                                                  │
        Memory-Kontext (FTS5, nur `user`-Provenienz) ─────────────┤
        Zeit + Ort (GeoLite2 lokal) ──────────────────────────────┤
                                                                  ▼
                                              ┌──────────── ProviderAdapter ────────────┐
                                              │ claude -p │ codex app-server │ hermes   │
                                              └────┬──────────────┬─────────────┬───────┘
                                                   │              │             │
                                        MCP ◀──────┴──────────────┴─────────────┘
                                         │
                             jarvis-MCP-Server (Policy-Gate + Taint)
                                         │
                    ┌────────────────────┼────────────────────┐
                    ▼                    ▼                    ▼
              Memory (SQLite)      Web/Dateien (tainted)   externe MCP
                                         │
                                    Fence + Taint
                                         │
                                         ▼
                                  RunEvents ──▶ RunStore + Audit
                                         │
                                    SafeReply
                                         │
                            ┌────────────┴────────────┐
                            ▼                         ▼
                     Piper/TTS (isoliert)        UI (SSE)
```

## Annex G — Trust-Boundary-Diagramm

```
╔══════════════════ Z0 NUTZER (vertraut) ═══════════════════╗
║  Mikro · Tastatur · UI-Eingaben · Approvals               ║
╚═══════════════════════════╤═══════════════════════════════╝
                            │
╔═══════════════════════════▼═══ Z1 jarvis-core (durchsetzend) ═══╗
║  API-Token · CapabilityPolicy · TaintTracker · Redaction        ║
║  Audit (append-only) · einziger DB-Writer                       ║
╟─────────────┬──────────────────┬───────────────────────────────╢
║             │                  │                               ║
║  ╔══════════▼═══════╗   ╔══════▼═════════════════════════════╗ ║
║  ║ Z2 Voice-lokal   ║   ║ Z3 Provider-Prozesse               ║ ║
║  ║ Whisper · Piper  ║   ║ claude · codex · hermes            ║ ║
║  ║ KEINE Rechte     ║   ║ halbvertraut: führen Modell-Output ║ ║
║  ║ nur str/SafeReply║   ║ aus → Policy + Approval + Sandbox  ║ ║
║  ╚══════════════════╝   ╚══════╤═════════════════════════════╝ ║
╚════════════════════════════════╪════════════════════════════════╝
                                 │
              ╔══════════════════▼══════════════════╗
              ║ Z4 MCP-Server (pro Server erlaubt)  ║
              ╚══════════════════╤══════════════════╝
                                 │
              ╔══════════════════▼══════════════════╗
              ║ Z5 Web · Mail · Repo · Nachrichten  ║
              ║ UNTRUSTED — immer tainted, gefenced ║
              ╚═════════════════════════════════════╝

Z6 Dateisystem außerhalb Workspace: Denylist (OneDrive absolut), Approval für NAS
```

## Annex H — Komponenten- und IPC-Diagramm

```
 PyQt-Client (Win)          SwiftUI-Client (macOS)
        │  HTTP+SSE 127.0.0.1:<rnd>, Bearer   │
        └──────────────┬───────────────────────┘
                       ▼
              ┌─────────────────┐
              │  api/server     │
              └────────┬────────┘
                       ▼
              ┌─────────────────┐     ┌──────────────┐
              │ runtime/EventBus│◀───▶│ operator/*   │
              └────────┬────────┘     └──────────────┘
                       ▼
              ┌─────────────────┐
              │ RunManager      │
              └───┬────┬────┬───┘
        stdio     │    │    │   CLI/HTTP
   ┌──────────────┘    │    └───────────────┐
   ▼                   ▼                    ▼
claude -p        codex app-server      hermes gateway
(stream-json)    (JSON-RPC v2)         + hermes cron
   │                   │                    │
   └───────── MCP stdio ┴────────────────────┘
                       ▼
             tools/server (jarvis-MCP)
                       ▼
             security/policy → memory/ · tools/builtin/
                       ▼
               SQLite (ein Writer)
```

## Annex I — Datenbanktabellenübersicht

| Tabelle | Status | Zweck |
|---|---|---|
| `meals` | bleibt | Ernährungslog |
| `conversation_summaries` | erweitert | Tagebuch + Provenienz + Retention |
| `summaries_fts` | bleibt | FTS5, Porter |
| `summaries_fts_uni` | **neu** | FTS5, unicode61 (mehrsprachig) |
| `memory_nodes` | erweitert | Wissensgraph + Provenienz |
| `embeddings`, `summary_vec` | **entfernt** | ausgelagert nach `jarvis.db.embeddings.bak` |
| `runs` | **neu** | Run-Metadaten inkl. Provider/Modell/Profil |
| `run_events` | **neu** | normalisierter Event-Strom |
| `audit_events` | **neu** | append-only, Hash-Kette |
| `approvals` | **neu** | Freigaben inkl. `artifact_hash` |
| `projects` | **neu** | Projekt-Registry |
| `specs` | **neu** | Spec-Workflow + hashgebundene Freigabe |
| `cron_jobs` | **neu** | Spiegel von `hermes cron` |
| `usage_snapshots` | **neu** | Quota/Usage-Verlauf mit `available`-Flag |

## Annex J — Reihenfolge der ersten 25 umsetzbaren Tickets

| # | Ticket | Phase | Größe |
|---|---|---|---|
| 1 | T-001 Config v4 + `execution_mode` + Backup | 0 | S |
| 2 | T-004 CI-Wächter (xfail) | 0 | S |
| 3 | T-002 Memory-Recall-Baseline | 0 | S |
| 4 | T-003 Voice-Intent-Baseline | 0 | S |
| 5 | T-005 `operator-requirements.md` (Clean-Room) | 0 | M |
| 6 | T-006 `ProviderAdapter` + Event-Modell | 1 | M |
| 7 | T-010 `AuthenticationManager` | 1 | M |
| 8 | T-008 `CodexAdapter` (app-server) | 1 | L |
| 9 | T-007 `ClaudeAdapter` (stream-json) | 1 | L |
| 10 | T-009 `HermesAdapter` | 1 | M |
| 11 | T-011 `ModelCatalog` + Claude-Probe fail-closed | 1 | M |
| 12 | T-012 `CapabilityRegistry` | 1 | S |
| 13 | T-013 Golden-Kontrakttests | 1 | M |
| 14 | T-014 Core-API-Skelett | 2 | M |
| 15 | T-017 `ports/` + Face-Import auflösen | 2 | M |
| 16 | T-015 `app.py` zerlegen | 2 | L |
| 17 | T-016 Memory-Viewer → API | 2 | L |
| 18 | T-018 Windows-Pfadmigration | 2 | M |
| 19 | T-019 `RunManager` + `RunStore` | 3 | L |
| 20 | T-021 Redaction-Pflicht-Middleware | 3 | M |
| 21 | T-020 Audit-Kette | 3 | M |
| 22 | T-022 Antwortpfad-Weiche | 3 | M |
| 23 | T-023 harter Abbruch (Prozessbaum) | 3 | S |
| 24 | T-024 jarvis-MCP-Server | 4 | L |
| 25 | T-026 `CapabilityPolicy` + Profile | 4 | M |

## Annex K — Die fünf gefährlichsten Migrationsfehler

1. **Zu früh löschen.** Wer `intent_judge.py`, `planner.py` oder die Embedding-Suche entfernt,
   bevor der Ersatz steht, hat ein Produkt, das schlechter ist als beides. Deshalb liegt Phase 6
   hinter 4, 5 und 7 — und deshalb ist das Feature-Flag bis dahin Pflicht.
2. **Redaction vergessen.** Heute läuft `redact()` vor dem *Speichern*. Ab dem Moment, in dem
   Prompts das Gerät verlassen, muss sie vor dem *Senden* laufen. Ein einziger Pfad ohne
   Redaction schickt Passwörter, Tokens und Klarnamen an einen Anbieter. Das ist der einzige
   Fehler in dieser Liste, der nicht rückholbar ist.
3. **Den Doppel-Writer mitschleppen.** Fünf `GraphMemoryStore`-Konstruktionen und ein zweiter
   Flask-Prozess auf derselben SQLite-Datei sind heute schon ein Korruptionsrisiko. Mit
   nebenläufigen Cron-Runs ab Phase 8 wird daraus ein Dauerproblem. Deshalb Single-Writer in
   Phase 2, nicht später.
4. **Sich auf CLI-Ausgaben verlassen, die kein Vertrag sind.** `claude` und `codex` sind fremde
   Produkte mit wöchentlichen Updates. Wer ihre Textausgabe parst statt der JSON-Modi, hat bei
   jedem Update einen stillen Ausfall. Nur `--output-format json/stream-json`, `--json`,
   `app-server`-JSON-RPC — und Golden-File-Tests, die brechen, bevor der Nutzer es merkt.
5. **Stille Fallbacks einbauen.** Ein automatischer Providerwechsel bei Quota, ein
   API-Key-Fallback, ein „wir nehmen halt das lokale Modell, wenn die Cloud weg ist" — jedes
   davon macht Kosten, Datenflüsse und Antwortqualität unvorhersehbar und verletzt die Vorgabe
   direkt. Alle Fehlerpfade enden in einem sichtbaren Zustand, nie in einer Ausweichhandlung.

## Annex L — Phase-1-Implementierungsprompt für den nächsten Coding-Agenten

> Kopiervorlage. Voraussetzung: Phase 0 (T-001…T-005) ist gemerged.

```
Du implementierst Phase 1 des Umbaus in docs/masterplan-subscription-rebuild.md
(Repo: JARVIS, Branch von `develop`, Konventionen aus CLAUDE.md gelten vollständig).

ZIEL
Ein neues Package `src/jarvis/providers/`, das Claude Code, Codex und Hermes als
Abo-Provider ansprechbar macht. Rein additiv: kein bestehender Codepfad wird
verändert, kein bestehender Test darf sich ändern, nichts wird gelöscht.

ZWINGENDE VORGABEN
- Kein Anthropic- oder OpenAI-API-Key. Vor jedem Prozessstart werden
  ANTHROPIC_API_KEY und OPENAI_API_KEY aus der Umgebung entfernt. Nach dem Start
  wird geprüft, dass der Auth-Modus `claude.ai` bzw. `chatgpt` ist; sonst Abbruch.
- Keine erfundenen Modell-Listen. Für Claude existiert keine Listing-Schnittstelle:
  der Katalog startet leer und füllt sich nur durch (a) verifizierte 1-Token-Probes
  eines vom Nutzer eingegebenen Namens oder (b) das im init-Event gemeldete Modell.
- Keine Textausgabe parsen, wo ein JSON-Modus existiert.
- Alle Fehler werden zu einem sichtbaren Zustand, nie zu einem Ausweichverhalten.
- Britisches Englisch in Code und Doku. Emojis in nutzersichtbarer CLI-Ausgabe.
- debug_log() an den Entscheidungspunkten, sparsam.
- TDD: erst der fehlschlagende Test, dann die Implementierung.

ZU BAUEN
src/jarvis/providers/
  base.py          ProviderAdapter (ABC) + RunSpec, RunHandle, RunEvent, AuthStatus,
                   ModelInfo, Capabilities, UsageSnapshot, HealthReport
  claude.py        ClaudeAdapter
  codex.py         CodexAdapter
  hermes.py        HermesAdapter
  auth.py          AuthenticationManager (60-s-Cache, fail closed)
  models.py        ModelCatalog (source: api|verified-probe|provider-default|unknown)
  capabilities.py  ProviderCapabilityRegistry
  registry.py      get_provider(id) / list_providers()
  cli.py           `python -m jarvis.providers.cli status` (Diagnose)
  providers.spec.md

VERIFIZIERTE SCHNITTSTELLEN (nicht neu recherchieren, aber vor Gebrauch einmal
gegen die installierte Version prüfen und bei Abweichung stoppen und fragen):

Claude Code 2.1.220
  Auth:      `claude auth status`  -> JSON {loggedIn, authMethod, apiProvider,
                                            email, orgId, subscriptionType}
  Run:       `claude -p --output-format stream-json --input-format stream-json
              --include-partial-messages --session-id <uuid> [--model <m>]
              [--mcp-config <file> --strict-mcp-config]
              [--permission-mode <mode>] [--allowedTools ...] [--add-dir ...]`
  Steering:  weitere User-Nachrichten als stream-json auf stdin
  Sessions:  `claude agents --json`  (aktive + Hintergrund, kein TTY nötig)
  Resume:    `--resume <id>`; Fork = Resume in neue --session-id
  Health:    `claude doctor`
  Modelle:   NICHT verfügbar  -> fail-closed-Katalog
  Usage:     NICHT verfügbar  -> UsageSnapshot(available=False)

Codex CLI 0.153.4
  Auth:      `codex login status` (Text) und `codex doctor` (Detail)
  Transport: `codex app-server` — JSON-RPC v2 über stdio. Das Protokollschema
             erzeugst du dir mit
             `codex app-server generate-json-schema --out <dir>`
             und generierst daraus deine Typen. Relevante Methoden:
             Thread/{start,list,read,resume,fork,archive,items/list,turns/list}
             Turn/{start,interrupt,steer}
             Model/list · ModelProvider/capabilities/read
             Account/{read,usage/read,rateLimits/read}
             PermissionProfile/list · McpServerStatus/list · Skills/list
  Fallback:  `codex exec --json` (JSONL) wenn app-server nicht startet

Hermes (lokal unter %LOCALAPPDATA%\hermes, Start über bin\hermes.cmd)
  Auth:      `hermes status`, `hermes auth status`
  Run:       `hermes -z "<prompt>" [-m <model>] [--provider <p>] [-t <toolsets>]
              [--skills <s>] [--in <dir>]`
  Modelle:   `hermes model --refresh` + cache/model_catalog.json
  WICHTIG:   `hermes proxy` unterstützt NUR nous und xai als OAuth-Upstreams —
             er ist KEIN Weg zu Claude oder Codex. Nicht so einplanen.

EVENT-NORMALISIERUNG
Jeder Adapter mappt seinen Strom auf: run.started, turn.started, text.delta,
thinking, tool.call, tool.result, approval.needed, needs_you, usage.delta,
run.finished{status: ok|error|cancelled|quota|timeout}.
`quota` ist ein eigener Endzustand — niemals ein stiller Providerwechsel.

TESTS
- Golden-File-Kontrakttests: für jeden Adapter einen echten Stream aufzeichnen
  (tests/providers/golden/*.jsonl) und den Parser dagegen prüfen.
- Auth-Parser-Tests inkl. "nicht eingeloggt" und "Ausgabeformat geändert".
- ModelCatalog: Claude bleibt ohne verifizierte Probe leer.
- Env-Scrubbing-Test: Adapter startet mit gesetztem ANTHROPIC_API_KEY -> Key ist
  im Kindprozess nicht vorhanden.
- Keine Netzwerktests in CI; Live-Prüfungen mit @pytest.mark.live.

ABNAHME
`python -m jarvis.providers.cli status` gibt für alle drei Provider ehrlich aus:
Auth-Zustand, Modellkatalog (oder "keine Auskunft"), Usage (oder "nicht
verfügbar"), Health mit Latenz. Ein manueller 1-Token-Run läuft je Provider durch.
Alle bisherigen Tests unverändert grün.

NICHT-ZIELE
Keine Voice-Anbindung, kein Tool-Loop, keine UI, keine Änderung an engine.py,
listener.py, config.py oder irgendetwas unter desktop_app/. Nichts entfernen.
```

---

## Abschluss

### 1. Eindeutige Architekturentscheidung

**JARVIS wird vom Agenten zum Agenten-Client.** Der Reasoning- und Tool-Loop wandert vollständig
in Claude Code, Codex und Hermes; JARVIS liefert Stimme, Werkzeuge (als MCP-Server), Gedächtnis,
Sicherheitsdurchsetzung und die Operator-Oberfläche. Die drei Provider werden über ihre offiziellen,
verifizierten Programmierschnittstellen angesprochen — `claude -p --output-format stream-json`,
`codex app-server` (JSON-RPC v2) und die Hermes-CLI mit `hermes cron` als 24/7-Schicht. Der
bestehende `LLMBackend` wird nicht erweitert, sondern durch einen run-orientierten
`ProviderAdapter` ersetzt. Lokale KI existiert nur noch hinter einer technisch erzwungenen
Isolationsgrenze für Whisper und Piper.

### 2. Kritischer Pfad

`T-001 → T-006 → T-007/T-008 → T-013 → T-014 → T-019 → T-022 → T-024 → T-026 → T-031 → T-037 →
T-041 → T-044 → T-046 → T-052 → T-060`

Riskanteste Glieder: **T-007/T-008** (fremde CLI-Verträge, wöchentliche Updates) und **T-037**
(deterministischer Ersatz für den Intent-Judge).

### 3. Die ersten zehn Tickets

| # | Ticket | Größe |
|---|---|---|
| 1 | T-001 Config-Migration v4 + `execution_mode`-Flag + Backup | S |
| 2 | T-004 CI-Wächter: Single-Writer, Import-Isolation, Local-LLM-Grep (xfail) | S |
| 3 | T-002 Memory-Recall@3-Baseline messen und einfrieren | S |
| 4 | T-003 Voice-Intent-Baseline (42 Fälle) messen | S |
| 5 | T-005 `docs/operator-requirements.md` als lizenzfreie Clean-Room-Spezifikation | M |
| 6 | T-006 `ProviderAdapter` ABC + normalisiertes Event-Modell | M |
| 7 | T-010 `AuthenticationManager` (fail closed, Env-Scrubbing) | M |
| 8 | T-008 `CodexAdapter` über `app-server` JSON-RPC v2 | L |
| 9 | T-007 `ClaudeAdapter` über `stream-json` | L |
| 10 | T-011 `ModelCatalog` mit fail-closed-Verhalten für Claude | M |

### 4. Vor der Implementierung zwingend zu bestätigen

| # | Frage | Warum blockierend |
|---|---|---|
| **E-1** | Bleibt STT/TTS lokal-primär (Empfehlung) oder soll Cloud-primär gebaut werden? | Cloud-STT/TTS bräuchte einen **vierten** Provider — die Vorgabe verbietet das. Ohne Antwort ist Phase 7 nicht spezifizierbar. |
| **E-2** | Welcher Provider trägt den Sprachdialog (Empfehlung: Hermes)? | Bestimmt Latenz, Kontingentverbrauch und Phase-3-Abnahme. |
| **E-6** | Wie viel Memory-Recall-Verlust ist akzeptabel, wenn Embeddings wegfallen? | Kein erlaubter Zugang bietet Embeddings. Wenn die Antwort „gar keiner" ist, kippt Phase 5 und wir brauchen ein anderes Gespräch. |
| **E-9** | Erlaubt die „Jarvis AI Assistant License" (Baris Sencan) diesen Umbau und eine spätere Weitergabe? | Private Nutzung unkritisch; Veröffentlichung ist ungeklärt. Betrifft auch den Namen. |
| **E-10** | Neupositionierung: „100 % lokal, keine Abos" wird ins Gegenteil verkehrt — wie soll das Produkt künftig auftreten? | Betrifft README, Release-Texte, Installer-Texte und die Erwartung an Datenschutz. |

Zusätzlich zur Kenntnis (keine Entscheidung, aber wichtig): Hermes greift intern über einen
gefälschten `claude-code`-User-Agent auf Anthropic-Credentials zu. Dieser Weg ist in diesem Plan
bewusst **nicht** eingeplant; der Claude-Zugang läuft ausschließlich über die offizielle CLI.

