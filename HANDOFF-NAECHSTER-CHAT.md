# Handoff — JARVIS, Stand 2026-09-04

Arbeite in `C:\Users\User\CodingProjekte\JARVIS`, Branch `feat/control-centre-ember`
(11 Commits, nach `fork` gepusht, Baum bis auf fremde Dateien sauber).

Lies zuerst: repo-`CLAUDE.md`, dann `src/jarvis/listening/listening.spec.md`,
`src/jarvis/llm/llm.spec.md`, `src/jarvis/tools/selection.spec.md`.

⚠️ **Ein zweiter Agent arbeitet im selben Worktree** an der Web-UI.
`headless_launcher.py` und `config_metadata.py` haben sich heute mitten in der
Arbeit auf der Platte geändert. Vor jeder Bearbeitung, die vom Umfeld abhängt,
die Datei neu lesen. Kleine, fokussierte Commits.

## Wie der Stand entstanden ist

Ein Fix-Brief mit 7 Defekten aus dem echten Sprachbetrieb vom 03.09. ist
abgearbeitet (Details: Vault, `01 - Projects/JARVIS.md`, Abschnitt „Stand
2026-09-04"). Beim Nachmessen kamen zwei Dinge heraus, die **größer sind als
der ursprüngliche Brief** und beide noch offen sind.

**Testlauf:**
```
PYTHONPATH=src ./.venv/Scripts/python.exe -m pytest tests/ -q -p no:randomly --ignore=tests/test_desktop_app.py
```
**Basislinie (selbst gemessen, nicht die aus dem alten Brief — Namen diffen, nie Zahlen):**
```
tests/test_install_cuda.py::test_happy_path_writes_marker_and_log
tests/test_install_cuda.py::test_idempotent_skip_when_everything_present
tests/test_install_cuda.py::test_missing_dll_after_extract_aborts
tests/test_install_cuda.py::test_stale_marker_with_missing_dlls_redownloads
tests/test_setup_wizard.py::TestModelsPageUI::test_auto_downgrades_fast_model_when_smaller_chat_selected
tests/test_setup_wizard.py::TestModelsPageUI::test_default_chat_model_is_default_config_model
tests/test_setup_wizard.py::TestModelsPageUI::test_unlinked_mode_allows_independent_selection
tests/test_updater.py::TestInstallUpdateMacos::test_shell_script_fallback_execs_binary_when_open_fails
tests/tools/builtin/test_fetch_web_page.py::TestFetchWebPageTool::test_run_success_without_beautifulsoup
```

---

## Baustelle 1 — Der Halluzinations-Schutz ist tot, seit die Sprache gepinnt ist

**Befund, belegt.** Am 03.09. wurden zwei deutsche Äußerungen als Isländisch
transkribiert (`'Hallú, Hallú, Jarvis, hvað mástu í gráðir?'`) und lösten
volle Turns aus, einer davon 108 s. Zwei Verdächtige wurden geprüft:

- **`whisper_hotwords` ist entlastet.** Synthetischer Rundlauf (Piper → Whisper,
  4 Sätze × 4 Rauschpegel, je mit und ohne Hotwords): 14 von 16 Transkripten
  unterscheiden sich, und die Hotwords-Variante ist fast immer **besser**
  (`Jarvis` statt `Javis`/`Hayavis`/`Ja`). Sprache blieb in **allen 32 Läufen**
  `de 1.00`. Skript-Vorlage siehe unten.
- **Die Verdrahtung ist in Ordnung.** Jede `transcribe()`-Aufrufstelle übergibt
  die konfigurierte Sprache, jetzt per Test festgenagelt
  (`tests/test_whisper_language.py`).

**Die echte Ursache:** `whisper_min_language_probability` (bei Felix 0.85) ist
**wirkungslos, sobald `whisper_language` eine Sprache pinnt**. faster-whisper
überspringt dann den Erkennungsdurchlauf und meldet per Definition 1.00, das
Gate feuert nie. Genau dieses Gate war die einzige Abwehr gegen die
selbstbewussten Halluzinationen: die kommen mit `no_speech_prob` 0.000 und
gesundem `avg_logprob`, also sieht sie **kein anderer Filter**. Das steht
sogar so in `listening.spec.md` — nur warnt nichts, und beide Werte stehen
gesetzt in Felix' Config, einer davon tot.

**Machbarkeit belegt.** Eine separate Erkennungspassage trennt sauber:

| Eingabe | Ergebnis |
|---|---|
| Piper-Sprache, deutsch | `de 0.999` |
| reines Rauschen | `en 0.406` |

Der Weg ohne öffentliche API in faster-whisper **1.0.3** (gepinnt):
```python
feats = model.feature_extractor(audio)
seg   = feats[:, :model.feature_extractor.nb_max_frames]
res   = model.model.detect_language(model.encode(seg))[0]
lang, prob = [(tok[2:-2], p) for tok, p in res][0]
```
`WhisperModel.detect_language` existiert in dieser Version **nicht** als
öffentliche Methode; das oben ist exakt der Weg, den faster-whisper intern
selbst geht (`transcribe.py:396-402`). Also defensiv bauen: fehlt eines der
Attribute, fällt das Gate ersatzlos aus statt zu werfen.

**Was zu tun ist:**
1. **Kosten auf CUDA messen.** Auf CPU (int8) kostet die Passage ~10 s, genauso
   viel wie eine ganze Transkription — dort dominiert der Encoder. Auf Felix'
   CUDA-Setup dauert eine volle Transkription 0,2–0,7 s (`turns.jsonl`,
   Stage `stt`), die Passage sollte also im Bereich weniger hundert ms landen.
   **Das ist die Zahl, an der die ganze Entscheidung hängt.** ⚠️ Der laufende
   Daemon hält die GPU und VRAM ist knapp (8151 MiB) — erst Daemon stoppen.
2. Entscheiden, ob es ein eigener Schalter wird (z. B.
   `whisper_verify_language_when_pinned`, Default aus) oder ob die Passage
   immer läuft, wenn eine Sprache gepinnt **und** `whisper_min_language_probability`
   > 0 ist. Zweiteres macht eine Einstellung wieder wahr, die der Nutzer
   ohnehin gesetzt hat, und braucht keinen neuen Schlüssel.
3. TDD, Spec (`listening.spec.md`, Tabelle bei `whisper_min_language_probability`
   und `whisper_language`), MLX-Zweig mitziehen oder das Fehlen dort explizit
   dokumentieren.
4. Ein neuer Config-Key braucht **alle vier Verdrahtungen**: Dataclass-Feld,
   `load_settings()`-Parsing, `Settings(...)`-Konstruktoraufruf,
   `config_metadata.py` — plus einen Test, der eine echte Config-Datei schreibt,
   `JARVIS_CONFIG_PATH` daraufsetzt und auf **Verhalten** prüft.

---

## Baustelle 2 — FAST-Härtung gegen Reasoning-Modelle

**Befund, roh gemessen.** Der Tool-Router gibt dem FAST-Modell 50 Token. Auf
`openai/gpt-oss-20b` (Felix' bisheriges FAST-Modell) ging **alles davon in den
Reasoning-Kanal**:
```
finish_reason: length
content      : ''
reasoning    : 'We need to respond with a comma-separated list of tool names only. The user says...'
usage        : 50 completion tokens
```
Also `direct()` → `None` → Route scheitert → nächste FAST-Route (auch gpt-oss)
→ dasselbe → Kette leer → **Router fällt bei jedem Turn auf den ganzen Katalog
zurück**. Das trifft Router, Planner, Step-Resolver, Extractor und Digests
gleichermaßen und hebelt nebenbei das Zero-Tool-Grounding-Gate aus, weil ein
voller Katalog laut `reply.spec.md` als Fallback-Form gilt und das Gate gar
nicht aktiviert.

**Sofortmaßnahme ist bereits live** (nur Config, kein Code): FAST auf
`groq/compound-mini`. Danach greift der Router wieder, 5 von 6 eng, und
`setConversationMode` wird auf Deutsch, Englisch, Türkisch und Japanisch
korrekt gewählt.

**Was noch fehlt — der Code ist weiterhin ungeschützt:**
1. **`direct()` reasoning-fest machen.** `OpenAICompatibleBackend.direct()`
   liest nur `message.content`. Der Intent-Judge löst dasselbe Problem bereits
   an seiner eigenen Aufrufstelle, indem er die Antwort aus `reasoning_content`
   rettet (siehe `docs/llm_contexts.md`, Kontext #2). Diese Rettung gehört
   eine Ebene tiefer, damit jeder FAST-Aufrufer sie hat.
   ⚠️ Im hier gemessenen Fall hätte sie **nicht** gereicht: das Reasoning war
   mitten im Gedanken abgeschnitten und enthielt gar keine Antwort. Sie ist
   also eine Ergänzung, kein Ersatz für Punkt 2.
2. **Ein Cap, der Reasoning einkalkuliert.** Entweder den 50-Token-Cap des
   Routers anheben (Spec sagt ausdrücklich „the router prompt and 50-token
   output cap remain classification-shaped" — eine Änderung gehört also in die
   Spec), oder beim Routenbau erkennen, dass ein Modell einen Reasoning-Kanal
   hat, und den Cap dafür anheben.
3. **Sichtbar machen, wenn der Router aufgibt.** `debug_log` sagt heute
   „falling back to all tools", aber nur im Debug-Log. Ein Router, der bei
   jedem Turn aufgibt, sollte in der Diagnose auffallen, nicht erst wenn
   jemand die Auswahl von Hand nachrechnet.
4. **Der Gruß bleibt offen.** `"Hallo Jarvis, wie geht es dir?"` macht auch mit
   `compound-mini` den ganzen Katalog auf, statt `none` zu sagen. Harmlos
   (Fallback-Form aktiviert das Gate nicht), aber es ist der Fall, für den es
   in `prompts.spec.md` eine ausdrückliche Regel gibt.
5. Evals laufen lassen — beides sind Änderungen am Router-Verhalten.
   ⚠️ Ohne `gemma4:e2b` überspringt die Eval-Suite fast alles und die Zahlen
   bedeuten nichts.

---

## Kleinere offene Punkte

- **`agentic_max_turns: 3` × 30 s Chain-Budget = bis zu 90 s Modellzeit pro
  Turn.** Das Budget begrenzt *einen* Kettenlauf, nicht den Turn. Felix
  entscheiden lassen, ob das für Sprache reicht.
- **PR #7 mergen** (`feat/control-centre-ember` → `felix/jarvis-consolidated`)
  steht weiter offen und hat jetzt 11 zusätzliche Commits drauf.
- **Rube-MCP-Endpoint** braucht Felix selbst (Composio-Account, dann
  `mcp-remote` einmal interaktiv für OAuth).
- Aus dem Control-Centre-Redesign: `api.js` ohne Request-Timeout,
  `Connection: close`, Kontrast-Sweep-Flake. **Gehört dem anderen Agenten**,
  nicht anfassen ohne Absprache.

## Was nur Felix kann

Kein Mikrofon, keine Lautsprecher in dieser Umgebung. Diese vier Punkte
brauchen ihn, mit den genauen Sätzen:

1. **„Jarvis, trag mir bitte einen Termin für morgen um 15 Uhr ein"** — muss den
   Termin anlegen. Falls nicht: es muss eine ehrliche Meldung kommen
   („kein tool-fähiges Modell hat geantwortet"), **nie** „versuch es nochmal".
2. **„Jarvis, Konversationsmodus an"** — der Header im Control Centre muss
   umspringen. Router-seitig ist das bereits bewiesen, die Sprachkette nicht.
3. **Eine Bestätigung durchspielen** und beim zweiten Aufruf desselben Tools
   prüfen, dass nichts mehr fragt (`~/.jarvis/security_approvals.json` zeigt,
   was gemerkt wurde).
4. **Gesamtlatenz** gegen das eigene Gefühl halten und `turns.jsonl` ansehen:
   `start_ms` mitlesen, nicht nur `duration_ms` — eine Lücke zwischen zwei
   Stages gehört oft dem Turn davor.

## Nützliches

- Control Centre: `http://127.0.0.1:5055`. `GET` geht per curl, `POST` gibt
  **403** (Request-Guard) — für echte Turns den Router direkt in Python rufen.
- Start: `start-jarvis-desktop.bat`. Neustart: Listener-PID auf 5055 per
  `netstat -ano`, hoch zum obersten Python-Elternprozess, `taskkill //PID x //T //F`,
  dann die bat neu starten. Nach ~40 s antwortet 5055 wieder.
- Config `~/.config/jarvis/config.json`, vor jeder Änderung eine
  `config.json.bak-<grund>`-Kopie anlegen (Felix' Konvention).
- Route-Cooldowns in `~/.jarvis/llm_routes_state.json` sind oft der **einzige
  erhaltene Beweis**, wenn die In-Memory-Logs weg sind: gegen die Live-Config
  für einen konkreten Zeitstempel nachrechnen.
- Eine Route gilt erst als gesund, wenn ein echter Chat-Call **mit
  Tool-Schema** durchgelaufen ist. `GET /models` verbirgt sowohl leeres
  Guthaben (Cerebras: 200 im Katalog, 402 beim Call) als auch fehlende
  Account-Freischaltung (NVIDIA: gelistet, aber 404 „Not found for account").

### Vorlage für den synthetischen Sprach-Rundlauf

```python
from piper import PiperVoice          # Stimme aus cfg.tts_piper_model_path
from faster_whisper import WhisperModel
# Satz synthetisieren -> WAV -> float32 -> auf 16 kHz resamplen
# Rauschen in Stufen addieren, je zweimal transcribe(), Ergebnisse vergleichen
```
Damit lassen sich Whisper-Fragen ohne Felix' Stimme entscheiden. Was es
**nicht** ersetzt: Weckwort-Erkennung, VAD-Endpointing, Echo-Unterdrückung und
die Lautsprecher-Ausgabe.
