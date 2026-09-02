## Tool Selection Spec

Selects a subset of available tools relevant to a given user query, so the LLM receives only tools it is likely to need. Reduces noise for smaller models and lowers token cost.

### ToolSelectionStrategy Enum

```python
class ToolSelectionStrategy(Enum):
    ALL = "all"
    KEYWORD = "keyword"
    EMBEDDING = "embedding"
    LLM = "llm"
```

### Strategies

Controlled by `tool_selection_strategy` in config:

| Value         | Behaviour                                                           | LLM call? | Extra dependency |
|---------------|---------------------------------------------------------------------|-----------|------------------|
| `"all"`       | Pass every registered tool.                                         | No        | None             |
| `"keyword"`   | Score tools by keyword overlap with the query; return top matches.  | No        | None             |
| `"embedding"` | Rank tools by cosine similarity of embeddings via nomic-embed-text. | No        | numpy            |
| `"llm"`       | Ask a lightweight LLM call to pick the top 3–5 relevant tool names (default). | Yes | None |

### Always-included Tools

Regardless of strategy, these tools are **always** included:
- `stop` — needed so the user can dismiss the assistant at any time.

### Keyword Strategy

1. Build a keyword index per tool from its `name` (camelCase split) and `description` (lowercased, stop-words removed).
2. Tokenise the user query (lowercase, split on whitespace/punctuation).
3. Score each tool: count of query tokens that appear in the tool's keyword set.
4. Return tools with score > 0, plus always-included tools.
5. If no tools score > 0, fall back to returning all tools (query is too vague to filter).

### Embedding Strategy

1. Embed the user query using `get_embedding()` (calls Ollama `/api/embeddings` with the configured embed model).
2. For each tool (excluding always-included), build a summary string from the tool name (camelCase split) and description, then embed it.
3. Compute cosine similarity between the query embedding and each tool embedding.
4. Select tools using a **relative threshold**: keep tools whose similarity >= `top_score * _RELATIVE_THRESHOLD` (0.97 — nomic-embed-text has a high baseline similarity, so a loose threshold lets the entire catalogue through).
5. If fewer than `_MIN_SELECTED` (3) tools pass the threshold, return the top 3 by similarity.
6. Append always-included tools.
7. If the query embedding fails, fall back to returning all tools.

Note: embedding is **not** the default strategy because nomic-embed-text produces tightly clustered similarities across all tools — the filter struggles to separate "good match" from "generic cluster" when a realistic MCP catalogue (20–40 tools) is in play. The `llm` strategy is cheaper in prompt size and more discriminative on small chat models.

### LLM Strategy (default)

1. Build a catalogue of `- name: description` lines (descriptions truncated to 120 chars) for every registered tool except always-included ones.
2. Send through the FAST-tier backend's `direct()` call with a system prompt asking for the **top 5 most relevant** tool names as a comma-separated list, followed by a single classification word. The prompt instructs the router to prefer 1–3 tools for narrow queries and to return `"none"` for greetings/small talk. The request uses `num_ctx=8192`, matching the main Ollama chat runner; when FAST and CHAT resolve to the same model, routing therefore reuses the resident runner instead of forcing Ollama to rebuild it at a different context size between the two calls.
3. Parse the response, matching tokens against known tool names (unknowns are dropped silently).
4. Apply a hard `_LLM_MAX_SELECTED` (5) cap regardless of what the router returned, to guard against chatty routers that echo the whole catalogue.
5. Append always-included tools.
6. If the router replies `"none"`, return only the always-included tools.
7. On timeout, empty response, or parse failure (no token in the response matched a known tool name), fall back to the **keyword strategy** rather than to the full catalogue. Reasoning: the catalogue can grow to 30–40 tools once an MCP server like `chrome-devtools` is enabled, and exposing all of them to a small chat model (gemma4:e2b class) overwhelms tool selection, producing empty replies. Keyword scoring narrows on query/name overlap deterministically, and the engine's `toolSearchTool` escape hatch still lets the chat model widen mid-loop if the keyword pick missed.

#### Chat backend preference

The same response also names which Tier.CHAT backend the turn prefers, so a caller can bias backend selection without a second LLM call (see `../llm/llm.spec.md`, "Chat backend selection"). The prompt instructs the router to append exactly one more word after the tool list: `HERMES` for a turn about building, debugging, or maintaining backend code, infrastructure, servers, or data systems — engineering work suited to a background crew with deep tool access; `COMPLEX` for a turn needing multi-step reasoning, careful structured output, or front-end/user-facing design work that is not HERMES-shaped; or `LOCAL` for everything else, including a simple factual lookup through a tool. Extraction is independent of the tool-name parsing above and tolerant of format drift: a case-insensitive word-boundary match for `local`, `complex`, or `hermes` anywhere in the response, taking the last match if more than one appears, and stripped out before the tool list's own `"none"` comparison so a response like `"none LOCAL"` still resolves to the mandatory-only tool result. Word-boundary matching means a real tool name containing the substring (e.g. `localFiles`) is never mistaken for the classification token. Neither the `"none"` short-circuit nor the keyword-strategy fallback (step 7) sees this token, since it is extracted from the raw router response before either path runs.

Only surfaced when the caller passes `chat_backend_signal`, a dict the router populates with `{"preference": "local" | "complex" | "hermes"}`. The key is left absent whenever no classification token is found in the response (including every fallback path in step 7): an absent key is the caller's fail-open signal to leave backend selection at its existing default. Other strategies never populate this dict.

Distinguishing `COMPLEX` from `HERMES` is a judgement the classifying model makes from the turn's own content, not a keyword match against the user's words: a small router model reads both as "needs real thinking" and the split is often close, particularly against the smallest supported chat models. That imprecision degrades gracefully — a `HERMES` turn misclassified as `COMPLEX` still reaches a synchronous cloud backend, just the other one, and the reply engine's own fail-open chain fallback (`../llm/llm.spec.md`, "Chat backend selection") means neither misclassification can leave a turn unanswered.

#### Context-aware routing

When the reply engine passes a `context_hint`, it is split into two labelled semantic slots in the router system prompt:

- **KNOWN FACTS** — things the assistant can already see (current time, detected location). If the query is answerable purely from these, the router should return `none`.
- **RECENT DIALOGUE** — recent user/assistant turns. The router is instructed to read the current query as a continuation of this exchange, so short follow-ups (e.g. "I'm in London" after "which city?") route to the tool that answers the combined intent across turns rather than being treated as idle chatter.

The split is the exact marker `"Recent dialogue (short-term memory):"` — any content before it is known facts, content after it is recent dialogue. If no dialogue marker is present, the whole hint is treated as known facts.

### Interface

```python
def select_tools(
    query: str,
    builtin_tools: Dict[str, Tool],
    mcp_tools: Dict[str, ToolSpec],
    strategy: ToolSelectionStrategy = ToolSelectionStrategy.ALL,
    llm_base_url: str = "",
    llm_model: str = "",
    llm_timeout_sec: float = 8.0,
    embed_model: str = "",
    embed_timeout_sec: float = 10.0,
    chat_backend_signal: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """Return list of tool names relevant to the query. ``chat_backend_signal``,
    when supplied, is populated by the "llm" strategy with
    ``{"preference": "local" | "complex" | "hermes"}`` — see "Chat backend
    preference"."""
```

### Integration

Called from the reply engine (Step 6) before `generate_tools_json_schema()` and `generate_tools_description()`. The returned list replaces the current `allowed_tools = list(BUILTIN_TOOLS.keys())`.

A narrowed result from the LLM strategy containing at least one tool other than `stop` and `toolSearchTool` is also the reply engine's structural signal that external work is relevant. If no tool implementation runs and the chat model attempts to finish in prose, the engine withholds that prose once and directs the next turn to a fitting tool or `toolSearchTool`. This signal is not inferred from words in the request or reply. The `all`, keyword, and embedding strategies are excluded: `all` expresses availability, and embedding always returns a minimum top-k even without a confident semantic match. A result equal to the complete catalogue is also excluded as a fallback shape. A selection containing only `stop` or no tools, and a planner result requiring memory enrichment but no callable tool, leave legitimate zero-tool replies unchanged.

### Configuration

- Key: `tool_selection_strategy`
- Type: `str` (validated against `ToolSelectionStrategy` enum values)
- Default: `"llm"`
- Valid values: `"all"`, `"keyword"`, `"embedding"`, `"llm"`

- Key: `fast_model` (the shared fast tier)
- Type: `str`
- Default: `""` (empty string — automatic: the small Ollama default on the Ollama chat path, the chat model on an OpenAI-compatible provider)
- Effect: when `tool_selection_strategy == "llm"`, routing runs on the fast tier (`resolve_model(cfg, Tier.FAST)`): small, fast, already warm for wake-word paths, and structurally the same classification job as intent judging. Set `fast_model` to pin every fast-tier context (routing included) to a specific model.
