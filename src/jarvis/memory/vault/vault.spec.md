# Obsidian Vault Memory Specification

## Overview

Jarvis reads the user's whole Obsidian vault and mirrors its own knowledge graph into a single folder inside it. Two asymmetric halves:

| Half | Scope | Direction |
|------|-------|-----------|
| **Reader** | Every markdown file in the vault | Vault → Jarvis, read-only |
| **Mirror** | Exactly one folder (`obsidian_memory_folder`) | Graph → Vault, write |

The asymmetry is the point. A personal vault holds years of notes the assistant could never learn from conversation alone, so reading is broad. Those same years are irreplaceable, so writing is fenced into one folder that the user can delete wholesale without losing anything of their own.

The knowledge graph in [`graph.spec.md`](../graph.spec.md) remains the source of truth. SQLite is authoritative; the vault folder is a projection of it plus a protected region the user owns. The mirror never reads graph state back out of markdown.

Everything is local file I/O. No sync service, no Obsidian plugin, no network. Obsidian itself need not be running or even installed: the folder is plain markdown on disk.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `obsidian_vault_path` | `None` | Absolute path to the vault root. `None` disables both halves entirely. |
| `obsidian_memory_folder` | `"Jarvis"` | Vault-relative folder the mirror owns. The only path Jarvis may write to. |
| `obsidian_write_mode` | `"dry_run"` | `"off"` (never write), `"dry_run"` (compute and report the plan, write nothing), `"on"` (apply the plan). |
| `obsidian_read_enabled` | `True` | Whether the reader indexes the vault. Independent of write mode. |
| `obsidian_read_max_results` | `3` | Vault snippets injected per enrichment pass. |
| `obsidian_index_max_file_kb` | `512` | Files larger than this are skipped by the indexer. |

`obsidian_write_mode` defaults to `"dry_run"` and stays there until the user changes it. A fresh install, a fresh config, or a config that predates this feature can never write into a vault without an explicit opt-in. `"off"` and `"dry_run"` differ only in whether the plan is computed and logged; neither touches disk.

Validation at load: a non-existent `obsidian_vault_path`, or one that is not a directory, degrades to `None` with a warning. `obsidian_memory_folder` is rejected if it is absolute, empty, or contains `..` after normalisation; rejection disables the mirror, it does not fall back to a different folder.

## Write Fence

`guard.py` owns one function, and every write in this subsystem passes through it.

`resolve_managed_path(vault_root, memory_folder, relative_name) -> Path` returns a path only if all of these hold, and raises `VaultWriteError` otherwise:

1. `vault_root` resolves (symlinks followed) to an existing directory.
2. The memory folder resolves to `vault_root/memory_folder` and is inside `vault_root`.
3. The final path resolves to a direct child of the memory folder.
4. The final path ends in `.md`.
5. No component of the resolved path is a symlink pointing outside the memory folder.

Resolution happens before the prefix comparison, so `..` traversal, a symlinked memory folder, and a symlinked note file all fail closed. The check is on the *resolved* path, never the string the caller passed.

There is no "write anywhere" escape hatch. The mirror cannot address a path outside its folder even with a hostile node name, because filenames are generated (see below), not taken from user or LLM text.

## File Layout

One graph node, one markdown file, flat inside the memory folder. No subdirectories: the hierarchy lives in wikilinks, which is what Obsidian's graph view reads, and a flat folder makes the orphan sweep trivial. The one exception is `_quarantine`, a holding area for notes removed from the mirror while still carrying user content (see "Deleting a Note With User Content" under Sync); it is not part of the node-to-file mapping and orphan sweeps do not descend into it.

The root node is not mirrored. It holds no data and exists only as a container.

### Filename

`<Branch label> — <Node name> (<short id>).md`

- Short id is the first 8 characters of the node's UUID, or the whole id for the fixed branch roots (`user`, `directives`, `world`).
- Node name is slugified: Obsidian-forbidden characters (`# ^ [ ] |`), path separators, Windows-reserved characters (`< > : " \ / ? *`), and control characters are stripped; whitespace collapses to single spaces; trailing dots and spaces are trimmed; the result is capped at 60 characters on a word boundary. An empty result becomes `Untitled`.
- Total filename stays under 120 characters so the full path survives on Windows.

The id suffix is not decoration. It guarantees a mirrored file can never collide with a note the user already wrote, which is what makes `[[wikilinks]]` unambiguous and makes deletion safe to automate.

A node renamed in the graph produces a new filename. The mirror detects this by node id, renames the existing file with `os.replace`, and rewrites every file that links to it. Links are stable because they are regenerated from graph edges on every write, never parsed out of markdown.

### File Content

```markdown
---
jarvis_managed: true
jarvis_node_id: a1b2c3d4-e5f6-4789-a0b1-c2d3e4f5a6b7
jarvis_branch: user
created: 2026-08-11
updated: 2026-08-11T14:03:00+02:00
access_count: 12
tokens: 340
tags:
  - jarvis/memory
  - jarvis/user
---

# Food preferences

What the user eats, likes, and avoids.

- Regularly eats sushi
- Dislikes coriander

## Related

- Parent: [[User (user)|User]]
- Children: [[User — Restaurants (d4e5f6a7)|Restaurants]]

<!-- jarvis:end -->
```

Frontmatter is emitted as explicit YAML with quoted scalars wherever the value could be misread (anything containing `: `, a leading `#`, `-`, `[`, `{`, `!`, `&`, `*`, or a trailing space). `jarvis_managed: true` is the ownership marker; a file without it is not ours regardless of where it sits.

The `tags` entries put every mirrored note under `jarvis/memory` plus a per-branch tag, so the user can exclude the whole mirror from Obsidian search with `-tag:jarvis/memory` in one query.

Body facts are the node's `data`, split on newlines, emitted as a bullet list. Each line is escaped so it cannot inject structure into the user's vault:

- `[[` becomes `\[\[` — a fact that happens to quote a wikilink does not silently create an edge to one of the user's notes. Only the Related section produces edges, and those come from graph parentage.
- A line consisting solely of `---` is prefixed with a bullet, so it can never be read as a frontmatter fence.
- `#` at line start survives (it is inside a list item, so it renders as text, not a heading).

### The Protected Region

Everything after the `<!-- jarvis:end -->` marker belongs to the user. On every rewrite the mirror reads the existing file, splits at the marker, and re-emits the tail verbatim. If the marker is absent from an otherwise managed file, the whole file is treated as machine content and replaced, with the marker appended.

This is the one place where the vault holds memory that SQLite does not. The reader indexes it (see below), so a note the user adds under the marker reaches the assistant on the next relevant question. It is a one-way user-to-Jarvis channel that needs no conflict resolution: Jarvis never edits below the marker, the user never needs to edit above it.

## Sync

`plan_sync(store, cfg) -> SyncPlan` compares the graph against the folder and returns an ordered list of `PlannedChange(action, node_id, path, reason, diff)` where action is one of `create`, `update`, `rename`, `delete`, `skip`, `refuse`.

- `update` is emitted only when the rendered content differs from the file on disk. An unchanged node produces `skip`. This matters: the vault sits in OneDrive and under Syncthing, and rewriting 60 identical files on every diary flush would generate sync traffic and Obsidian re-index churn for nothing.
- `delete` is emitted for a file in the folder whose `jarvis_managed` is true and whose `jarvis_node_id` no longer exists in the graph.
- `refuse` is emitted for a file in the folder that lacks the ownership marker but occupies a name the mirror wants. The mirror never overwrites it and never deletes it; the node is left unmirrored and the collision is logged.
- Any file in the folder without `jarvis_managed: true` is otherwise ignored completely. The user may keep their own notes in the folder; they are read, never written.

`apply_sync(plan, cfg)` executes a plan. In `dry_run` mode it is never called. Writes are atomic: render to `<name>.md.tmp` in the same directory, `os.replace` onto the target. A crash mid-sync leaves every file either fully old or fully new.

### Deleting a Note With User Content

A `delete` action removes the file from disk only when its protected tail (see below) is empty after stripping whitespace. When the tail holds real user content, `apply_sync` quarantines the file instead: it is moved with `os.replace` to `<obsidian_memory_folder>/_quarantine/<same filename>`, resolved through `resolve_managed_path` exactly like every other target in this module, so the quarantine slot is bound to the vault root and cannot escape it. The content is untouched by the move, machine header and protected tail alike. If a file already occupies that quarantine slot, the delete is refused rather than overwriting whatever was quarantined there before.

This exists because a node id can disappear from the graph for reasons that have nothing to do with the user's own annotations, most notably a graph-wide reshape that empties `valid_ids` for an entire sweep at once. Unlinking on sight would destroy the one thing this subsystem promises never to touch: content below `<!-- jarvis:end -->`. Quarantining costs nothing when the tail truly is empty (the common case, and the only one that still unlinks) and costs one house-kept file the one time it is not.

### When Sync Runs

The mirror subscribes via `register_graph_mutation_listener` (see `graph.spec.md`). Events are pushed onto a queue drained by a single background worker with a 3 second debounce, so one diary flush that writes five facts across two nodes produces one sync pass, not five. The worker only ever syncs the nodes named by the coalesced events plus a link-fixup for their parents and children.

A full sweep, including the orphan `delete` pass, runs once at daemon start-up and on explicit request. Mutation-driven passes never delete.

Listener exceptions are swallowed and logged, exactly as the graph's own listener contract requires. A vault that is offline, on an unmounted drive, or read-only degrades to no mirroring; it never fails a memory write. The mirror is a projection, and a projection failing is not a data loss.

### Dry Run

With `obsidian_write_mode = "dry_run"` the worker computes the plan and reports it, then discards it. The report goes to `debug_log` and to stdout as an indented, emoji-prefixed summary listing every file that would be created, updated, renamed, or deleted, with a line-level diff for updates and a total.

`python -m jarvis.memory.vault.preview` runs a full sweep plan against the configured vault and prints the same report without starting the daemon. It is read-only by construction: it calls `plan_sync` and never `apply_sync`.

## Reader

`index.py` builds an in-memory index over every `*.md` file under the vault root.

Excluded: any path component starting with `.` (`.obsidian`, `.trash`, `.git`), files above `obsidian_index_max_file_kb`, and non-markdown files. Attachments, canvases, and bases are not read.

For files inside the memory folder, only the protected region below `<!-- jarvis:end -->` is indexed. The machine-written part is already in the graph and enriches replies through the graph path; indexing it too would inject the same fact twice into one system prompt.

Each entry holds the vault-relative path, the note title (H1 if present, else filename stem), frontmatter tags, mtime, and the body text. An entry is re-read only when its mtime or size changed; every other file on the tree is served from memory. 392 notes at ~2 MB is small enough that a full re-scan is a few milliseconds, so there is no persistent index file to corrupt or invalidate.

`get_vault_index(vault_root, memory_folder, max_file_kb)` is the process-wide cache: it returns the same `VaultIndex` instance for the same resolved vault root plus those two config knobs, building one only on first use. Both callers below (enrichment and the `vaultSearch` tool) go through it rather than constructing `VaultIndex` directly, so a vault of any size is walked once per process, not once per reply turn. The cache never goes stale, because the returned index still refreshes its own entries on every `search()` call: a note edited in Obsidian, or a file the mirror just wrote, is picked up on the next search against that same cached index. A config change to `obsidian_memory_folder` or `obsidian_index_max_file_kb` is a different cache key, so it gets a fresh index rather than one built under the old settings.

Search is keyword-based over title, tags, and body, ranked by number of distinct query terms matched, then by term frequency, then by recency of mtime. Matching is Unicode-aware, reusing the same NFKC + casefold folding as `normalise_fact` in `graph.py` so German umlauts and casing behave. There are no hardcoded language patterns; content words come from the existing extractor, and stop-wording is the reader's caller's job, exactly as it is for graph enrichment.

Results are returned as `VaultHit(path, title, snippet, score, provenance)`
where the snippet is the matching region padded to whole lines and capped at
300 characters. `provenance` carries the vault-relative path known by the
index at retrieval time.

### Reads Are Untrusted Input

Vault notes are user-authored, but they also contain pasted web content, forwarded email, and AI chat logs. Anything the reader injects into a system prompt is fenced as data, not instructions, using the same envelope convention as `web_search.spec.md`. A note containing "ignore your previous instructions" is quoted evidence about the user's files, not a directive.

### Enrichment Integration

The reader is a third enrichment channel alongside diary and graph in `engine.py` Step 4, gated by the same `needs_memory` planner signal and by `obsidian_read_enabled`.

It runs on the extractor's *keywords* rather than the implicit questions, because a vault is a topic index: unlike the knowledge graph, matching on a topic term is exactly the right retrieval mode for a folder of notes about topics. It shares the graph's minimum of two content words to avoid noisy single-term matches.

Hits are injected under their own heading without a title or file path:

```
Notes from the user's personal knowledge base (read-only files on their machine):
[Local vault note excerpt] ...
```

The vault-relative path remains attached to the local `VaultHit` and the
reply engine's `RetrievedSnippet`. It is rendered only by a deliberate
`vaultSearch` call or by `memoryProvenance` after the user asks for the origin.
This keeps folder names out of ordinary cloud-routed FAST and CHAT prompts.
`memoryProvenance` calls and results do not enter tool carryover, and a path in
its visible answer is replaced with a placeholder before that answer is stored
in the hot conversation window.

Raw hits also feed `digest_memory_for_query` for small models, alongside the existing diary and graph parts.

### vaultSearch Tool

`vaultSearch` exposes the same index for deliberate lookups.

| Property | Value |
|----------|-------|
| Name | `vaultSearch` |
| Schema | `{"query": string (required), "limit": int (optional, default 5, max 20)}` |
| Returns | Ranked hits with path, title, and snippet, in the untrusted-data fence |

The tool is read-only and has no write, create, or delete operation. It returns raw data with no LLM processing, per the tool contract. It is unavailable (not registered) when `obsidian_vault_path` is unset.

Full-note reads go through the existing `localFiles` tool, which already fences reads to the home directory. `vaultSearch` does not duplicate it.

## Privacy

The vault is the most sensitive store Jarvis touches: it is the user's private notes about school, applications, health, and projects. Three consequences:

- Selected vault snippets can reach the configured FAST or CHAT route for
  memory distillation and reply synthesis. Exact note titles and paths stay
  local unless the user deliberately invokes `vaultSearch` or asks for memory
  provenance.
- Vault snippets are subject to the same redaction pass as any other prompt content before they reach a model.
- The reader is a pure `Path.read_text`. There is no upload, no telemetry, no index shipped anywhere, and no dependency on Obsidian Sync, iCloud, or any account.

## Invariants

These hold regardless of config, LLM output, or graph state:

1. Jarvis writes only inside `<vault>/<obsidian_memory_folder>` (including its `_quarantine` subfolder), verified after path resolution.
2. Jarvis never modifies or deletes a file lacking `jarvis_managed: true`, even inside its own folder.
3. Content below `<!-- jarvis:end -->` survives every rewrite and every delete: a note is only ever unlinked once that content is empty, otherwise it is quarantined, never destroyed.
4. `obsidian_write_mode` defaults to `dry_run`; writing requires an explicit config change.
5. A vault failure never fails a graph write, a diary write, or a reply.
6. The graph in SQLite is authoritative; markdown is never parsed back into graph state.
