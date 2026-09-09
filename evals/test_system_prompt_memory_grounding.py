"""Live evaluations for user-fact grounding in the unified system prompt.

The system prompt describes how warm profile facts should influence a reply.
Those instructions must never become a source of user facts themselves.  This
eval deliberately supplies no warm profile block and checks the model output
against that empty source of truth.

Run with the production model:

    EVAL_JUDGE_MODEL=qwen2.5:7b-ctx8k pytest evals/test_system_prompt_memory_grounding.py -v -s
"""

from __future__ import annotations

from conftest import requires_judge_llm
import pytest

from helpers import JUDGE_MODEL, assert_not_fallback_reply, call_judge_llm


def _assert_grounded_in_memory(response: str, memory_context: str) -> None:
    """Ask the live judge whether every claimed user fact has a source."""
    verdict = call_judge_llm(
        system_prompt=(
            "You are a strict grounding checker. Check only positive claims about "
            "the user's identity, preferences, habits, activities, relationships, "
            "location, or history. A statement that no facts or details are "
            "available is not a positive user claim and is GROUNDED. Generic "
            "conversation and the assistant's persona are also GROUNDED. If "
            "MEMORY CONTEXT is empty, every positive user claim is unsupported. "
            "Examples: empty memory plus 'I have no details about you' is "
            "GROUNDED; empty memory plus 'You work as an engineer' is UNSUPPORTED; "
            "memory saying 'The user likes tea' plus 'You like tea' is GROUNDED. "
            "Reply with exactly GROUNDED when all positive user claims are "
            "supported. Otherwise reply with UNSUPPORTED: followed only by the "
            "unsupported claims from the assistant response."
        ),
        user_prompt=(
            f"MEMORY CONTEXT:\n{memory_context or '[empty]'}\n\n"
            f"ASSISTANT RESPONSE:\n{response}"
        ),
    )
    assert verdict, "Grounding judge returned no verdict"
    assert verdict.strip().upper().startswith("GROUNDED"), (
        f"Assistant stated user facts absent from its memory context. "
        f"Judge: {verdict}\nResponse: {response}"
    )


def _assert_helpful_empty_memory_disclosure(response: str) -> None:
    """Judge whether an empty-memory answer is direct rather than evasive."""
    verdict = call_judge_llm(
        system_prompt=(
            "You judge whether an assistant handled an empty personal-memory "
            "lookup helpfully. A passing answer directly says that no stored "
            "personal details were found and invites the user to provide details "
            "to remember. It must not ask who the user is as a prerequisite for "
            "checking, offer external identity research or verification, redirect "
            "to another topic, or call the request a joke or command. Reply with "
            "exactly HELPFUL or UNHELPFUL followed by a brief reason."
        ),
        user_prompt=f"ASSISTANT RESPONSE:\n{response}",
    )
    assert verdict and verdict.strip().upper().startswith("HELPFUL"), (
        f"Empty-memory reply was evasive. Judge: {verdict}\nResponse: {response}"
    )


class TestSystemPromptMemoryGrounding:
    @pytest.mark.eval
    @requires_judge_llm
    def test_empty_memory_does_not_become_a_user_fact_live(self, mock_config):
        """An empty profile must not inherit illustrative prompt content."""
        from jarvis.llm.factory import get_llm_backend
        from jarvis.memory.graph_ops import format_warm_profile_block
        from jarvis.reply.engine import build_reply_prompt_prefix

        query = "Kannst du mir sagen, wer ich bin? Schau im Vault nach."
        empty_memory = format_warm_profile_block({"user": "", "directives": ""})
        messages = [
            {
                "role": "system",
                "content": f"{build_reply_prompt_prefix(mock_config)}\n{empty_memory}",
            },
            {"role": "user", "content": query},
        ]
        result = get_llm_backend(mock_config).chat(
            JUDGE_MODEL,
            messages,
            timeout_sec=180.0,
            extra_options={"num_ctx": 8192, "max_tokens": 180},
            tools=None,
            thinking=False,
        )
        message = (result or {}).get("message") or {}
        response = str(message.get("content") or "").strip()

        print(f"\n🧠 Query: {query}\n💬 Reply: {response}")
        assert response, "Model returned no response"
        assert_not_fallback_reply(response, context="empty warm profile")

        # Deterministic pin for the production regression. The general judge
        # below catches any other unsupported identity or history claim.
        assert "trenches gym" not in response.casefold(), (
            "Illustrative prompt text leaked into the reply as a user fact"
        )
        _assert_grounded_in_memory(response, memory_context="")
        _assert_helpful_empty_memory_disclosure(response)
