# systemManager Specification

## Purpose

`systemManager` gives Jarvis bounded operating-system management in three categories: exact package management, file access beyond the home-directory sandbox, and a named set of Windows settings. It complements `desktopInteract`, `browserInteract`, `openOnComputer`, and `localFiles`; it does not replace them.

The tool is absent unless `system_management_enabled` is true. The setting defaults to false and is independent of `computer_interaction_enabled`.

## Contract

- **Name**: `systemManager`
- **Input**: one required `operation` enum plus only the typed field required by that operation. The schema exposes no executable, flags, script, registry path, registry value name, or free-form command-line field.
- **Output**: a `ToolExecutionResult` describing the observed value or completed action. Operating-system errors fail the action.

| Category | Operation | Required field | Default `critical` confirmation |
|---|---|---|---|
| Packages | `listInstalledPackages` | none | no |
| Packages | `installPackage` | `packageId` | yes |
| Packages | `uninstallPackage` | `packageId` | yes |
| Files | `listFiles` | absolute `path` | no |
| Files | `readFile` | absolute `path` | no |
| Files | `writeFile` | absolute `path`, string `content` | yes |
| Files | `appendFile` | absolute `path`, string `content` | yes |
| Files | `deleteFile` | absolute `path` | yes |
| Settings | `getDarkMode` | none | no |
| Settings | `setDarkMode` | boolean `enabled` | yes |
| Settings | `getPowerPlan` | none | no |
| Settings | `setPowerPlan` | `powerPlan` enum | yes |

At `paranoid`, every operation is confirmed. At `off`, the gate does not confirm operations, but hard-denied paths remain unavailable because the boundary is enforced inside the tool.

## Package management

Package management is Windows-only in practical availability and uses the installed `winget` executable. `packageId` must match `^[A-Za-z0-9][A-Za-z0-9._+\-]{0,127}$`; spaces, switches, separators, and shell syntax are refused. The value is winget's exact package ID, not its display name.

The exact argument vectors are:

```text
["winget", "list", "--accept-source-agreements", "--disable-interactivity"]
["winget", "install", "--id", packageId, "--exact", "--accept-package-agreements", "--accept-source-agreements", "--disable-interactivity"]
["winget", "uninstall", "--id", packageId, "--exact", "--disable-interactivity"]
```

All subprocess calls pass an argument vector with `shell=False`. A non-zero exit status is a failed tool result.

## File scope

File operations accept absolute local paths and UNC paths. This deliberately widens the `localFiles` home sandbox to any absolute path except the hard-denied roots below. The wider scope is necessary for user data on secondary drives, NAS shares, and application-managed data outside the home directory. Relative paths are refused so the daemon working directory can never silently alter the target.

The tool lists at most 100 direct children and reads at most 50,000 characters. `deleteFile` deletes files only, not directories. Write and append create missing parent directories.

The raw path is checked lexically before the target is touched. The resolved path is checked again before any operation, preventing a symlink or junction outside a permitted location from landing inside a protected root.

### Hard-denied paths

On every Windows drive, each directory and all descendants are denied for every file operation:

- `\Windows`
- `\Program Files`
- `\Program Files (x86)`
- `\ProgramData`
- `\Boot`
- `\Recovery`
- `\System Volume Information`
- `\$Recycle.Bin`

These critical files are denied at the root of every Windows drive: `bootmgr`,
`BOOTNXT`, `pagefile.sys`, `hiberfil.sys`, `swapfile.sys`, `DumpStack.log.tmp`,
`ntldr`, and `NTDETECT.COM`.

The effective paths in `SystemRoot`, `ProgramFiles`, `ProgramFiles(x86)`, and `ProgramData` are also denied, including non-standard installations.

On POSIX platforms, each directory and all descendants are denied:

- `/bin`, `/boot`, `/dev`, `/etc`, `/lib`, `/lib64`, `/proc`, `/root`, `/sbin`, `/sys`, `/usr`, `/var`
- `/System`, `/Library`, `/Applications`, `/private`

These denials are not confirmation decisions and cannot be overridden by an approval.
Windows device namespaces (`\\?\`, `\\.\`, and `\??\`), alternate data streams,
administrative or hidden UNC shares whose share name ends in `$`, and components
ending in a space or full stop are refused before resolution because their Win32
alias semantics can make the written spelling differ from the actual target.

## Named Windows settings

Only these settings are supported:

1. **Dark mode**: `getDarkMode` reads `AppsUseLightTheme`. `setDarkMode` writes `AppsUseLightTheme` and `SystemUsesLightTheme` under the fixed current-user key `Software\Microsoft\Windows\CurrentVersion\Themes\Personalize`. Values are derived only from the boolean `enabled` field.
2. **Power plan**: `getPowerPlan` calls `powercfg /getactivescheme`. `setPowerPlan` accepts only `balanced`, `powerSaver`, or `highPerformance` and maps those names to the built-in Windows GUIDs `381b4222-f694-41f0-9685-ff5bb260df2e`, `a1841308-3541-4fab-bc81-f71556f20b4a`, and `8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c` respectively.

There is no generic registry read or write operation. The model cannot supply a registry key, value name, value type, GUID, executable, or arguments.

## Security and observability

The security gate classifies mutations from the structured `operation` value. It never infers risk from the user's phrasing. Package installation and removal, file writes, appends and deletion, and both setting changes require confirmation at the default `critical` level. Inspection operations do not.

`debug_log` records hard-deny refusals and successful action execution. The existing gate records confirmation requested, approved, denied, unavailable, and failed-channel decisions.

No subprocess call uses a shell or a formatted command line. File contents and package output are data and are never executed.

## What systemManager is not

- Not a shell, terminal, PowerShell, script, or arbitrary executable runner.
- Not a generic package search by display name.
- Not a generic registry or system-preferences editor.
- Not permission elevation. The called operating-system facility decides whether the daemon account may complete an approved action.
- Not UI automation or application launching. Those remain the roles of `desktopInteract`, `browserInteract`, and `openOnComputer`.

## Testing

`tests/tools/builtin/test_system_manager.py` asserts each package vector, file API call, fixed registry call, and power-plan vector reaching the operating system. It covers package-ID validation, hard-deny refusal before action I/O, default-off registration, the read/mutate gate split, confirmation approval and refusal round trips for all three categories, and hard-deny persistence after approval. `evals/test_system_manager_selection.py` checks that the enabled tool's LLM-facing description routes a package-install request to `systemManager`.
