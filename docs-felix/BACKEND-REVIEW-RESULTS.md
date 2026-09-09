# Backend consolidation acceptance - 9 September 2026

Implementation and local automated verification include the NAS storage follow-up.
Final-head CI status is tracked in the PR and Vault. Release is not accepted: Mistral
quota, supervised explicit Deny and productive activation remain open.
Original tilapia changes are preserved. All authored work is on NAS/Synology;
there is no authored frontend-asset diff against the integrated baseline.

## Reproduction and automated evidence

Use the repository dependencies, PYTHONPATH=src, PYTHONDONTWRITEBYTECODE=1,
QT_QPA_PLATFORM=offscreen and an owned Synology TEMP/TMP directory. Run
`python -B -m pytest tests -v --tb=short -p no:cacheprovider
-o faulthandler_timeout=60 --durations=10`. Linux unit CI uses Xvfb.

- Full NAS storage implementation 4d6f693: **3805 passed, 5 skipped, 2 deselected**,
  1 warning, 501.49s. The subsequent c77d213 changes only the subprocess test
  import path: its five storage tests passed without inherited PYTHONPATH. Linux
  had exposed that test setup omission; both browser checks on 4d6f693 passed.
- Full 536c3c6: **3800 passed, 5 skipped, 2 deselected**, 4 warnings, 521.12s.
- Full preceding 595a7a8: 3793 passed; its slower voice tests performed real
  provider warmup. Test fixtures now isolate that I/O while retaining real threads.
- All seven package heads below passed all three CI checks independently.
- Exact initial package checks: A 82; B 359 plus one skip; D 35; C 75; E 48.
  Counts overlap and must not be added. Baseline CI fixes: 238 passed.
- HTTP residency/API follow-up: 89 passed. Ordered MCP swap/API: 28 passed.
- NAS storage follow-up: 97 focused tests passed, including real dictation-file
  output and a subprocess proving location cache paths are chosen at startup.
  Full local results are above; final-head CI is recorded in the PR and Vault.

## Local review findings resolved

- Atomic config replacement produced Windows PermissionError 5, in addition to
  sharing errors 32/33. Bounded retries preserve the previous file on permanent
  denial; diagnostics expose the error class/code, never configuration values.
- PRIVATE is enforced at graph/school and memory/tool/loop summary boundaries.
  FAST/CHAT reject local routes; subscription routes are CHAT-only even when
  constructed directly. Empty credentials cannot create an active cloud route.
- Runtime settings are copied; environment-key rotation creates new adapters.
  Parallel old/new turns retain their generation; failed builds roll back.
  Claude now exposes its sidecar close hook and disables session persistence.
  Codex history is disabled and temporary logs/SQLite state use the call directory.
- MCP worker startup does not hold the global pool lock. Expected-worker identity
  prevents late eviction of replacements. Registry writers/refresh snapshots are
  serialised separately from cache readers, admitting the new config before
  discovery. Real-process change/removal and concurrent publication are tested.
- `ollama ps` hung in Windows subprocess pipe cleanup despite its timeout. System
  residency now uses bounded, proxy-free loopback HTTP with redirects disabled.
  Existing response fields are preserved; no Ollama CLI is spawned. The actual
  local call returned in 2.093s. This proves bounded status behaviour, not model
  readiness. Endpoint contract: https://docs.ollama.com/api/ps.
- CI prerequisites were propagated base -> A -> B -> D -> C -> E -> F without
  rewriting history. Fixes cover X display, Windows paths, IPC capture, scoped
  import/sleep mocks, current config versions and privacy-aware API assertions.
- Vault search bounds waiting, skips offline placeholders and reports partial
  indexing. A stalled filesystem worker may remain alive; worker count is bounded.
  Bulk read_notes is not represented as a hard filesystem timeout.

Review covers the authored backend delta, regressions and package CI. Inherited
frontend commits are preserved, not claimed as newly authored or re-reviewed.
No external review comments or approvals were posted.

## Live evidence

| Check | Result |
|---|---|
| Groq GPT-OSS chat, stream/TTFT, native tools and all 20 schemas | passed |
| Mistral small, including recheck after the UTC day reset | unavailable: quota |
| Tested OpenRouter candidates | unavailable: missing model, quota or empty response |
| Composio authenticated discovery and harmless schema retrieval | passed, also with candidate NAS auth: seven tools, 10.922s cold start |
| Chrome 1.8.0 discovery/list_pages | passed |
| Coya YouTube 1.2.0 real transcript and semantic check | passed |
| Known screenshot OCR text through registry | passed |
| Explicit supervised MCP Approve | passed |
| Explicit supervised MCP Deny | open: latest click was Approve; earlier timeout is not a denial |
| Claude Sonnet 5 / Codex gpt-5.6-sol text parser and getTime dispatch | passed |
| Temporary files and nutrition DB: write/read/approved delete | passed |
| Critical writes/crew/browser/desktop without a confirmation channel | expected_denial |
| Codex CHAT browser, independently checked Example Domain title | passed |
| Codex CHAT controlled native editor, independently checked exact field | passed |
| openOnComputer on owned test directory | passed |
| Ten read-only registry cases: time, weather, search/fetch, system read, MCP refresh, keyword tool search, school/provenance, stop | passed |
| Actual NAS Vault search, complete 434-note index | passed |
| NAS profile path check and real getTime dispatch | passed, nine paths under NAS |
| Productive NAS activation | pending live gates |

Failed Groq CHAT browser-planner trials remain failed evidence; Codex CHAT was
accepted separately. No external message was sent. No new metered route was
activated and no quota failure was converted to a healthy provider.

## NAS runtime preparation

A private configuration/environment candidate lives outside Git under
`\\DS723plus\home\CodingProjekte\JARVIS-backend-runtime-20260909`.
It corrects the Vault root, pins accepted MCP versions, uses Groq FAST and
subscription CHAT, and disables quota-blocked/unverified cloud candidates.
The original database was copied without opening the source in SQLite: no WAL or
rollback journal existed, repeat source hashes matched, the copy was byte-identical
and SQLite quick_check returned ok. Revalidate before adoption; it is a snapshot.
Seven Whisper-cache files, two Piper files and six supporting data/state files
were copied and verified. Existing Claude/MCP authentication was copied privately. An initial Composio
check with an outdated C-drive auth snapshot failed; the accepted NAS OAuth
copy then passed real discovery and schema retrieval. The stale copy is unused.
Whisper resolves offline from the copied cache; this does not claim model loading.
TEMP/TMP use the local Synology equivalent for Windows CLI compatibility and a
real temporary-directory check passed. HOME/CODEX_HOME are unchanged.

`JARVIS_DATA_DIR` redirects application defaults (database, dictation, location,
Piper cache, prompt dumps, provider state/probes, desktop logs/lock) without
changing HOME or provider authentication. Explicit configured paths keep priority.
Third-party caches/authentication have separate NAS paths in the private profile.
No production daemon has been launched or replaced by these checks.

## PR stack and release verdict

All target develop, remain Draft and have cumulative diffs until predecessors
merge. Base through E and the preceding F head 536c3c6 each have three successful
CI checks. The latest F follow-up is tracked separately in PR #14:

| Order | PR | Head |
|---|---|---|
| Base | https://github.com/fg618455-droid/jarvis/pull/8 | 1af661c |
| A | https://github.com/fg618455-droid/jarvis/pull/9 | fec821d |
| B | https://github.com/fg618455-droid/jarvis/pull/10 | ac5ec4e |
| D | https://github.com/fg618455-droid/jarvis/pull/11 | ab2be40 |
| C | https://github.com/fg618455-droid/jarvis/pull/12 | a5e8f70 |
| E | https://github.com/fg618455-droid/jarvis/pull/13 | 07b4b1e |
| F | https://github.com/fg618455-droid/jarvis/pull/14 | c77d213 (final-head CI in PR #14) |

No merge or release while the handoff's required live gates remain unavailable
or untested. Evidence files contain status/capability/timing metadata, not keys,
private text, screenshots or transcripts. Private runtime/auth files are not part
of any PR. Durable logs are in the shared NAS repository Git metadata and owned
Synology acceptance directory; the Vault tracks the latest exact heads/results.
