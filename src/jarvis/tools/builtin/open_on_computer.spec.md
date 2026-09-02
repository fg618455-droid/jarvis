## openOnComputer Spec

### Purpose

Put something on the user's screen. Jarvis runs on the machine it is speaking from, so "open YouTube", "start Notepad" and "show me my downloads" are local actions. Without this tool the assistant can describe a link but never open it, which is the difference between an assistant and a search box read aloud.

### Contract

- **Name**: `openOnComputer`
- **Input schema**: a single required `target` string. One property, because the planner's direct-exec fast path resolves single-property schemas without a second LLM round-trip (same reason `logMeal` keeps one public field).
- **Output**: on success, a one-line statement of what was opened. On failure, a `ToolExecutionResult` with a stable code: `invalid_config` for a missing target or a refused URL scheme, `invalid_argument` when nothing on the machine matches, `execution_failed` when the operating system refused the action.

### Resolution order

A `target` is resolved deterministically, first match wins:

1. **Explicit scheme** (`something://…`) — opened only when the scheme is `http` or `https`. Any other scheme is refused.
2. **A path inside the home directory** that exists and is not an executable-type file — handed to the platform opener.
3. **An application name** that resolves to an executable file — started with no arguments.
4. **A domain-shaped target** (`youtube.com`, `www.bbc.co.uk/news`) — prefixed with `https://` and opened.
5. Otherwise the tool fails and says so.

Step 4 sits below step 3 so an unresolvable `notepad.exe` fails honestly instead of opening a browser at `https://notepad.exe`. The host-shaped check rejects targets carrying characters a hostname cannot hold, so a Windows path with a dot in it (`C:\Users\me\notes.txt`) never reads as a site. A single suffix list (Windows/macOS/Linux executable and script types — `exe`, `bat`, `ps1`, `vbs`, `sh`, `py`, …) both blocks step 2 for a matching existing file and blocks the domain fallback for the same names, so a target that resolves to nothing everywhere fails honestly instead of quietly becoming a web address. `.com` is deliberately absent from that list: as a top-level domain it outweighs the DOS executable format, so an unopenable `invoice.com` falls through to a harmless `https://invoice.com` browser open rather than failing.

### Cross-platform behaviour

| Action | Windows | macOS | Linux |
|---|---|---|---|
| URL | `webbrowser.open` | `webbrowser.open` | `webbrowser.open` |
| Path | `os.startfile` | `open <path>` | `xdg-open <path>` |
| Application lookup | `shutil.which`, then the `App Paths` registry table | `shutil.which` | `shutil.which` |
| Application launch | `Popen([exe])` | `Popen([exe])`, or `open -a` for a `.app` bundle | `Popen([exe])` |

The `App Paths` fallback is what makes named programs installed outside `PATH` (Spotify, Chrome, Discord) reachable. It is the same table the Windows Run dialog consults.

### Security

No user-supplied string ever reaches a shell. URLs go through `webbrowser`, applications are resolved to an absolute executable path **before** anything is started, and every subprocess call passes an argument vector, never a command line. A target carrying `&&`, `|`, `;` or `$(…)` is therefore a name that resolves to nothing, not an instruction that runs.

Three further restrictions:

- **Schemes**: only `http` and `https`. `file:`, `javascript:` and `data:` turn "open this" into "read this off disk" or "run this", which is a different and far more privileged action.
- **Paths**: only inside the user's home directory, matching `localFiles`, and never an executable-type suffix. The platform opener does not "view" a `.bat`, `.lnk`, `.ps1`, `.vbs`, `.py`, `.sh`, … file, it runs it the same as double-clicking would, which turns "open this" into "run this" exactly as an unapproved scheme would. Such a target resolves to nothing, the same as a path outside home.
- **Applications**: bare names only. A target carrying a path separator is treated as a path, so no arbitrary executable elsewhere on the disk can be launched by pointing at it, and a directory (which `os.access` reports as executable) is never mistaken for a program.

The tool is not in the security gate's static `_CRITICAL_BUILTINS` set, because two of its three reachable actions don't need confirmation: a web page opens in the user's own browser, and a home-directory path is bounded to the user's own non-executable data (enforced above). The third, launching an installed application, is gated: the gate resolves `target` through `resolves_to_application_launch` (the same resolution order `run()` uses, steps 1–3) before the tool executes, and requires confirmation whenever that resolves to an application launch, at the default `critical` level. This is necessary because a plain name is still a choice of *which* program starts, made by whatever chose the tool call rather than the user typing it, and the gate cannot tell that choice apart from a URL or a path without doing the same resolution the tool itself would. Asking for a spoken numeric challenge before every "open YouTube" would make the feature unusable for its main purpose, so only the application-launch path pays that cost. At `paranoid` level every action is confirmed regardless.

### What openOnComputer is NOT

- Not a way to run commands. There is no argument, flag or command-line field, by design.
- Not a file reader. It hands a path to the desktop; `localFiles` is for reading contents.
- Not a browser automation surface. It opens an address and stops there.

### Prompting

Websites need a full `https://` address, and the tool description says so, because the model is the only part of the system that knows a "watch" address from a "search" address for a given site. A bare site name with no dot fails with a message naming both possibilities rather than guessing.

### Testing

`tests/tools/builtin/test_open_on_computer.py` asserts what reached the operating system: the exact URL handed to the browser, the exact argument vector handed to `Popen`, the exact path handed to the opener. It covers scheme refusal, home-directory containment, shell-syntax targets resolving to nothing, an unresolvable executable-suffix target not falling through to the browser, and directories not being launchable.
