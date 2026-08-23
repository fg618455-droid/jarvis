"""Integration test for the toolSearchTool escape hatch and related loop behaviours.

Scenario: the router picks a narrow initial tool set. Mid-loop the chat model
realises it needs a different tool and invokes ``toolSearchTool``. The engine
dispatches it, merges the returned tool names into the per-turn allow-list,
and the next turn calls the newly-surfaced tool (``getWeather``). The final
content is delivered immediately.
"""

from unittest.mock import patch

import pytest


def _assistant_tool_call(name: str, args: dict, call_id: str = "call_1"):
    return {
        "message": {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args},
                }
            ],
        }
    }


def _assistant_content(text: str):
    return {"message": {"role": "assistant", "content": text}}


_FABRICATED_CALCULATOR_REPLY = (
    'Das Produkt von 7 und 6 beträgt 42. Auf der Anzeige erscheint es als "42".'
)


def test_zero_tool_live_state_claim_is_withheld_and_replaced(
    mock_config, db, dialogue_memory
):
    """A router-positive turn cannot ship an external-state claim when the
    chat model ignored every tool, including the discovery escape hatch."""
    from jarvis.reply import engine as engine_mod

    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.agentic_max_turns = 3
    mock_config.tool_selection_strategy = "llm"

    chat_messages: list[list[dict]] = []
    spoken: list[str] = []
    logged: list[str] = []

    def fake_chat(*args, **kwargs):
        chat_messages.append([dict(message) for message in kwargs["messages"]])
        on_token = kwargs.get("on_token")
        if on_token is not None:
            on_token(_FABRICATED_CALCULATOR_REPLY)
        return _assistant_content(_FABRICATED_CALCULATOR_REPLY)

    with patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(
             engine_mod,
             "select_tools",
             return_value=["openOnComputer", "stop"],
         ), \
         patch.object(
             engine_mod,
             "run_tool_with_retries",
             side_effect=AssertionError("the fake model made no tool call"),
         ), \
         patch.object(
             engine_mod,
             "debug_log",
             side_effect=lambda message, _area: logged.append(str(message)),
         ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text=(
                "In der bereits geöffneten Calculator-App: berechne 7 mal 6, "
                "indem du auf die Zahlen- und Operator-Buttons klickst, und sag "
                "mir dann das angezeigte Ergebnis."
            ),
            dialogue_memory=dialogue_memory,
            on_speech_segment=spoken.append,
        )

    assert reply != _FABRICATED_CALCULATOR_REPLY
    assert reply and "can't confirm" in reply.lower()
    assert len(chat_messages) >= 2, "the engine should force a corrective turn"
    assert any(
        "toolSearchTool" in str(message.get("content", ""))
        for message in chat_messages[1]
    ), "the corrective turn must point at the discovery escape hatch"
    assert spoken == [], "withheld ungrounded prose must not leak through TTS"
    assert any(
        "zero-tool" in message and "withheld" in message for message in logged
    ), "the structural decision point must be written to debug_log"


@pytest.mark.parametrize(
    ("query", "answer"),
    [
        ("Hello there", "Hello!"),
        ("Tell me a joke", "Why did the byte cross the bus?"),
        ("What is 7 times 6?", "42"),
        ("Which colour feels calming to you?", "I like quiet blues."),
    ],
)
def test_legitimate_zero_tool_reply_passes_through(
    mock_config, db, dialogue_memory, query, answer
):
    """Router-confirmed conversation and pure reasoning need no tool proof."""
    from jarvis.reply import engine as engine_mod

    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.tool_selection_strategy = "llm"

    with patch.object(
        engine_mod, "chat_with_messages", return_value=_assistant_content(answer)
    ) as chat, patch.object(
        engine_mod, "select_tools", return_value=["stop"]
    ), patch.object(
        engine_mod,
        "run_tool_with_retries",
        side_effect=AssertionError("ordinary conversation must not call tools"),
    ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text=query,
            dialogue_memory=dialogue_memory,
        )

    assert reply == answer
    assert chat.call_count == 1


@pytest.mark.parametrize(
    ("strategy", "selection_kind"),
    [
        ("all", "narrowed"),
        ("keyword", "narrowed"),
        ("embedding", "narrowed"),
        ("llm", "full_catalogue"),
    ],
)
def test_non_semantic_router_shapes_do_not_gate_zero_tool_replies(
    mock_config, db, dialogue_memory, strategy, selection_kind
):
    """Availability and fall-open shapes are not positive relevance signals."""
    from jarvis.reply import engine as engine_mod

    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.tool_selection_strategy = strategy

    selected = (
        ["openOnComputer", "stop"]
        if selection_kind == "narrowed"
        else list(engine_mod.BUILTIN_TOOLS.keys())
    )
    answer = "A direct answer that needs no external observation."

    with patch.object(
        engine_mod, "chat_with_messages", return_value=_assistant_content(answer)
    ) as chat, patch.object(
        engine_mod, "select_tools", return_value=selected
    ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="Share a general thought.",
            dialogue_memory=dialogue_memory,
        )

    assert reply == answer
    assert chat.call_count == 1


def test_memory_only_plan_is_grounded_without_a_callable_tool(
    mock_config, db, dialogue_memory
):
    """Memory enrichment is external evidence even though it is not a tool."""
    from jarvis.reply import engine as engine_mod

    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.tool_selection_strategy = "llm"
    mock_config.memory_digest_enabled = False

    answer = "Your stored project note says the launch is Friday."
    with patch.object(
        engine_mod, "select_tools", return_value=["webSearch", "stop"]
    ), patch.object(
        engine_mod,
        "plan_query",
        return_value=["searchMemory topic='project launch'", "Reply to the user."],
    ), patch.object(
        engine_mod,
        "extract_search_params_for_memory",
        return_value={"keywords": [], "questions": []},
    ), patch.object(
        engine_mod, "chat_with_messages", return_value=_assistant_content(answer)
    ) as chat:
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="What do my project launch notes say?",
            dialogue_memory=dialogue_memory,
        )

    assert reply == answer
    assert chat.call_count == 1


def test_zero_tool_gate_recovers_via_toolsearchtool_then_desktop_interact(
    mock_config, db, dialogue_memory
):
    """A challenged turn may widen the allow-list and use the surfaced tool."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.builtin.desktop_interact import DesktopInteractTool
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.agentic_max_turns = 5
    mock_config.tool_selection_strategy = "llm"

    invoked_tools: list[str] = []

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        invoked_tools.append(tool_name)
        if tool_name == "toolSearchTool":
            return ToolExecutionResult(
                success=True,
                reply_text=(
                    "desktopInteract: Inspect and operate an already-open "
                    "desktop application."
                ),
                error_message=None,
            )
        if tool_name == "desktopInteract":
            return ToolExecutionResult(
                success=True,
                reply_text="Calculator display inspected after button clicks: 42",
                error_message=None,
            )
        raise AssertionError(f"unexpected tool call: {tool_name}")

    chat_responses = iter(
        [
            _assistant_content(_FABRICATED_CALCULATOR_REPLY),
            _assistant_tool_call(
                "toolSearchTool",
                {"query": "operate buttons in an already-open desktop app"},
                call_id="search_1",
            ),
            _assistant_tool_call(
                "desktopInteract",
                {"task": "Click 7, multiply, 6, equals, then read the display"},
                call_id="desktop_1",
            ),
            _assistant_content(_FABRICATED_CALCULATOR_REPLY),
        ]
    )

    with patch.object(
        engine_mod, "chat_with_messages", side_effect=lambda *a, **kw: next(chat_responses)
    ), patch.object(
        engine_mod,
        "select_tools",
        return_value=["openOnComputer", "stop"],
    ), patch.object(
        engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner
    ), patch.dict(
        engine_mod.BUILTIN_TOOLS,
        {"desktopInteract": DesktopInteractTool()},
    ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text=(
                "In der bereits geöffneten Calculator-App: berechne 7 mal 6 "
                "durch Klicks und lies das Ergebnis ab."
            ),
            dialogue_memory=dialogue_memory,
        )

    assert invoked_tools == ["toolSearchTool", "desktopInteract"]
    assert reply == _FABRICATED_CALCULATOR_REPLY


def test_tool_search_alone_does_not_ground_a_claim_about_external_state(
    mock_config, db, dialogue_memory
):
    """Discovery without invoking the surfaced tool is still ungrounded."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.builtin.desktop_interact import DesktopInteractTool
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.llm_chat_model = "qwen2.5:7b-ctx8k"
    mock_config.planner_enabled = False
    mock_config.agentic_max_turns = 4
    mock_config.tool_selection_strategy = "llm"

    invoked_tools: list[str] = []
    spoken: list[str] = []

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        invoked_tools.append(tool_name)
        assert tool_name == "toolSearchTool"
        return ToolExecutionResult(
            success=True,
            reply_text=(
                "desktopInteract: Inspect and operate an already-open "
                "desktop application."
            ),
            error_message=None,
        )

    chat_responses = iter(
        [
            _assistant_content(_FABRICATED_CALCULATOR_REPLY),
            _assistant_tool_call(
                "toolSearchTool",
                {"query": "operate buttons in an already-open desktop app"},
                call_id="search_only",
            ),
            _assistant_content(_FABRICATED_CALCULATOR_REPLY),
        ]
    )

    def fake_chat(*args, **kwargs):
        response = next(chat_responses)
        content = response["message"].get("content") or ""
        on_token = kwargs.get("on_token")
        if content and on_token is not None:
            on_token(content)
        return response

    with patch.object(
        engine_mod, "chat_with_messages", side_effect=fake_chat
    ), patch.object(
        engine_mod,
        "select_tools",
        return_value=["openOnComputer", "stop"],
    ), patch.object(
        engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner
    ), patch.dict(
        engine_mod.BUILTIN_TOOLS,
        {"desktopInteract": DesktopInteractTool()},
    ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text=(
                "In der bereits geöffneten Calculator-App: berechne 7 mal 6 "
                "durch Klicks und lies das Ergebnis ab."
            ),
            dialogue_memory=dialogue_memory,
            on_speech_segment=spoken.append,
        )

    assert invoked_tools == ["toolSearchTool"]
    assert reply != _FABRICATED_CALCULATOR_REPLY
    assert reply and "can't confirm" in reply.lower()
    assert spoken == []


def test_loop_merges_toolsearchtool_results_into_allowlist(
    mock_config, db, dialogue_memory
):
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"  # LARGE → no forced text tools

    mock_config.llm_chat_model = "gpt-oss:20b"  # LARGE → no forced text tools

    invoked_tools: list[tuple[str, dict]] = []

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        invoked_tools.append((tool_name, tool_args or {}))
        if tool_name == "toolSearchTool":
            # Returns a newly-routed tool that was NOT in the initial pick.
            return ToolExecutionResult(
                success=True,
                reply_text="getWeather: Report current weather.",
                error_message=None,
            )
        if tool_name == "getWeather":
            return ToolExecutionResult(
                success=True,
                reply_text="London: 12C partly cloudy.",
                error_message=None,
            )
        return ToolExecutionResult(
            success=True, reply_text="result", error_message=None
        )

    chat_responses = iter(
        [
            # Turn 1: model calls toolSearchTool.
            _assistant_tool_call(
                "toolSearchTool", {"query": "current weather in london"}
            ),
            # Turn 2: model uses the newly-surfaced getWeather.
            _assistant_tool_call(
                "getWeather", {"location": "London"}, call_id="call_2"
            ),
            # Turn 3: final reply.
            _assistant_content("It's 12C and partly cloudy in London."),
        ]
    )

    def fake_chat(*args, **kwargs):
        try:
            return next(chat_responses)
        except StopIteration:
            return _assistant_content("Done.")

    with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="how's the weather in london?",
            dialogue_memory=dialogue_memory,
        )

    tool_names = [n for n, _ in invoked_tools]
    assert "toolSearchTool" in tool_names, (
        f"Expected toolSearchTool to be invoked; got {tool_names}"
    )
    assert "getWeather" in tool_names, (
        "Expected getWeather (surfaced mid-loop by toolSearchTool) to be "
        f"invoked on a subsequent turn; got {tool_names}"
    )
    # getWeather must follow toolSearchTool (the allow-list widening
    # happens after the tool result is appended).
    assert tool_names.index("getWeather") > tool_names.index("toolSearchTool")
    assert reply and "London" in reply


def test_initial_allowlist_always_includes_toolsearchtool(
    mock_config, db, dialogue_memory
):
    """Even when the router returns no additional tools, the engine must
    always append ``toolSearchTool`` so the escape hatch is reachable."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"

    mock_config.llm_chat_model = "gpt-oss:20b"

    captured_allow_lists: list[list[str]] = []

    def fake_chat(*args, **kwargs):
        # Capture a snapshot of allowed_tools via the first system message
        # (too invasive to reach into the closure — instead we assert on the
        # final reply path indirectly).
        return _assistant_content("Hello back!")

    with patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ):
        # Patch the tools description generator to snapshot the allow-list.
        real_generate = engine_mod.generate_tools_json_schema

        def spy_schema(allowed_tools, mcp_tools):
            captured_allow_lists.append(list(allowed_tools))
            return real_generate(allowed_tools, mcp_tools)

        with patch.object(
            engine_mod, "generate_tools_json_schema", side_effect=spy_schema
        ):
            engine_mod.run_reply_engine(
                db=db,
                cfg=mock_config,
                tts=None,
                text="hi",
                dialogue_memory=dialogue_memory,
            )

    assert captured_allow_lists, "generate_tools_json_schema was never called"
    # The engine now runs the router before the planner, which builds an
    # auxiliary schema for the planner's tool catalogue (router-narrowed,
    # no escape hatch) before the final chat-model schema. The escape hatch
    # only joins in the chat-model allow-list. Assert it appears somewhere
    # in the captured calls — implementations are free to reuse the same
    # schema generator at multiple call sites.
    assert any("toolSearchTool" in al for al in captured_allow_lists), (
        f"toolSearchTool missing from any allow-list: {captured_allow_lists}"
    )


def test_schema_regenerated_after_toolsearchtool_merge(
    mock_config, db, dialogue_memory
):
    """F1: after toolSearchTool widens the allow-list, the next native-mode
    LLM call must receive a tools schema that includes the newly surfaced
    tool name."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"  # LARGE → native tools

    mock_config.llm_chat_model = "gpt-oss:20b"  # LARGE → native tools

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        if tool_name == "toolSearchTool":
            return ToolExecutionResult(
                success=True,
                reply_text="getWeather: Report current weather.",
                error_message=None,
            )
        return ToolExecutionResult(
            success=True, reply_text="done", error_message=None
        )

    chat_responses = iter(
        [
            _assistant_tool_call(
                "toolSearchTool", {"query": "weather"}, call_id="c1"
            ),
            _assistant_content("All good."),
        ]
    )
    captured_tools_params: list = []

    def fake_chat(*args, **kwargs):
        captured_tools_params.append(kwargs.get("tools"))
        try:
            return next(chat_responses)
        except StopIteration:
            return _assistant_content("done")

    with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ):
        engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="weather?",
            dialogue_memory=dialogue_memory,
        )

    # Two LLM calls: pre-merge and post-merge. The post-merge call must
    # include getWeather in its tools schema.
    assert len(captured_tools_params) >= 2
    post_merge_schema = captured_tools_params[1] or []
    names = []
    for s in post_merge_schema:
        if isinstance(s, dict):
            fn = s.get("function", {}) if isinstance(s.get("function"), dict) else {}
            nm = fn.get("name")
            if nm:
                names.append(nm)
    assert "getWeather" in names, (
        f"Expected getWeather in post-merge tools schema; got {names}"
    )


def test_tool_search_max_calls_cap(mock_config, db, dialogue_memory):
    """F5: toolSearchTool invocations are capped per reply."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"

    mock_config.llm_chat_model = "gpt-oss:20b"
    mock_config.tool_search_max_calls = 2

    dispatch_count = {"toolSearchTool": 0}

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        if tool_name == "toolSearchTool":
            dispatch_count["toolSearchTool"] += 1
            return ToolExecutionResult(
                success=True,
                reply_text="No additional tools found for that description.",
                error_message=None,
            )
        return ToolExecutionResult(
            success=True, reply_text="ok", error_message=None
        )

    # Model keeps trying toolSearchTool; last turn emits final content.
    responses = [
        _assistant_tool_call("toolSearchTool", {"query": "a"}, call_id="c1"),
        _assistant_tool_call("toolSearchTool", {"query": "b"}, call_id="c2"),
        _assistant_tool_call("toolSearchTool", {"query": "c"}, call_id="c3"),
        _assistant_tool_call("toolSearchTool", {"query": "d"}, call_id="c4"),
        _assistant_content("All right, giving up."),
    ]
    it = iter(responses)

    def fake_chat(*args, **kwargs):
        try:
            return next(it)
        except StopIteration:
            return _assistant_content("done")

    with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "select_tools", return_value=["webSearch", "stop"]), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ):
        engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="hello",
            dialogue_memory=dialogue_memory,
        )

    assert dispatch_count["toolSearchTool"] == 2, (
        f"Expected cap to limit dispatch to 2; got "
        f"{dispatch_count['toolSearchTool']}"
    )


def test_validate_tool_args_catches_unknown_keys():
    """Unit test for the schema validator — unknown arg key is the exact
    failure mode the field log hit."""
    from jarvis.reply.engine import _validate_tool_args_against_schema

    err = _validate_tool_args_against_schema(
        "webSearch",
        {"query": "tube strikes today"},
        mcp_tools=None,
    )
    assert err is not None
    assert "unknown argument" in err.lower()
    assert "search_query" in err


def test_validate_tool_args_passes_correct_keys():
    from jarvis.reply.engine import _validate_tool_args_against_schema

    err = _validate_tool_args_against_schema(
        "webSearch",
        {"search_query": "tube strikes today"},
        mcp_tools=None,
    )
    assert err is None


def test_validate_tool_args_catches_missing_required():
    from jarvis.reply.engine import _validate_tool_args_against_schema

    err = _validate_tool_args_against_schema(
        "webSearch",
        {},
        mcp_tools=None,
    )
    assert err is not None
    assert "missing required" in err.lower()


def test_max_turns_produces_digest(mock_config, db, dialogue_memory):
    """When the loop hits ``agentic_max_turns`` via a pure tool-call loop
    (no content turn), the engine runs ``digest_loop_for_max_turns`` and
    ships the caveat-prefixed digest."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"

    mock_config.llm_chat_model = "gpt-oss:20b"
    mock_config.agentic_max_turns = 3

    # The model keeps calling toolSearchTool every turn — no content is
    # ever produced, so the loop exhausts max_turns and the digest fires.
    def fake_chat(*args, **kwargs):
        return _assistant_tool_call("toolSearchTool", {"query": "a"}, call_id="c1")

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        return ToolExecutionResult(
            success=True,
            reply_text="No additional tools found.",
            error_message=None,
        )

    captured = {}

    def fake_digest(user_query, loop_messages, cfg):
        captured["user_query"] = user_query
        captured["loop_messages"] = loop_messages
        return "Couldn't finish: I was still working through the request."

    with patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(
             engine_mod, "select_tools", return_value=["toolSearchTool", "stop"]
         ), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ), \
         patch.object(
             engine_mod, "digest_loop_for_max_turns", side_effect=fake_digest
         ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="do something complicated",
            dialogue_memory=dialogue_memory,
        )

    assert reply == "Couldn't finish: I was still working through the request."
    assert captured.get("user_query"), "digest should receive the user query"
    assert isinstance(captured.get("loop_messages"), list)


def test_max_turns_digest_failure_falls_back_to_generic_error(
    mock_config, db, dialogue_memory
):
    """If the digest returns None (e.g. timeout) and there is no last
    candidate reply (pure tool-call loop), the engine must emit the
    generic error rather than returning None."""
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"

    mock_config.llm_chat_model = "gpt-oss:20b"
    mock_config.agentic_max_turns = 2

    # Pure tool-call loop — no content, so last_candidate_reply stays None.
    def fake_chat(*args, **kwargs):
        return _assistant_tool_call("toolSearchTool", {"query": "a"}, call_id="c1")

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        return ToolExecutionResult(
            success=True,
            reply_text="No additional tools found.",
            error_message=None,
        )

    with patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(
             engine_mod, "select_tools", return_value=["toolSearchTool", "stop"]
         ), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ), \
         patch.object(
             engine_mod, "digest_loop_for_max_turns", return_value=None
         ):
        reply = engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="do something complicated",
            dialogue_memory=dialogue_memory,
        )

    # Must return some reply (generic error), not None.
    assert reply is not None and reply.strip()


def test_toolsearchtool_empty_result_does_not_register_sentence_as_tool(
    mock_config, db, dialogue_memory, capsys
):
    """Regression: when toolSearchTool surfaces nothing, it returns the
    plain sentence ``"No additional tools found for that description."``
    as ``reply_text``. The engine's line-splitting merger used to treat
    that whole sentence as a tool name and append it to ``allowed_tools``,
    producing the field-log line ``🔧 Discovered 1 tool(s): No additional
    tools found for that description.`` and polluting the allow-list
    with a bogus entry. The parser must reject anything that is not an
    actual tool name from the registry.
    """
    from jarvis.reply import engine as engine_mod
    from jarvis.tools.types import ToolExecutionResult

    mock_config.ollama_chat_model = "gpt-oss:20b"

    mock_config.llm_chat_model = "gpt-oss:20b"

    def fake_tool_runner(db, cfg, tool_name, tool_args, **kwargs):
        if tool_name == "toolSearchTool":
            return ToolExecutionResult(
                success=True,
                reply_text="No additional tools found for that description.",
                error_message=None,
            )
        return ToolExecutionResult(
            success=True, reply_text="ok", error_message=None
        )

    chat_responses = iter(
        [
            _assistant_tool_call(
                "toolSearchTool", {"query": "open youtube"}, call_id="c1"
            ),
            _assistant_content("I could not find a tool for that."),
        ]
    )
    captured_tools_params: list = []

    def fake_chat(*args, **kwargs):
        captured_tools_params.append(kwargs.get("tools"))
        try:
            return next(chat_responses)
        except StopIteration:
            return _assistant_content("done")

    with patch.object(engine_mod, "run_tool_with_retries", side_effect=fake_tool_runner), \
         patch.object(engine_mod, "chat_with_messages", side_effect=fake_chat), \
         patch.object(engine_mod, "select_tools", return_value=["stop"]), \
         patch.object(
             engine_mod,
             "extract_search_params_for_memory",
             return_value={"keywords": []},
         ):
        engine_mod.run_reply_engine(
            db=db,
            cfg=mock_config,
            tts=None,
            text="open youtube",
            dialogue_memory=dialogue_memory,
        )

    # The user-facing `🔧 Discovered N tool(s):` line is the first
    # symptom of the bug — if the parser accepts the empty-result
    # sentence as a tool name, the log prints it verbatim.
    stdout = capsys.readouterr().out
    assert "No additional tools found for that description" not in stdout or (
        "🔍 No new tools found" in stdout
    ), (
        "Engine's toolSearchTool merger printed the empty-result sentence "
        "as a discovered tool name. Expected `🔍 No new tools found` "
        "instead. Full stdout:\n" + stdout
    )
    assert "🔧 Discovered" not in stdout or (
        "No additional tools found" not in stdout
    ), (
        "Engine logged `🔧 Discovered ... No additional tools found ...` "
        "— the sentence was misclassified as a tool name. Stdout:\n" + stdout
    )
