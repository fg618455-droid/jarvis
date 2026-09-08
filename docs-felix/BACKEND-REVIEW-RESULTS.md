# Backend consolidation acceptance - 9 September 2026

Status: implementation and package review in progress; release not accepted.
Original tilapia changes are preserved in their original worktree and a verified
NAS import. Implementation and package work is on NAS. No frontend assets authored.

## Reproduction

Use Python 3.10 with repository dependencies, PYTHONPATH=src,
PYTHONDONTWRITEBYTECODE=1, QT_QPA_PLATFORM=offscreen for tests, and TEMP/TMP in an
owned Synology Drive directory. Run `python -B -m pytest tests -q --tb=short
-p no:cacheprovider`. Linux CI uses Xvfb for desktop dependencies.

## Automated evidence

- Integrated run on d8368c1: **3787 passed, 5 skipped, 2 deselected**, 352.36 seconds.
- This includes the error-5 config retry, initial harness guards and Linux CI fixes.
  Subsequent subscription storage/shutdown and expanded harness changes passed 85 focused tests; their final CI remains pending.
- Exact package checks in clean worktrees: A 82 passed; B 359 passed, 1 skipped;
  D 35 passed; C 75 passed; E 48 passed. Counts overlap and must not be added.
- Original timer expiry and Windows/Git-Bash fallback failures are corrected.
- Sporadic config persistence produced Windows PermissionError 5. Atomic replace
  now retries access/sharing errors with a bound; permanent denial preserves the
  old file. Diagnostics omit config values.
- Linux baseline CI exposed missing X display, WindowsPath construction on Linux,
  lost IPC capture history and shared time.sleep test interference. All three F CI
  checks passed on d8368c1: Linux unit job 5m16s, browser jobs 6m22s and 6m14s.

## Live evidence

| Check | Result |
|---|---|
| Groq GPT-OSS visible chat, stream/TTFT, native tools | passed |
| Groq all 20 registered tool schemas together | passed |
| Mistral small | unavailable: quota, rechecked 9 September |
| OpenRouter tested configured/free candidates | unavailable: missing model, quota or empty response |
| Composio authenticated discovery, 7 tools, GET_TOOL_SCHEMAS | passed |
| Chrome 1.8.0 discovery, 29 tools, list_pages | passed |
| Coya YouTube 1.2.0 discovery, 14 tools, semantic transcript check | passed |
| Controlled screenshot OCR through registry | passed |
| Supervised desktop MCP approval through registry | passed |
| Supervised explicit denial | open: earlier Approve selections; latest attempt timed out without a click |
| Claude Sonnet 5 / Codex gpt-5.6-sol text-tool parser and registry dispatch | passed |
| Temporary file and nutrition write/read/approved delete through registry | passed |
| Fail-closed critical actions without a channel | expected_denial |
| Time, public web fetch, system file read, MCP refresh, keyword tool search | passed |
| School/provenance tools with empty test DB, stop | passed |
| Codex CHAT browser interaction, independently verified Example Domain title | passed |
| Codex CHAT controlled native editor, independently verified exact field contents | passed |
| openOnComputer on owned test directory | passed |
| Weather for explicit Berlin location and public web search through registry | passed |
| Actual NAS Vault search, complete 434-note index | passed |
| NAS configuration candidate with correct Vault root and accepted MCP pins | prepared, not activated; database transfer and storage audit pending |

Safe live result files are retained in the NAS repository's private Git metadata
and the owned Synology acceptance directory. They contain capability/status/timing
metadata, not prompts, response bodies, credentials, transcript text or screenshots.
No external message was sent. No new metered route was activated. Quota failures
are not converted into healthy routes.

## Implementation checks

PRIVATE is enforced at graph/school and memory/tool/loop summary boundaries.
Runtime tests cover credential rotation and parallel old/new snapshots. MCP tests
use disposable real processes for discovery, isError, idle and one reconnect;
startup no longer blocks unrelated servers and stale callers cannot resurrect
removed configurations. Vault search bounds its wait and reports partial indexing;
a stalled filesystem worker may remain alive, with no unbounded worker spawning.

The opt-in live harness refuses BOM, malformed JSON, implicit/default config paths
and output workspaces outside NAS/Synology before initialising application state.
The harness covers the specific cases recorded above; unlisted scenarios are not
claimed as accepted. Productive configuration was read only: its Vault root is still the
parent SecondBrain directory and must be corrected via a NAS configuration.

## PR sequence

All draft PRs target develop; pending predecessor diffs are cumulative.

- Base [#8](https://github.com/fg618455-droid/jarvis/pull/8): f438370.
- A [#9](https://github.com/fg618455-droid/jarvis/pull/9): ba2f6a4.
- B [#10](https://github.com/fg618455-droid/jarvis/pull/10): 54114e5, 02bcd84.
- D [#11](https://github.com/fg618455-droid/jarvis/pull/11): 354d209.
- C [#12](https://github.com/fg618455-droid/jarvis/pull/12): e835dab.
- E [#13](https://github.com/fg618455-droid/jarvis/pull/13): 5f81bb2.
- F [#14](https://github.com/fg618455-droid/jarvis/pull/14): d8368c1 plus pending live-acceptance follow-up.

No merge/release approval is inferred from automated tests while live gates remain
unavailable or untested. PR review is local; no review comments posted.


## Follow-up review

The real subscription text-tool checks exposed a missing Claude adapter close hook;
this now stops the sidecar on runtime shutdown and is regression-tested. Claude
session persistence is disabled. Codex history is disabled and its log/SQLite state
paths stay inside the caller-controlled request temporary directory. A direct
router constructor now enforces the CHAT-only subscription restriction as well.

Groq CHAT browser-planner trials failed and are not counted as accepted. A separate
Codex CHAT browser acceptance is in progress. Mistral primary remains quota-blocked;
there is no claim that all configured providers are currently healthy.

## 9 September continuation

The ten read-only registry cases passed together, including weather and web search.
The first web-search harness call used the wrong argument name; correcting it to
the actual `search_query` schema made the real call pass. No product workaround.
Browser/editor subscription tests no longer depend on a Groq credential.
The full post-follow-up suite is running; the earlier 3787 count is not its result.
A private NAS runtime candidate is prepared outside Git. It is not activated and
does not claim to satisfy the blocked Mistral-primary or database-migration gates.
