# LLM Contexts Map

Every distinct LLM call in Jarvis, what feeds it, what consumes it, and how it is gated. This is the reference for optimising the app's main bottleneck (LLM latency). Keep it in sync with the code — see the note at the bottom.

> **Backend abstraction and lanes.** Every completion below enters `get_llm_backend(cfg)` and selects FAST, CHAT, or PRIVATE through `resolve_model(cfg, tier)`. Configured FAST and CHAT chains use generic endpoint routes followed by loopback Ollama. PRIVATE contains loopback Ollama only. `get_embedding_backend(cfg)` is separate, always local Ollama, and never routed.

---

## 1. Main Reply Loop (agentic messages loop)

- **File**: [src/jarvis/reply/engine.py](src/jarvis/reply/engine.py) — `reply()` and the loop at ~lines 1370-1650; native tool-call path in `chat_with_messages()` (~1424, 1455).
- **Trigger**: every user message. Runs up to `agentic_max_turns` (default 8) iterations per reply.
- **Model / gating**: CHAT tier. The route-specific model and timeout come from the ordered CHAT chain. Not optional. No size branching on the loop itself; size branching affects the digests around it.
- **Inputs**:
  - Redacted user query
  - Recent dialogue (last 5 minutes), including in-loop tool-call + tool-role messages from prior replies within the active conversation (tool carryover, `DialogueMemory.record_tool_turn` / `get_recent_turns_with_tools` in [src/jarvis/memory/conversation.py](src/jarvis/memory/conversation.py); per-prompt cap via `cfg.tool_carryover_max_turns` / `tool_carryover_per_entry_chars`; storage cap `_tool_turns_max_storage = 16`; cleared on `stop` signal AND on new-conversation entry; UNTRUSTED WEB EXTRACT fence markers preserved on truncation; both `content` and `tool_calls[*].function.arguments` scrubbed on write)
  - Unified system prompt from [src/jarvis/system_prompt.py](src/jarvis/system_prompt.py) + ASR note + tool-protocol guidance. The persona contains no concrete example user facts or denial-mirroring heading; those appear only in the dynamic warm-profile state below.
  - **Warm profile block** (query-agnostic User + Directives excerpt from the knowledge graph, composed by `build_warm_profile()` / `format_warm_profile_block()` in [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) at Step 3.5 of `reply()`; no LLM call, pure SQLite read; injected unconditionally. A populated User branch renders the denial-mirroring heading, the real stored facts, and a rule making that list the only authority for user claims. An empty User branch renders `MEMORY_RECORD_COUNT: 0 (NONE RETRIEVED)` and forbids positive user-fact claims rather than leaving an ambiguous omission. Anti-denial guidance exists only in the populated shape. Both shapes finish with the configured or mirrored reply-language decision and a no-switch rule. The result is cached in `DialogueMemory._hot_cache` under `DialogueMemory.WARM_PROFILE_CACHE_KEY` for the lifetime of the active conversation. Invalidated on `stop`, on new-conversation entry, AND on User/Directives graph mutations via the listener registered in [src/jarvis/daemon.py](src/jarvis/daemon.py) against `register_graph_mutation_listener` in [src/jarvis/memory/graph.py](src/jarvis/memory/graph.py); World-branch writes are ignored)
  - Digested memory enrichment (optional, see #4)
  - Time + location context (computed once per reply, placed at the END of the system message's dynamic region — never the head — so every in-loop call sends a byte-identical system message and the server's KV/prefix cache can reuse the whole prompt head; in text-tools mode it sits just before the tool-call syntax guidance so the instruction block stays final)
  - Tool schema: native via `generate_tools_json_schema()` ([src/jarvis/tools/registry.py](src/jarvis/tools/registry.py)) or text fallback via `_text_tool_call_guidance()` ([engine.py:68](src/jarvis/reply/engine.py:68))
  - Tool results from prior turns (raw or digested — see #5)
- **Output**: OpenAI-style `{content, tool_calls, thinking}`. Consumed by the tool orchestrator and TTS pipeline. Natural-language content is delivered immediately; no post-turn evaluator runs. In text-tool mode, the syntax example names only a tool from the current allow-list. A `tool_calls:` protocol literal returned as content is malformed whether it is bare or follows a prose preface on a later line, and is blocked before delivery.
- **Automatic crew edge**: gated on `crew_handoff_enabled` (default off — the confirmation wait below is not yet bounded to the deadline, so an unattended escalation can cost the full confirmation timeout for a refusal instead of an answer). When on, a `TurnTrace` is active, and both existing crew Telegram settings are configured, the trace's monotonic origin governs a direct edge from this loop to `askCrew`. At 3 seconds the edge fires unless the router made a positive no-tool decision, every local tool step has a result and only synthesis remains, or a complete natural-language response has arrived. At 5 seconds it fires regardless. The engine calls `run_tool_with_retries` with `agent="jarvis"` and the redacted request, so the critical confirmation gate and ordinary askCrew transport remain in force. This edge adds no LLM call and bypasses only the model's tool-choice decision. A handoff discards local content and returns the fire-and-forget acknowledgement as the turn's only reply; the crew result arrives later through Telegram or the shared vault.
- **Limits**: `num_ctx: 8192` (explicit). Timeout `llm_chat_timeout_sec` (180s default). A configured route chain's appended local CHAT candidate has the same 180-second route limit, while the caller may impose a smaller budget. With automatic crew handoff available, each main chat call is capped by the remaining 3-second decision budget, or the remaining 5-second hard budget when the structural close-to-done predicate is true. Router, embedding-router, planner, resolver, memory extractor, retrieval, and digest calls share the same applicable remainder. Auto-fallback from native to text tool-calls on HTTP 400 (`ToolsNotSupportedError`), sticky for the session. Risk: `fetch_web_page` truncates at 50,000 chars (~37k tokens) — mitigated for SMALL models by tool-result digest (#5) which compresses the payload before it enters the messages history. LARGE models receive the raw payload and may silently see a truncated context.
- **Text-chat entry**: The desktop `ChatWindow` (see `src/desktop_app/chat_window.spec.md`) submits via `jarvis.daemon.submit_text_query`, which calls this same context on a worker thread with `tts=None` and `language=None` (no Whisper-detected language for typed input). Voice and text share the global `DialogueMemory` so they are one conversation. No new LLM context is introduced — the planner, router, enrichment, and digests all run unchanged. Text chat never speaks; the reply is returned to the UI via callbacks (bundled) or `__CHAT__:` IPC events (subprocess).
- **Telegram entry**: a message from the configured chat submits through the same `jarvis.daemon.submit_text_query` (see `src/jarvis/telegram/telegram.spec.md`), so it shares the global `DialogueMemory` with voice and text and introduces no new LLM context. Gated on `telegram_chat_enabled`; the reply is sent back to the chat rather than spoken.

## 2. Intent Judge

- **File**: [src/jarvis/listening/intent_judge.py](src/jarvis/listening/intent_judge.py) — `IntentJudge.evaluate()`.
- **Trigger**: on a speech segment only when context is needed: an interior wake-name occurrence or hot-window input. A wake name at the first or last token dispatches deterministically after Whisper, and playback is a closed listening interval, so both paths skip this call. Pure ambient speech also skips it.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)` via `get_llm_backend(cfg).chat(...)`. Provider-aware default at config load: `gemma4:e2b` (~2B) on the Ollama chat path; on an OpenAI-compatible chat provider an unset `fast_model` resolves to the active `llm_chat_model` (the Ollama pull-name does not exist on the user's server). An explicit `fast_model` in config.json wins on both paths. The backend re-raises `ConnectionError` so the judge can apply a 30s cooldown after the server actively refuses; falls back to text-based wake detection while the cooldown is active.
- **Inputs**:
  - Rolling transcript buffer (last 120s, with timestamps)
  - Wake-word timestamp (if any), normalised aliases
  - Last TTS text + finish time (echo rejection)
  - State flags (wake_word_mode, hot_window_mode)
- **System prompt**: `SYSTEM_PROMPT_TEMPLATE` at [intent_judge.py:135](src/jarvis/listening/intent_judge.py:135). Teaches query extraction, echo detection, stop commands, pronoun/topic disambiguation, imperative re-addressing, declaratives to the wake word.
- **Output**: strict JSON `IntentJudgment{directed, query, stop, confidence, reasoning}` ([intent_judge.py:94](src/jarvis/listening/intent_judge.py:94)). Consumed by the listening state machine which dispatches to the reply engine. When `content` is empty **or truncated mid-JSON** (reasoning models count thinking tokens against the generation cap), the judge also recovers the JSON answer from `reasoning_content` — reasoning models typically end their thinking with the full structured answer.
- **Limits**: `intent_judge_timeout_sec` (6s). `num_ctx: 8192` (explicit; the system prompt is ~2k tokens and the rolling transcript buffer at default `transcript_buffer_duration_sec=120` can reach ~1.5k tokens in chatty multi-speaker scenes; the larger window gives the few-shot examples and TRANSCRIPT NOISE block at the tail of the prompt enough headroom on Ollama). `max_tokens: 1500` (canonical cap — covers reasoning + answer on reasoning models; OpenAI-compatible backends get it at the payload root; Ollama maps it to `num_predict`). Ollama-only knobs (`keep_alive`, `num_ctx`, `num_predict`) flow via `extra_options`; OpenAI-compatible backends silently drop them. `keep_alive` is `"30m"` by default and `"1m"` when `low_power_mode` is true.

## 3. Memory Enrichment Extractor

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `extract_search_params_for_memory()` (~line 71).
- **Trigger**: once per reply only when the enabled pre-flight planner (#12) emitted a `searchMemory` directive or returned an empty plan through its fail-open path. A disabled planner and a positive reply-only plan both skip this call.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)`. Factory-dispatched. Small classification task; rides the same small/warm model as the router. Silent empty-dict on failure (early-return when no chat model is configured — no wasted LLM round-trip).
- **Inputs**: user query (with the planner's `topic` hint appended when present), optional context hint (live-context compact summary) or UTC-now anchor, both carried in the USER message.
- **System prompt**: inline at [enrichment.py:35-63](src/jarvis/reply/enrichment.py:35). Byte-static — no hint block, no timestamp — so the system prompt is identical across every extractor call and stays cacheable; the per-call hint / UTC anchor rides at the end of the user content.
- **Output**: `{keywords, from?, to?, questions?}`. Consumed by memory search in the reply engine.
- **Limits**: up to 2 retries; timeout from `llm_tools_timeout_sec`. `max_tokens: 50`.
- **Caching**: result cached in `DialogueMemory._hot_cache` under key `enrichment:{redacted_query[+topic_hint]}` for the lifetime of the active conversation. Identical follow-ups within the same conversation reuse the dict and skip the LLM hop. Cleared by `clear_hot_cache()` on the `stop` signal and on new-conversation entry.

## 3b. Recall Gate (pre-enrichment short-circuit)

- **File**: [src/jarvis/memory/recall_gate.py](src/jarvis/memory/recall_gate.py) — `should_recall()`.
- **Trigger**: once per reply, before diary/graph/digest enrichment runs (after the planner has decided memory is potentially needed).
- **Model / gating**: NO LLM — deterministic keyword-coverage heuristic. Cheap.
- **Inputs**: query, recent dialogue (incl. tool carryover rows).
- **Output**: `False` only if hot-window contains a fresh tool result AND ≥50% of the query's content words appear in the hot-window transcript → skips diary, graph, and memory digest for this reply. Else `True`. Fail-open on any exception. Content-word extraction uses `\w{3,}` with `re.UNICODE`, so the gate works for Latin, Cyrillic, CJK, Arabic, Hebrew, etc. (per CLAUDE.md "no hardcoded language patterns"). Overlap words are run through `redact()` before being written to debug logs.
- **Planner precedence**: when the planner explicitly emitted a `searchMemory` step, the gate is bypassed — the planner has more signal than coverage and overriding it would silently drop intent. The gate only short-circuits the fail-open empty-plan path.
- **Rationale**: prevents re-running diary/graph lookups when the hot window already grounds the follow-up (e.g. "his most famous song" after a Bieber webSearch).

## 4. Memory Digest (optional, SMALL models)

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `digest_memory_for_query()` + `_distil_batch()`.
- **Trigger**: once per reply when enrichment returns hits AND `memory_digest_enabled` (default OFF; `null` = auto-ON for SMALL ≤7.5B / OFF for LARGE). Skipped if raw < `_DIGEST_MIN_CHARS` (400). Batched if raw > `_DIGEST_BATCH_MAX_CHARS` (2000).
- **Model / gating**: FAST tier via `resolve_model(cfg, Tier.FAST)`. Gated by `memory_digest_enabled`; the auto-on decision still follows the effective CHAT model size.
- **Inputs**: user query, raw diary entries, raw graph nodes.
- **System prompt**: `_DIGEST_SYSTEM_PROMPT` at [enrichment.py:122](src/jarvis/reply/enrichment.py:122). Teaches relevance filtering, preference-signal detection, attribution preservation, `NONE` sentinel, identity queries.
- **Output**: ≤400 chars text per batch (`_DIGEST_MAX_CHARS`) injected as reference-only memory context into the main loop's system message. Empty on failure.
- **Limits**: `llm_digest_timeout_sec` (8s, shared). `max_tokens: 200`.

## 5. Tool-Result Digest (optional, opt-in)

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `digest_tool_result_for_query()` + `_distil_tool_batch()`.
- **Trigger**: after each tool result in the loop, if `tool_result_digest_enabled` (default `null` = auto-ON for SMALL ≤7.5B, OFF for LARGE). Primary motivation on small models: prevents `fetch_web_page`'s 50k-char payloads from filling the 8192 num_ctx window. Skipped if raw < 400 chars (`_TOOL_DIGEST_MIN_CHARS`); batched if > 2500 (`_TOOL_DIGEST_BATCH_MAX_CHARS`).
- **Model / gating**: FAST tier via `resolve_model(cfg, Tier.FAST)`. Gated by `tool_result_digest_enabled`; auto-on remains based on `detect_model_size(cfg.llm_chat_model)`.
- **Inputs**: user query, tool name, raw tool result (e.g. webSearch payload inside UNTRUSTED WEB EXTRACT fence).
- **System prompt**: `_TOOL_DIGEST_SYSTEM_PROMPT`. Teaches attributed fact extraction, `NONE` sentinel, no inference.
- **Output**: ≤600 chars per batch (`_TOOL_DIGEST_MAX_CHARS`) replacing the raw payload in the messages stream. Falls back to raw on `NONE`.
- **Limits**: `llm_digest_timeout_sec` (8s, shared). `max_tokens: 300`.

## 6. Max-Turn Loop Digest

- **File**: [src/jarvis/reply/enrichment.py](src/jarvis/reply/enrichment.py) — `digest_loop_for_max_turns()` (~line 847).
- **Trigger**: when the loop exhausts `agentic_max_turns` without producing a natural-language reply (e.g. pure tool-call loop). The evaluator no longer drives this — termination on content is immediate.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)`. Factory-dispatched.
- **Inputs**: user query + loop activity (tool calls, results summaries, any prose).
- **System prompt**: `_LOOP_DIGEST_SYSTEM_PROMPT` — caveat-prefixed, user-language, concise.
- **Output**: caveat-prefixed final reply. Fails open to the last raw candidate or generic error.
- **Limits**: `llm_digest_timeout_sec` (8s, shared). `max_tokens: 200`.

## 7. Tool Router (pre-loop tool selection)

- **File**: [src/jarvis/tools/selection.py](src/jarvis/tools/selection.py) — `select_tools_with_llm()` (~line 331).
- **Trigger**: once per reply, **at the very front of the flow before the planner (#12)**. Always runs — the router is the authoritative tool picker, and its narrowed catalogue is what the planner sees. When the planner later references tools, those names are unioned into the router's allow-list but never replace it; small models tend to default to `webSearch` where a dedicated tool like `getWeather` should win, and the router is tuned for that classification. `tool_selection_strategy == "llm"` is the default; other strategies (`all`, `keyword`, `embedding`) also run here.
- **Model / gating**: FAST tier — `resolve_model(cfg, Tier.FAST)`. Factory-dispatched.
- **Inputs**: user query, tool catalogue (builtin + MCP with descriptions), optional narrow-down hint. User-prompt order is KV-cache-disciplined: the mostly-static catalogue opens, the dynamic hint (time + dialogue) follows, the query is the final token — consecutive router calls in one conversation share the full catalogue as prefix.
- **System prompt**: inline (~lines 260-315). Teaches pick up-to-5 tools or `none`.
- **Output**: comma-separated tool names or `none`. Capped at `_LLM_MAX_SELECTED` (5). Always-included tools (`stop`, `toolSearchTool`) are unioned in regardless.
- **Limits**: `llm_timeout_sec`. Local route limit 60 seconds, with any smaller caller budget taking precedence. `num_ctx: 8192` keeps an Ollama model shared by FAST and CHAT on the same resident runner as the main loop instead of rebuilding it between 4096 and 8192 contexts. `max_tokens: 50`. On failure → keyword strategy.
- **Caching**: `routed_tools` is cached in `DialogueMemory._hot_cache` under key `router:{redacted_query}|{strategy}|{builtin-names}|{mcp-names}` for the lifetime of the active conversation. For the embedding strategy, each static tool-description vector is also held in a bounded process cache keyed by backend endpoint, model, tool name, and summary. Voice startup warms that catalogue; live turns normally embed only the query. The dialogue catalogue signature lets a mid-conversation MCP refresh invalidate the routed selection, while a changed tool summary creates a new static-vector key.
- **Carry-over guard (engine-side overlay)**: after the cache lookup/write, the engine inspects the previous assistant turn's tool calls. When a previous tool reported `success=False` on its `ToolExecutionResult` (read via the `tool_failed` flag stamped onto each recorded tool result), that tool name is unioned back into the local `routed_tools` for this turn only. Compensates for small routers that misroute follow-ups where the user is supplying missing info (e.g. "I'm in London" routing to `webSearch` after a stalled `getWeather` chain). Successful chains do not carry over — a genuine new short ask after a completed chain keeps the router pick clean. The augmentation never touches the cache; replays of the same query in future turns get the raw router output. See `src/jarvis/reply/reply.spec.md` §6 (Tool allow-list per turn) for the full contract.

## 8. Tool Searcher (mid-loop escape hatch)

- **File**: [src/jarvis/tools/builtin/tool_search.py](src/jarvis/tools/builtin/tool_search.py) — `toolSearchTool`.
- **Trigger**: when the model explicitly invokes `toolSearchTool` during the loop. Capped at `tool_search_max_calls` (3) per reply.
- **Model**: reuses the tool router (#7) — no separate LLM call here.
- **Inputs**: self-contained query from the model.
- **Output**: newline-separated tool names + one-liners, merged into the allow-list for the next turn.

## 9. Conversation Summariser

- **File**: [src/jarvis/memory/conversation.py](src/jarvis/memory/conversation.py) — `generate_conversation_summary()` (~lines 350/355).
- **Trigger**: background, periodic when unsaved dialogue reaches `dialogue_memory_timeout`, plus the normal daemon shutdown path with `force=True`. `⚡ Stop Now (Skip Diary)` sets the daemon's shutdown skip flag, so this final forced pass is skipped while normal periodic saves remain unchanged. One summary is stored per day per `source_app`.
- **Model / gating**: PRIVATE tier via `resolve_model(cfg, Tier.PRIVATE)`. The chain is one loopback Ollama route. Respects `llm_thinking_enabled`. Uses streaming when a token callback is provided, else direct.
- **Inputs**: recent conversation chunks + prior same-day summary (for incremental update).
- **System prompt**: inline (~lines 310-320). Hygiene rules per [src/jarvis/memory/summariser.spec.md](src/jarvis/memory/summariser.spec.md): no deflection narration, attribution preservation, topic separation. The deflection rule (rule 6) is enumerated with concrete BAD/GOOD pairs in English plus parallel pairs in Turkish and Spanish so small models don't assume the rule is keyed to English phrasing. ≤200 words + 3-5 topic keywords.
- **Output**: `(summary_text, topics_text)` → `conversation_summaries` table, embedded for vector search, feeds enrichment (#3) and graph extraction (#10). No post-process scrub — the prompt is single-source-of-truth, language-agnostic, and improves automatically as the chat model upgrades.
- **Deflection rewrite (separate bulk op)**: `rewrite_all_diary_summaries()` (`POST /api/diary/scrub-deflections`) uses the PRIVATE tier for each row. Diary text is fenced as untrusted data, `ts_utc` is preserved, and changed rows are re-embedded locally on a best-effort basis.
- **Topic optimisation (separate bulk op)**: `optimise_diary_topics()` (`POST /api/diary/optimise-topics`) uses the PRIVATE tier to propose a normalised taxonomy, then applies it locally while preserving `ts_utc` and diary text.
- **Limits**: `timeout_sec` (30s default). `max_tokens: 400` on the direct (non-streaming) path so a full 200-word summary + TOPICS line is never truncated; the streaming path is uncapped.

## 10. Knowledge Graph Fact Extraction + Branch Classification

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) — `extract_graph_memories()`.
- **Trigger**: after each daily summary (#9). Background.
- **Model**: PRIVATE tier. Summary text and extracted graph facts never leave loopback.
- **Inputs**: summary text + optional date.
- **System prompt**: inline — asks for JSON array of `{"branch": "USER|DIRECTIVES|WORLD", "fact": "..."}` objects, with a heuristic ("user telling the assistant how to behave → DIRECTIVES; user telling the assistant about themselves → USER; external facts → WORLD"). Unknown branches default to USER. The DO-NOT-EXTRACT block hardens two recurring traps: assistant-generated recommendations (would-a-different-assistant-give-the-same-answer? heuristic separates these from external lookups, which DO count as facts) and transient snapshots like the current weather / time of day (described as "moments not facts" so the model stops conflating ephemera with persistent climate / location knowledge).
- **Output**: list of `(branch_id, fact_text)` tuples → routed into the tagged branch via branch-pinned descent (no cross-branch contamination).
- **Limits**: `timeout_sec`. Failures → empty list.

## 11. Knowledge Graph Best-Child Picker

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) — `_llm_pick_best_child()` (~line 167).
- **Trigger**: during graph insertion, per fact, to place it under the best existing category. Background.
- **Model**: FAST tier via the supplied `picker_model`; minimal callers without Settings fall back to their supplied chat model. Placement may receive the single fact and candidate category labels through a configured FAST endpoint.
- **Inputs**: fact text + numbered list of candidate child nodes (name + description).
- **System prompt**: inline (~lines 156-161) — answer with number or `NONE`.
- **Output**: child node id or `None` (fact still inserted, just not under an optimal parent).

## 11b. Knowledge Graph Node Merge (rewrite-on-write consolidation)

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py) — `merge_node_data()` (system prompt at `_MERGE_SYSTEM_PROMPT`).
- **Trigger**: **once per (node, flush)** during `update_graph_from_dialogue`. The orchestrator first applies the exact-match dedupe fast-path, then groups the remaining facts by their resolved `node_id` so a 5-fact flush hitting the User node fires one rewrite, not five. Cold-start writes (empty target node) skip straight to plain append. Also invoked with `new_facts=[]` by the `consolidate_all_populated_nodes` maintenance op (powering the memory viewer's 🧹 button) to re-apply current rules to historical data.
- **Model**: PRIVATE tier, always loopback Ollama. Temperature 0 because the task is rule-following classification.
- **Inputs**: existing node `data` + the batch of new facts (zero or more) routed to that node in this flush.
- **System prompt**: defines an ordered rule set — contradiction/reversal drops the old version, near-duplicate phrasings collapse to one, repeated daily activities consolidate into patterns, independent attributes coexist (visible contradictions are NOT silently dropped), common-knowledge facts are pruned. Demands a bare `{"facts": [...]}` JSON object. Parser tries direct `json.loads` first, then a scoped regex (no greedy `\{.*\}`) before giving up.
- **Output**: `MergeResult(success: bool, incorporated_indices: list[int])`. The revised fact list is written back as the node's full `data`; `incorporated_indices` tells the orchestrator which inputs survived as new lines (under NFKC + casefold matching) so consolidated-out facts aren't reported as "newly stored". Subsumes per-flush supersession, near-duplicate dedupe, and ongoing consolidation in a single call. Because the latest prompt rewrites the whole node, updated conventions propagate to old data without a separate migration step.
- **Limits**: 20s timeout. **Hallucination guard**: rewrites with more than `len(existing) + len(new) + 2` lines are rejected as runaway output. Fail-open on any error, parse failure, oversized rewrite, or empty rewrite → caller falls back to plain `append_to_node` for each new fact so they still land (a contradiction is recoverable; a silent wipe or hallucinated bloat is not).

## 11c. Knowledge Graph Auto-split

- **File**: [src/jarvis/memory/graph_ops.py](src/jarvis/memory/graph_ops.py): `auto_split_node()`.
- **Trigger**: after a graph node exceeds its token threshold.
- **Model / gating**: PRIVATE tier, always loopback Ollama.
- **Inputs**: the selected node's name, description, and stored facts.
- **Output**: two to five child categories plus a parent summary. Parsing or model failure leaves the node unchanged.
- **Limits**: 45s timeout and `max_tokens: 200`.

## 12. Task-list Planner (pre-flight decomposition, gates the whole turn)

- **File**: [src/jarvis/reply/planner.py](src/jarvis/reply/planner.py) — `plan_query()`.
- **Trigger**: once per reply, **after the tool router and before memory search**. Skipped when `cfg.planner_enabled = False`, when the query is shorter than `MIN_QUERY_CHARS` (4), when no model / base URL is available, or when the **engine-level fast-path skip** fires (the tool router returned no real tools AND the query is ≤ 8 words — the engine injects `["Reply to the user."]` as the plan without calling the LLM). A disabled planner also disables speculative long-term recall; an enabled planner that fails still returns an empty plan and takes the fail-open recall path.
- **Model / gating**: CHAT tier — `resolve_model(cfg, Tier.CHAT)`. Factory-dispatched. The planner tracks the active chat model so upgrading it (via setup wizard, config, or provider switch) automatically upgrades plan quality.
- **Inputs**: user query, dialogue context, **router-narrowed** tool catalogue (names + one-line descriptions) — not the full 30+ list. When the carry-over guard from #7 fires, the previous turn's failed tool name is unioned into this catalogue before the planner sees it, so the planner can plan a re-call without `toolSearchTool` round-tripping. **No** memory context — the planner decides *whether* memory is needed.
- **System prompt**: `_PROMPT_TEMPLATE` in `planner.py`. Teaches the `searchMemory topic='...'` directive for prior-conversation lookups, short imperative tool steps, angle-bracket entity placeholders, final synthesis step, same-language output, no numbering.
- **Output**: list of plan steps (max `MAX_STEPS` = 5). Gates memory enrichment (#3 / #4) and augments the tool router (#7 — planner's picks are unioned in, not replacing). Single-step `["Reply to the user."]` plans are the planner's positive "no memory, no tools" signal. An empty list is fail-open — the engine reverts to running #3 unconditionally. A **stop-only plan** (every step is `stop`) is also rejected by a deterministic post-plan guard and returns `[]` — same fail-open path as an LLM failure — so the engine falls through to the tool router and chat model rather than silently dismissing the conversation. Consumed further by the engine to build the `ACTION PLAN:` system-message block and drive the direct-exec loop (#13) for small models.
- **Limits**: `planner_timeout_sec` (3s). `max_tokens: 150`. Fail-open → `[]`. When automatic crew handoff is available, the engine passes the smaller time remaining to the 3-second decision point.

## 13. Plan Step Resolver (per direct-exec turn, small models)

- **File**: [src/jarvis/reply/planner.py](src/jarvis/reply/planner.py) — `resolve_next_tool_call()`.
- **Trigger**: top of each agentic-loop iteration when `use_text_tools` is True, the plan from #12 still has unexecuted tool steps, AND the plan is not under-specified (`plan_has_unresolved_tool_steps` returns False — steps that paraphrase tools without naming them skip direct-exec so the resolver doesn't guess arguments). Runs instead of the chat model for that turn. **Fast path skips the LLM entirely** when the step is fully concrete (tool name + `key='value'` args, no `<placeholder>`); the LLM call only fires when entity substitution or key remapping is needed.
- **Model**: CHAT tier, the same chain as #12.
- **Inputs**: next planned step text, prior tool calls (name + args + result excerpt), per-turn tool schema.
- **System prompt**: `_STEP_RESOLVER_SYSTEM` at [planner.py:300](src/jarvis/reply/planner.py:300). Teaches one-JSON-object output, placeholder substitution from prior results, `null` for synthesis steps.
- **Output**: `(tool_name, arguments)` tuple or `None`. Unknown tool names are rejected via the allow-list guard.
- **Limits**: `planner_timeout_sec` (3s). `max_tokens: 100`. Fail-open → `None` (engine falls back to the chat-model turn). When automatic crew handoff is available, the engine passes the smaller time remaining to the 3-second decision point and checks the same trace before executing the resolved tool.

## 14. Tool-specific LLM calls

- **Weather** ([src/jarvis/tools/builtin/weather.py](src/jarvis/tools/builtin/weather.py), ~line 60) — factory-dispatched. Place extraction is a FAST-tier pass (`resolve_model(cfg, Tier.FAST)`) so small/warm models handle the parse without paging in the chat model. `max_tokens: 50`. Parses location/time/unit from the query.
- **Nutrition log_meal** ([src/jarvis/tools/builtin/nutrition/log_meal.py](src/jarvis/tools/builtin/nutrition/log_meal.py), lines 48 & 136) uses the CHAT tier. Extractor `max_tokens: 200`, follow-up `max_tokens: 100`. Extracts nutrients and confirms logging.

## 15. Server Capability Probe (setup-time, OpenAI-compatible only)

- **File**: [src/jarvis/llm/openai_compatible.py](src/jarvis/llm/openai_compatible.py) — `OpenAICompatibleBackend.check_capabilities()`. Called from the setup wizard's `_CapabilityWorker` ([src/desktop_app/setup_wizard.py](src/desktop_app/setup_wizard.py)).
- **Trigger**: not part of the runtime pipeline. Fires when the user clicks **Connect** on the OpenAI-compatible wizard page (once per connection attempt). The desktop startup reachability check (`_check_openai_compat_reachable` in [src/desktop_app/app.py](src/desktop_app/app.py)) uses only `list_models`, not this probe.
- **Model / gating**: the chat model the user selected on the page (and the selected embedding model, if any). Off the UI thread.
- **Inputs**: a fixed `"ping"` message; a trivial no-op tool schema; a `"ping"` embedding input. No user or memory data.
- **Output**: `ServerCapabilities{reachable, chat, tools, embeddings, models}`. Consumed only by the wizard to render an honest capability summary and offer the Ollama-embeddings fallback. Never persisted.
- **Limits**: `timeout_sec` default 8s per sub-request. Issues up to two `/chat/completions` calls (plain + tool), one `/embeddings`, one `/models`. Fail-soft: every error collapses to a `False` flag; a `ConnectionError` short-circuits to `reachable=False`.

## 16. Route Model Probe

- **Files**: [src/jarvis/llm/probe.py](src/jarvis/llm/probe.py) and [src/jarvis/webui/api/llm.py](src/jarvis/webui/api/llm.py).
- **Trigger**: `python -m jarvis.llm.probe`, `scripts/import_fcc_keys.py`, or the control centre's explicit **Probe models** action. Loading the LLM routes view does not trigger it.
- **Model / gating**: no completion model. It performs `GET /models` against each configured generic endpoint with the route timeout.
- **Inputs**: endpoint URL and bearer credential only. No conversation, memory, prompt, or user content.
- **Output**: advertised model names and safe exception-class labels. Credentials never appear in CLI output, logs, API responses, or the probe catalogue.
- **Persistence**: the CLI writes model catalogues to `~/.jarvis/llm_probe.json` with mode `0o600` where supported.

---

## 17. Ambient Digest

- **File**: [src/jarvis/memory/ambient.py](src/jarvis/memory/ambient.py) — `generate_ambient_digest()` and `process_ambient_digest_once()`.
- **Trigger**: background, every `passive_digest_interval_min` (default 15) while passive capture is enabled. The worker does not exist while the switch is off. Each pass takes the oldest UTC day with eligible lines and processes at most one bounded batch.
- **Model / gating**: `cfg.llm_chat_model` via `get_llm_backend(cfg).direct(...)`, outside the tiered route chain. Gated by the live `passive_capture_enabled` switch. Addressed and already-digested lines are excluded before the call.
- **Inputs**: up to `passive_digest_max_lines` (default 120) raw passive transcript lines from one UTC day. Every line is passed through `utils.redact.py`, timestamped, and wrapped in `<<<BEGIN UNTRUSTED WEB EXTRACT>>>` / `<<<END UNTRUSTED WEB EXTRACT>>>` before reaching the model.
- **System prompt**: `_AMBIENT_DIGEST_SYSTEM_PROMPT`. Enforces overheard provenance, permits an empty result as the ordinary answer, drops momentary small talk, separates topics, and rejects broadcast, performed, recited, or ambiguous speech.
- **Output**: a short attributed digest or an empty string. A non-empty digest is appended through the ordinary daily-summary writer and then sent through the shared graph-extraction helper. An empty result writes no diary row. Eligible source lines are marked digested only after the pass succeeds; model or diary failure leaves them available for retry.
- **Limits**: `llm_chat_timeout_sec`; `max_tokens: 300`; one UTC day and `passive_digest_max_lines` per pass. No ambient text enters the reply path.

---

## 18. Canned Fallback Rendering

- **File**: [src/jarvis/reply/fallbacks.py](src/jarvis/reply/fallbacks.py) — `in_the_voices_language()`.
- **Trigger**: the reply engine is about to deliver one of its own canned messages (the malformed-output guard, or the empty-reply backstop) and the configured voice names a language. Every other reply is written by the model itself under the prompt's language rule, so no rendering is needed.
- **Model / gating**: FAST tier, `resolve_model(cfg, Tier.FAST)` via `get_llm_backend(cfg).chat(...)`. Gated on `resolve_voice_language(cfg.tts_piper_model_path)` returning a name, so text chat, a non-Piper engine, and unreadable voice metadata never reach the model.
- **Inputs**: the canned English sentence and the voice's language name. No user text, no memory, no tool output.
- **System prompt**: translate into the named language, translation only, no quotes or commentary. Anything beyond the sentence would be spoken aloud with it.
- **Output**: the sentence in the voice's language, cached per language and message for the process lifetime, so the call happens at most once per message. An empty result, a timeout, or any exception leaves the English original standing.
- **Limits**: `RENDER_TIMEOUT_SEC` (20 s). The guard has already fired by this point, so the wait sits on top of a turn that has gone wrong.

---

## Frequency / Size Summary

| # | Context | Per reply | Optional? | Model tier |
|---|---------|-----------|-----------|------------|
| 1 | Main chat loop | 1-8 | No | CHAT |
| 2 | Intent judge | 0-1 (voice only) | skipped for edge wake addresses | SMALL |
| 3 | Memory enrichment extract | 0-1 | gated by enabled planner | SMALL (FAST tier) |
| 4 | Memory digest | 0-N | auto by size | FAST |
| 5 | Tool-result digest | 0-N | auto by size | FAST |
| 6 | Max-turn digest | 0-1 | No | SMALL |
| 7 | Tool router | 1 | always runs; planner picks unioned in | SMALL |
| 8 | Tool searcher | 0-3 | model-initiated | SMALL (reuses #7) |
| 9 | Summariser | ~1/session | No (background) | PRIVATE |
| 10 | Graph extraction | ~1/session | No (background) | PRIVATE |
| 11 | Graph best-child | 0-N | No (background) | SMALL (FAST tier) |
| 11b | Graph node merge | 0-N (per node, batched) | No (background) | PRIVATE |
| 11c | Graph auto-split | 0-1 per oversized node | threshold-gated | PRIVATE |
| 12 | Planner (plan_query) | 1 | yes (planner_enabled) | CHAT |
| 13 | Plan step resolver | 0-N (SMALL only) | auto by size + plan | CHAT |
| 14 | Tool-specific | per-tool | n/a | FAST or CHAT as listed above |
| 17 | Ambient digest | 0-1 per configured interval | passive capture only | untiered (`llm_chat_model` direct) |
| 18 | Canned fallback rendering | 0-1, once per message per language | only with a named voice language | SMALL (FAST tier) |

## Size-aware auto switches

Driven by `detect_model_size(model_name) → SMALL (≤7.5B) | LARGE (>7.5B)` — uses a regex to extract the parameter count from the model name, handles MoE (`8x7b`) as LARGE, and defaults bare `gemma4` names (no size tag) to SMALL while sized variants (e.g. `gemma4:12b`) follow the threshold:

| Feature | SMALL | LARGE |
|---------|-------|-------|
| Memory digest | ON | OFF |
| Tool-result digest | ON | OFF |
| Text-based tool calling | ON | OFF (native) |
| Planner direct-exec | ON | OFF |

## Config keys

- Routes and models: `llm_routes` contains ordered FAST and CHAT entries. `llm_chat_model` and `fast_model` carry the first effective route models for prompt sizing. `ollama_chat_model` is the PRIVATE and local-fallback model. Every explicit context model is obtained through `resolve_model(cfg, tier)`, except the untiered ambient digest (#17), which calls `cfg.llm_chat_model` directly. Streaming uses one monotonic request deadline; an available loopback route receives a 1.2-second progress window before the remaining tier chain is tried, and the first route to emit text exclusively owns the answer.
- Embeddings: `ollama_embed_model` through loopback `get_embedding_backend(cfg)`. Embeddings never use `llm_routes`.
- Flags: `memory_digest_enabled`, `tool_result_digest_enabled`, `remio_memory_enabled`, `llm_thinking_enabled`, `intent_judge_thinking_enabled`, `tool_selection_strategy`, `planner_enabled`, `low_power_mode`, `passive_capture_enabled`. Enabled Remio retrieval runs locally alongside planner-directed diary lookup, contributes attributable excerpts only, and fails without changing the prompt.
- Timeouts: `llm_chat_timeout_sec` (180s, also #17), `llm_digest_timeout_sec` (8s, shared across #4/#5/#6), `llm_tools_timeout_sec`, `intent_judge_timeout_sec` (6s), `planner_timeout_sec` (3s), `simple_reply_first_audio_sec` (3s), `memory_reply_first_audio_sec` (10s), `passive_digest_interval_min` (15-minute worker interval). Reply budgets are monotonic and planner-directed memory work shares the remaining budget. When automatic crew handoff is enabled and the existing crew token and chat ID are configured, the current `TurnTrace` additionally caps local pre-flight and loop work at the 3-second decision point or 5-second hard cutoff. These handoff thresholds are the fixed reply contract, not configuration keys.
- Caps: `agentic_max_turns` (8), `tool_search_max_calls` (3), `_LLM_MAX_SELECTED` (5), `_DIGEST_MAX_CHARS` (400), `_TOOL_DIGEST_MAX_CHARS` (600), `passive_digest_max_lines` (120). Per-context `max_tokens` caps listed above (50–1500 depending on task — the ambient digest uses 300, the intent judge's 1500 covers reasoning + answer on reasoning models; rewrite tasks scale with input length).
- Runtime residency: `low_power_mode` skips startup LLM warmups and shortens Ollama `keep_alive` for intent judge and warmup calls from `"30m"` to `"1m"`. It does not change prompts, model selection, timeouts, or context limits.

## KV-cache discipline (prompt construction rules)

Every context is built against servers (Ollama, vLLM, SGLang, llama.cpp `llama-server`, LM Studio) that reuse the KV state of the longest matching prompt prefix. The first diverging token decides how much compute is saved, so these rules are load-bearing:

1. **System prompts are byte-static** — no timestamps, hints, or per-call data inside. Per-call data (time, location, dialogue) lives in the user message.
2. **Dynamic blocks go to the tail** — anything that changes per call (context line, hint blocks) is appended at the END of its message, never at the head.
3. **Stable-before-dynamic ordering** — the mostly-static block (persona, tool catalogue) opens the prompt; per-query blocks (digest, plan, hint) follow; the user query is the final token.
4. **Per-reply memoisation** — the main loop's time/location context string is computed once per reply, so all in-loop calls of one reply are byte-identical from token 1; the KV prefix extends through the whole history, not just the system message.
5. **Ollama payloads set `cache_prompt: true` explicitly** on `chat()`, `direct()`, and `streaming()` so the server always retains the request's KV state.

Anything that reorders messages between calls, injects a changing value at the head of a prompt, or rebuilds a system prompt with per-call content breaks prefix reuse for every token after the divergence point.

## Flow

```
user input
  ├─▶ edge wake address           (voice only, deterministic)
  └─▶ [2] Intent Judge            (contextual voice only, SMALL)
        └─▶ [7] Tool router (static vectors warmed; narrows catalogue)
              └─▶ [12] Planner (gates memory; advisory for the router allow-list)
                    ├─ plan requests searchMemory  → [3] Enrichment extract → [4] Memory digest (optional)
                    ├─ plan empty (fail-open)      → [3] Enrichment extract → [4] Memory digest
                    ├─ planner disabled            → skip #3 and #4
                    └─ plan reply-only             → skip #3 and #4 entirely
                    └─▶ AGENTIC LOOP  (≤ agentic_max_turns)
                                      ├─ 3s and not close to done → askCrew fire-and-forget
                                      ├─ 5s hard cutoff           → askCrew fire-and-forget
                                      ├─ [13] Plan step resolver (SMALL, direct-exec)
                                      ├─ [1] Main chat turn
                                      ├─ tool execution
                                      │    └─ [5] Tool-result digest (optional)
                                      │    └─ [8] Tool searcher (model-initiated)
                                      └─ content → deliver immediately
                                      └─ if max turns → [6] Max-turn digest
                          └─▶ TTS / output
                          └─▶ background: [9] summariser (PRIVATE) → [10] graph extract (PRIVATE)
                                                           ├─▶ [11] best-child (FAST)
                                                           ├─▶ [11b] node merge (PRIVATE)
                                                           └─▶ [11c] auto-split (PRIVATE)
```

## Optimisation ideas (seed list)

1. Batch multi-chunk memory digests (#4) into a single call with explicit markers.
2. Parallelise multiple tool-result digests (#5) when several results land at once.
3. Keep the intent-judge model resident throughout active voice sessions.
4. Cache tool-router (#7) output by query hash.
5. Give each digest its own timeout budget rather than sharing `llm_digest_timeout_sec` (today a slow memory digest can starve the max-turn digest).
6. Consider single-model deployments: the FAST tier prefers a small dedicated model while the planner tracks `llm_chat_model`; loading a second model hurts cold-start latency on small hardware. (On an OpenAI-compatible chat provider an unset `fast_model` already resolves to the chat model, so every context rides the one served model.)
7. Narrow `llm_thinking_enabled` to router/planner only, not every context.
ambient transcript (passive switch on)
  └─▶ [16] Ambient digest → [9] daily summary update → [10] graph extract → [11] best-child
8. Keep contextual intent judging off the deterministic edge-wake path.

## 21. Model and reply-prefix warm-up

- **Source**: provider `warm_up()` plus `warm_up_reply_prefix()` in `src/jarvis/reply/engine.py`.
- **Trigger**: once per configured model at listener startup in parallel daemon threads. After the chat-model reachability/weight probe succeeds, the chat thread also prefills the main reply prefix. The embedding model takes the static-catalogue path described below.
- **Model / gating**: the first available candidate in the requested route lane plus the local Ollama candidate. The reply-prefix prefill uses the CHAT lane after its provider warmup succeeds.
- **What is sent**: `build_reply_prompt_prefix(cfg)` as the system message and `Reply with OK.` as the user message. The prefix contains the persona, model-size prompt components, and a response-language constraint: the configured voice's language when one resolves (or English for Chatterbox), otherwise an instruction to answer in whatever language the user used. Query-dependent profile, memory, plan, time, and tool descriptions follow this prefix only on live turns.
- **Prefix contract**: `_build_initial_system_message()` begins with the exact string returned by `build_reply_prompt_prefix()`. This is the cache boundary covered by `tests/test_response_latency.py`.
- **Gating**: the warmup always fires when a model is configured (regardless of provider). What differs between providers is the *probe behaviour*: the two-phase chat-completion probe described here is specific to `openai_compatible`; the Ollama warmup sends `POST /api/generate` with `keep_alive`.
- **Output**: `True`/`False` — consumed by the listener startup dashboard (shown as `⚠️ warmup failed — will load on first use`)
- **Limits**: provider warmup uses the shared 60 s startup budget. Reply-prefix prefill uses the same remaining per-thread timeout, `max_tokens: 1`, `keep_alive: 30m`, no tools, and thinking disabled.
- **Data flow**: `warm_up()` → raw `requests.post` → `resp.ok` (any 2xx) → `bool` returned to `_start_llm_warmup()` → listener startup print
- **Notes**: Best-effort and non-blocking. A failed warmup never prevents the listener from starting. Ollama uses `POST /api/generate` for residency before the cache-prefill chat request.

    The **embed warmup** is a separate path: `listener.py:_start_llm_warmup` embeds every static builtin and cached MCP tool summary through the embedding backend. Embedding-only models are not served on chat endpoints, and the resulting vectors are the same cached values consumed by live embedding selection.

---

## Measuring

`tests/performance/test_pipeline_timings.py` times each context in this graph against a live Ollama. The recorder patches the reply engine's provider-dispatched chat boundary and `RoutedBackend.direct`, so the main loop and pre-flight contexts are both visible. `JARVIS_PERF_MODEL` is assigned to the effective provider-independent chat model and its Ollama alias; `JARVIS_PERF_FAST_MODEL` optionally selects a dedicated FAST-tier model. Run:

```
pytest tests/performance/ -v -m performance -s
```

It records per-context p50/p95 latencies using a monkey-patch recorder that infers the context from the caller's `__qualname__` (see `_CALLER_TO_CONTEXT` in `tests/performance/timing_recorder.py`). Dumps a JSON report to `tests/performance/reports/`. A micro-benchmark with a tiny fixed prompt runs alongside to give a per-call floor — if that floor moves, every context's total moves with it, so hardware/model drift is visible immediately.

Baseline on a local gemma4:e2b (as of 2026-04-22, 3 queries × 3 runs): main chat turn p50 ~4.5s, enrichment extract p50 ~0.9s (small-model chain), micro-prompt floor ~0.15s. Sample sizes: main 25 calls, enrichment 9. Use these as rough reference points — the assertions in the test are relative-shape (router ≤ 1.5× main chat turn), not absolute.

When you add or change a context, update `_CALLER_TO_CONTEXT` so it shows up in the report instead of landing in the `other:` bucket.

## Keep this doc in sync

This graph is the reference for LLM-latency optimisation. Treat it as authoritative: whenever code changes affect an LLM call — a new context, a removed one, a changed model/timeout/cap/gating/prompt source, or a new data-flow edge — update this file in the same PR. If the update would be more than a one-line tweak, reflect it in the relevant `*.spec.md` too.
