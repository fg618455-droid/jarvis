# Knowledge Graph Specification

## Overview

A self-organising node graph that stores the assistant's accumulated world knowledge — anything learned during conversations that it wouldn't already know from training data. This includes user-specific facts, real-world discoveries (opening hours, local businesses), practical knowledge (recipes, solutions), and current events. The diary records *what happened*; the knowledge graph records *what was learned*.

The graph dynamically structures knowledge by topic relevance using a hierarchical tree where nodes auto-split when they grow too large. Three fast-access entry points — **recent nodes**, **top nodes**, and **root node** — ensure the most relevant knowledge is always reachable without exhaustive search.

## Fixed Top-Level Branches

On first bootstrap the graph seeds three non-deletable branches under root, defined in `FIXED_BRANCHES` in `graph.py`:

| Branch ID | Name | Purpose |
|-----------|------|---------|
| `user` | User | Everything about the user: identity, location, tastes, habits, history |
| `directives` | Directives | Imperatives the user issued at the assistant: reply style, tone rules, standing instructions |
| `world` | World | External facts the assistant has learned: discoveries, practical knowledge, current events |

These branches are created idempotently via `INSERT OR IGNORE` on stable IDs. The structure is intentionally shallow and purpose-driven — splits deepen each subtree over time, but the top layer stays fixed so the **warm profile** (see below) has a stable shape.

No Other branch: the extractor defaults unknown classifications to `world`. A `user` fact is loaded into every future prompt as an established truth about the person, so an unclear or misclassified fact — including overheard/third-party content the extractor failed to route correctly — is filed as general knowledge instead.

### Legacy-Shape Migration (destructive)

`GraphMemoryStore.migrate_legacy_shape()` checks the on-disk graph against the expected shape at daemon start-up. The graph is considered non-conforming if root has any direct child that isn't one of the fixed branches, or if root's own `data` column is non-empty (cold-start writes that landed on root before the taxonomy existed). In either case the entire `memory_nodes` table is wiped and root + the three fixed branches are re-seeded.

Why destructive: nodes outside the fixed taxonomy would remain invisible to the warm profile forever. Carrying them as dead weight is worse than a clean slate. The diary is untouched, so users can repopulate the graph through Import diary in the control centre's Memory view. Knowledge nodes are in beta — the structure and classification are stable but the extractor quality is still being tuned.

Called **only** from the daemon start-up path in `daemon.main()`. The control centre and reply engine instantiate `GraphMemoryStore` without triggering the migration, so opening the Memory view mid-session never wipes anything.

### Branch-Pinned Traversal

`find_best_node(..., branch_root_id=...)` skips the recent/top entry points and descends from the given branch root only. This prevents cross-branch contamination when routing extracted facts: a User fact cannot land in the World subtree just because a World node was recently touched.

## Warm Profile

`build_warm_profile(store, *, user_max_chars, directives_max_chars)` returns a `{"user": "...", "directives": "..."}` dict by walking the User and Directives subtrees breadth-first (ordered by each sibling's decayed access score) and concatenating node data up to the char caps. `format_warm_profile_block(profile)` always renders an explicit verified-memory state. With User facts, it uses denial-template mirroring (see CLAUDE.md): the heading literally occupies the semantic slot that small-model canonical denials refer to ("INFORMATION THE USER HAS SHARED IN PRIOR CONVERSATIONS"), and a grounding footer makes that supplied list the only authority for user facts. With no User facts, it renders `MEMORY_RECORD_COUNT: 0 (NONE RETRIEVED)`, forbids positive user-fact claims for the turn, and treats a stored-personal-context lookup as completed by that result. The response contract has exactly two actions: disclose the empty result and ask for details the user wants remembered. It excludes identity prerequisites or discovery, external verification or research, topic changes, affectionate address, jokes, and persona banter. This explicit absence state prevents a small model from filling an omitted section with plausible personal details or evading the lookup. Standing Directives keep their own mirrored heading.

The warm profile is injected into every reply's initial system message (see `reply/engine.py` Step 3.5) unconditionally and query-agnostically. Personalisation is the default when facts exist, while an empty graph produces the explicit zero-record state rather than an omitted block. Memory capability and anti-denial guidance are state-dependent: the model is told not to deny supplied persistent facts only when the block actually contains those facts. The block ends by reasserting the configured or mirrored reply-language rule so its English metadata cannot authorise a language switch. No LLM call is involved in composition; it is a pure SQLite read.

## Data Model

### MemoryNode

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID string | Unique identifier (root node has id `"root"`) |
| `name` | string | Human-readable label |
| `description` | string | 1-2 sentences used by traversal to decide which branch to explore |
| `data` | string | The actual memories held at this node |
| `parent_id` | UUID or null | Back-reference (null for root) |
| `access_count` | int | Total accesses (for top-nodes ranking) |
| `last_accessed` | ISO 8601 | For recent-nodes ranking |
| `created_at` | ISO 8601 | When the node was created |
| `updated_at` | ISO 8601 | Last modification time |
| `data_token_count` | int | Cached token estimate (len/4 heuristic) |

### Storage

SQLite table `memory_nodes` in the same database as the diary system. Schema is initialised automatically on first access. The root node is created if absent.

`GraphMemoryStore`'s own lock makes each individual method call atomic, but placing and merging a fact spans a read, an LLM round trip, and a write — not atomic on its own. The ambient digest worker, the reply-turn pipeline, and the control centre's manual node edit can all reach the same node concurrently, so `update_graph_from_dialogue()` (and the manual-edit endpoint) hold `memory.write_lock.MEMORY_WRITE_LOCK` for the full read-transform-write span, serialising mutations to one writer at a time. The diary's `update_daily_conversation_summary()` shares the same lock for the same reason.

### Entry Points

| Entry Point | Query | Purpose |
|-------------|-------|---------|
| Recent nodes | Last N accessed (excl. root) | Fast path for ongoing conversations |
| Top nodes | Highest decayed access score (excl. root) | Core knowledge domains |
| Root node | Single root | Full graph traversal for novel queries |

## Core Operations

### Create

New nodes are created with a name, description, optional data, and a parent_id (defaults to root). Token count is computed on creation.

### Read

Nodes can be fetched individually, as children of a parent, as a subtree (nested dict), or as graph data (flat nodes + edges for visualisation).

### Update

Any combination of name, description, and data can be updated. Token count is recomputed when data changes. `updated_at` is always refreshed.

### Delete

Any node except root can be deleted. Children are orphaned (parent_id set to NULL via FK). The UI should warn before deleting nodes with children.

### Touch

Increments `access_count` and updates `last_accessed`. Called automatically when a node is viewed in the UI or retrieved during query traversal.

### Mutation Listeners

The graph module exposes a small observer registry, `register_graph_mutation_listener(cb)` / `unregister_graph_mutation_listener(cb)`, invoked after every successful `create_node`, `update_node`, `delete_node`, and (transitively) `append_to_node`. Callbacks receive `action`, `node_id`, and `branch` (the FIXED_BRANCH ancestor id, or `None` for root-level mutations and unresolvable nodes). Listener exceptions are logged via `debug_log` and swallowed so they cannot break a write.

Touch is intentionally NOT a mutation event: it changes access metadata only, not the warm-profile-relevant fields, so it does not need to invalidate caches.

The reply layer uses this hook from `daemon.py` to invalidate `DialogueMemory`'s warm-profile cache when the User or Directives branches change mid-conversation. World-branch writes are filtered out because the warm profile does not include the world branch.

### Access Decay

All ordering by access frequency uses a **time-decayed score** computed at query time: `access_count / (1 + age_days / half_life)`. This is hyperbolic decay — a node's effective score halves every `DECAY_HALF_LIFE_DAYS` (default 14) since its last access. The raw `access_count` is never modified, so changing the half-life retroactively reweights all nodes. This applies to `get_top_nodes`, `get_children`, `get_all_nodes`, and `search_nodes` tie-breaking.

### Search

- **search_nodes(query, limit)** — Keyword search across name, description, and data fields. Case-insensitive LIKE matching; nodes matching more keywords rank higher. Excludes root. Touches matched nodes for access tracking.
- **find_node_by_name(name, parent_id)** — Exact name match (case-insensitive), optionally scoped to a parent node. Excludes root when no parent specified.

## Tree & Graph Queries

- **get_subtree(node_id, max_depth)** — Nested dict for tree sidebar
- **get_ancestors(node_id)** — Path from root to node (breadcrumb)
- **get_graph_data(root_id, max_depth)** — Flat {nodes, edges} for canvas rendering. Each node includes depth and has_children flags.

## Auto-Split (Natural Reduction)

Triggered automatically when `data_token_count > SPLIT_THRESHOLD` (1500 tokens) after a write. Auto-split is the system's primary consolidation and pruning mechanism — it's where temporal events get distilled into patterns, common knowledge gets dropped, and the tree structure deepens organically.

1. LLM analyses the node's data and proposes 2-5 child categories
2. Each fact is assigned to exactly one child
3. **Consolidation**: duplicate facts are merged, and repeated similar activities across different dates are consolidated into patterns (e.g. "ate sushi on Mon, ate sushi on Thu" → "regularly eats sushi"). Date context is preserved only for significant events.
4. **Pruning**: facts that the LLM already knows from its training data are dropped. This keeps the graph as a delta from the model's baseline knowledge. When migrating to a newer model with broader training data, subsequent splits will naturally prune more — reducing the graph's memory footprint over time.
5. Child nodes are created under the split node
6. Parent data is cleared; parent description updated to a summary

This means the tree depth itself encodes a raw→refined spectrum: surface-level nodes hold recently ingested knowledge, deeper nodes hold distilled novel knowledge that survived multiple split cycles. Model upgrades naturally shrink the graph as previously-novel facts become common knowledge.

Split quality safeguards:
- Minimum 2 categories required (abort if LLM proposes fewer)
- Each category must have at least one fact
- If the split fails (LLM error, bad JSON), the node retains its data and the next write retries

## Auto-Merge (Future — requires LLM integration)

When all children collectively hold < MERGE_THRESHOLD (200 tokens):

1. Collapse children's data back into parent
2. Delete child nodes
3. Update parent description
4. Cascade summaries upward

## Housekeeping (Future)

Periodic process that:
- Promotes buried-but-hot nodes (high access, depth > 3)
- Compresses cold branches (no access in > Y days)
- Merges sparse subtrees
- Validates parent summaries

## LLM Integration

The graph memory system is fully automatic — no tool calls required. It integrates at two points in the existing pipeline.

### Automatic Writes (via `graph_ops.py`)

Piggybacks on the existing diary update flow in `conversation.py`:

1. After a successful diary update, the conversation summary is passed to `update_graph_from_dialogue()`
2. **Extract + classify**: LLM extracts novel knowledge from the summary and classifies each fact into one of the three fixed branches (`USER` / `DIRECTIVES` / `WORLD`). Output is a JSON list of `{"branch": "...", "fact": "..."}` objects. Rough routing heuristic baked into the prompt: if the user is *telling the assistant how to behave* → DIRECTIVES; if the user is *telling the assistant about themselves* → USER; if the assistant *discovered a fact about the world* → WORLD. Overheard or reported speech about a third party (as produced by the ambient digest, see `passive_capture.spec.md`) must route to WORLD, never USER. Unknown branches also default to WORLD. Requests are reframed as knowledge ("user asked about CEX hours" → "CEX Kensington closes at 6pm on Sundays"). Patterns and consolidation emerge through auto-split.
3. **Traverse**: Each fact is placed in the best-fitting node using branch-pinned descent from its tagged branch root (recent/top shortcuts are skipped so cross-branch contamination is impossible):
   - **Recent nodes** — checked first; follows conversational momentum
   - **Top nodes** — checked second; matches frequently accessed knowledge domains
   - **Root traversal** — greedy top-down descent; LLM picks the best child at each level, or stops at the current node if none fit
   - **Picker model**: `update_graph_from_dialogue` / `find_best_node` / `_llm_pick_best_child` accept an optional `picker_model` override. Callers (daemon, control centre diary-import endpoint) resolve it via `resolve_model(cfg, Tier.FAST)` so the best-child classification runs on the small warm fast model instead of the big chat model. When `picker_model` is `None` the picker falls back to the chat model.
4. **Dedupe (fast-path)**: Before any LLM call, `GraphMemoryStore.node_contains_fact` compares the fact against each line of the chosen node's data under Unicode-aware folding (`unicodedata.NFKC` + `str.casefold` + whitespace collapse), so ASCII casing, locale quirks (Turkish `İ`/`ı`, German `ß`/`ss`), and incidental whitespace don't cause false negatives. Exact matches are skipped, **not** reported as newly learned, and do **not** touch the node's access score (a re-extraction isn't fresh reinforcement). The merge step below would also collapse re-extractions, but cumulative daily summaries re-emit the same lines often enough that catching them with a cheap SQL read avoids a flood of small-model calls — semantically equivalent, just faster. Skips are still counted: `update_graph_from_dialogue` returns a `GraphUpdateResult(stored, skipped)` so the CLI can log "nothing new (N duplicates skipped)" on all-duplicate flushes; silencing that line would make the memory pipeline look broken. The check only covers the picker's chosen node, so a later flush that routes the same fact to a different node within the branch can still leak through — caught by the merge step on that node instead.
5. **Merge** (batched per node): `merge_node_data(store, node_id, new_facts: list[str], ...)` sends the existing node data + **all** new facts routed to that node in this flush to the picker model and asks it to produce a clean, consolidated, contradiction-free fact list, which is written back as the node's full `data`. The orchestrator groups the flush by `node_id` first so a 5-fact flush against the User node fires **one** rewrite that incorporates all five facts, not five separate rewrites of the same `data`. The call returns a `MergeResult(success: bool, incorporated_indices: list[int])` so the orchestrator can report only the facts that actually survived as new lines (consolidated-out facts aren't claimed as "newly stored"). One LLM call subsumes four behaviours: (a) **supersession** — contradictions, negations, and same-attribute updates drop the old line ("user does not need a daily check-in" replaces both "user has a need for a daily check-in" and the same need framed as an interest); (b) **near-duplicate dedupe** — different wordings of the same fact collapse to one canonical phrasing; (c) **consolidation** — repeated daily activities fold into patterns ("ate sushi on Monday", "ate sushi on Thursday" → "regularly eats sushi"); (d) **meta-narrative pruning** — lines that narrate the assistant's own behaviour, capabilities, or denials ("The assistant is unable to navigate to a web page", "The assistant suggested grilled salmon") are extractor artefacts from earlier prompt versions and get dropped. Counterpart to the extractor's BANNED FACT FORMS list: the extractor blocks them at write-time, the merge prompt scrubs the historical leftovers that a `consolidate-all` sweep can then surface. Genuine user-issued imperatives ("Always reply in British English") are not meta-narrative and survive. Independent facts coexist (a "user ate a Big Mac" line does not silently drop "user is vegetarian"; the contradiction stays visible). Because the latest prompt always rewrites the whole node, updated conventions propagate to old data without a separate migration. **Hallucination guard**: the rewrite is rejected if it returns more lines than `len(existing) + len(new) + 2` — a runaway model can't quietly inflate the node. Fail-open: empty/cold node, LLM error, parse failure, oversized rewrite, or an empty rewrite all fall back to plain `append_to_node` for each new fact so they still land — a contradiction is recoverable, a silent wipe or hallucinated bloat is not.
6. **Split**: If the merge or fallback append pushes the node past `SPLIT_THRESHOLD`, auto-split is triggered

Cold start: each fact lands directly on its tagged branch root (User / Directives / World) until enough data accumulates there for the first auto-split. The tree structure emerges organically under each branch.

LLM failure at any step is non-fatal — the diary update still succeeds, and the graph simply misses that cycle.

### Automatic Reads (via enrichment in `engine.py`)

At the start of each reply cycle, the reply engine enriches the system prompt with graph context:

1. **Question-driven**: Graph enrichment runs only when the query generator produced implicit personal questions. Utility queries (time, maths) and queries whose context is already live skip the graph entirely — the knowledge graph is a Q&A index, not a topic index.
2. **Question search**: Questions are joined, stop-worded, and used to find matching nodes (up to 5 results with data previews).
3. Results are injected as "Stored knowledge about the user" — separate from diary history to preserve provenance.

No tool calls needed. The LLM sees relevant graph memories as part of its system context.

Controlled by `memory_enrichment_source` config:
- `"all"` — both diary and graph enrich replies
- `"diary"` — only diary (conversation summaries) used for enrichment
- `"graph"` — only graph (structured knowledge) used for enrichment

Default is `"all"` — both channels enrich replies. The graph has graduated from alpha to beta with the purpose-driven taxonomy and warm profile now always-on, so the default flipped from `"diary"` to include graph recall too. Both systems always receive writes regardless of this setting.

Note: the always-on warm profile (User + Directives injected on every turn) is separate from query-driven enrichment. Warm profile covers "who the user is"; enrichment covers "what the user has said/seen about this specific topic". The graph contributes to both.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `SPLIT_THRESHOLD` | 1500 | Tokens before auto-split |
| `MERGE_THRESHOLD` | 200 | Tokens below which children collapse |
| `RECENT_NODES_COUNT` | 10 | Recent nodes to surface |
| `TOP_NODES_COUNT` | 15 | Top nodes to surface |
| `TOP_NODES_WINDOW_DAYS` | 30 | Legacy — kept for API compat, no longer used for filtering |
| `DECAY_HALF_LIFE_DAYS` | 14 | Days until a node's access score halves |
| `MAX_TRAVERSAL_DEPTH` | 8 | Safety limit on graph traversal |
| `SUMMARY_MAX_LENGTH` | 300 | Max chars for node description |
| `memory_enrichment_source` | `"all"` | Which system enriches replies: `"all"`, `"diary"`, or `"graph"` |

## UI: Control Centre Memory View

The graph explorer occupies the upper part of the control centre's Memory
view. A two-column layout places the full tree on the left and the selected
node editor on the right. Tree rows show each node's decayed access weight;
selecting a row opens its name, description, contents, ancestry, access count,
token count, and timestamps. The editor can save a node, add a child, or delete
a non-preset subtree.

The Maintenance section below the explorer contains Import diary and
Consolidate graph. Both consume streaming NDJSON and show processed counts,
live progress, and an action-specific completion summary. Consolidate graph
requires confirmation that populated node contents will be rewritten.

### API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/graph/nodes` | Graph data (nodes + edges) for canvas |
| GET | `/api/graph/tree` | Nested tree structure for sidebar |
| GET | `/api/graph/node/<id>` | Single node + children + ancestors |
| POST | `/api/graph/node` | Create new node |
| PUT | `/api/graph/node/<id>` | Update node fields |
| DELETE | `/api/graph/node/<id>` | Delete node (not root) |
| GET | `/api/graph/recent` | Recently accessed nodes |
| GET | `/api/graph/top` | Most frequently accessed nodes |
| GET | `/api/graph/stats` | Node count and total data tokens (`total_tokens = 0` means the graph holds no knowledge) |
| POST | `/api/graph/import-diary` | Import all diary summaries into graph (streaming NDJSON) |
| POST | `/api/graph/consolidate-all` | Self-consolidate every populated node (streaming NDJSON) — runs the merge LLM with no new facts on each node so the current conventions and supersession rules apply to all stored data |

### Import diary

The Import diary action feeds every diary summary through the standard
`update_graph_from_dialogue()` pipeline (extract → traverse → append → split).
The endpoint streams `start`, `progress`, `complete`, and `error` NDJSON events
so the action card stays responsive throughout the run. A failure on one
summary is reported in its progress event and does not stop later summaries.

### Consolidate graph

The Consolidate graph action walks every populated node and calls
`merge_node_data` with an empty `new_facts` list, applying the current
supersession, deduplication, consolidation, and pruning rules to the complete
node contents. The endpoint streams NDJSON progress per node. The control
centre confirms before starting and reports the total line-count delta on
completion.

## Relationship to Existing Systems

The graph memory system lives alongside the existing diary system (conversation_summaries + FTS + vector search). It shares the same SQLite database but uses its own table. The diary system remains the primary memory system for now; the graph is a v2 system being built in parallel.

Users can feed all diary summaries into the graph through Import diary in the
control centre's Memory view. The extract-and-place pipeline builds and updates
the graph structure organically.

### Diary Summariser Hygiene

Graph extraction ingests diary summaries, so the graph inherits whatever corruption the summary contains. Summariser hygiene rules (no deflection narration, attribution preservation, topic separation) are documented in [`summariser.spec.md`](summariser.spec.md).

## Privacy

All data is stored locally in the user's SQLite database. No data leaves the device. The graph store has no network dependencies.
