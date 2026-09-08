# Backend consolidation acceptance ? 8 September 2026

Status: implementation and package review in progress; release not accepted.
Original tilapia changes are preserved in their original worktree and a verified
NAS import. Implementation and package work is on NAS. No frontend assets authored.

## Reproduction

Use Python 3.10 with repository dependencies, PYTHONPATH=src,
PYTHONDONTWRITEBYTECODE=1, QT_QPA_PLATFORM=offscreen for tests, and TEMP/TMP in an
owned Synology Drive directory. Run `python -B -m pytest tests -q --tb=short
-p no:cacheprovider`. Linux CI uses Xvfb for desktop dependencies.

## Automated evidence

- Integrated run: 3779 passed, 5 skipped, 2 deselected, 374.06 seconds.
- This precedes the final error-5 config retry, harness guards and Linux CI fixes;
  those changes require a final full run.
- Exact package checks in clean worktrees: A 82 passed; B 359 passed, 1 skipped;
  D 35 passed; C 75 passed; E 48 passed. Counts overlap and must not be added.
- Original timer expiry and Windows/Git-Bash fallback failures are corrected.
- Sporadic config persistence produced Windows PermissionError 5. Atomic replace
  now retries access/sharing errors with a bound; permanent denial preserves the
  old file. Diagnostics omit config values.
- Linux baseline CI exposed missing X display, WindowsPath construction on Linux,
  lost IPC capture history and shared time.sleep test interference. Corrections
  are included in F and require CI verification. The baseline browser jobs passed.

## Live evidence

| Check | Result |
|---|---|
| Groq GPT-OSS visible chat, stream/TTFT, native tools | passed |
| Groq all 20 registered tool schemas together | passed |
| Mistral small | unavailable: quota |
| OpenRouter tested configured/free candidates | unavailable: missing model, quota or empty response |
| Composio authenticated discovery, 7 tools, GET_TOOL_SCHEMAS | passed |
| Chrome 1.8.0 discovery, 29 tools, list_pages | passed |
| Coya YouTube 1.2.0 discovery, 14 tools, semantic transcript check | passed |
| Controlled screenshot OCR through registry | passed |
| Supervised desktop MCP approval through registry | passed |
| Supervised explicit denial | open: Approve was selected in denial attempts |
| Subscription text-tool parser and dispatcher | open |
| Complete built-in matrix including temporary nutrition DB | open |
| Productive Vault root and validated configuration adoption | open |

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
The current harness implements controlled OCR; remaining tools are not claimed as
accepted. Productive configuration was read only: its Vault root is still the
parent SecondBrain directory and must be corrected via a NAS configuration.

## PR sequence

All draft PRs target develop; pending predecessor diffs are cumulative.

- Base [#8](https://github.com/fg618455-droid/jarvis/pull/8): f438370.
- A [#9](https://github.com/fg618455-droid/jarvis/pull/9): ba2f6a4.
- B [#10](https://github.com/fg618455-droid/jarvis/pull/10): 54114e5, 02bcd84.
- D [#11](https://github.com/fg618455-droid/jarvis/pull/11): 354d209.
- C [#12](https://github.com/fg618455-droid/jarvis/pull/12): e835dab.
- E [#13](https://github.com/fg618455-droid/jarvis/pull/13): 5f81bb2.
- F: being prepared.

No merge/release approval is inferred from automated tests while live gates remain
unavailable or untested. PR review is local; no review comments posted.
