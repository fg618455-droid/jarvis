"""Voice intent accuracy of the LLM judge and of the deterministic rules.

The intent judge decides whether an utterance was aimed at the assistant, and
it decides it with a local model. A cloud round trip per speech segment is
neither fast enough nor cheap enough to replace it, so the rules that already
run when the judge is unavailable become the normal path. This module scores
both against the existing intent-judge eval catalogue, so the cost of that
swap is a measured gap rather than a guess.

The rule engine here is the listener's judge-free path, composed in one place
from the shipped primitives (`is_wake_word_detected`, `extract_query_after_wake`,
`is_stop_command`, the partial-ratio echo check) with the listener's own
thresholds. It is the measurement of what exists, and the starting point the
replacement has to beat.

Run it (needs Ollama with the judge model for the LLM half):

    python -m evals.baselines.voice_intent

🎙️ Writes ``docs/baselines/voice_intent_baseline.json``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _path in (REPO_ROOT / "src", REPO_ROOT / "evals", REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

BASELINE_PATH = REPO_ROOT / "docs" / "baselines" / "voice_intent_baseline.json"

JUDGE_MODEL = "gemma4:e2b"

# The listener's echo thresholds, which it holds as inline literals with
# nothing to import. Everything else the rules need comes from the config
# defaults, so changing a wake word or a stop command stays measured.
ECHO_SIMILARITY_THRESHOLD = 70
ECHO_LENGTH_FACTOR = 1.3
ECHO_LENGTH_MARGIN = 3


@dataclass(frozen=True)
class Verdict:
    directed: bool
    query: str
    stop: bool


@dataclass(frozen=True)
class Case:
    name: str
    kind: str  # "single" | "multi"
    text: str  # the utterance the decision is made on
    segments: tuple
    last_tts_text: str
    in_hot_window: bool
    wake_timestamp: Optional[float]
    expected_directed: bool
    expected_stop: bool
    expected_contains: tuple = field(default=())
    expected_not_contains: tuple = field(default=())
    aliases: tuple = field(default=())


def _as_tuple(value) -> tuple:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def _load_catalogue_module():
    """Import the eval catalogue by path.

    ``evals/test_intent_judge.py`` and ``tests/test_intent_judge.py`` share a
    module name, so a plain import returns whichever pytest collected first.
    """
    import importlib.util

    path = REPO_ROOT / "evals" / "test_intent_judge.py"
    spec = importlib.util.spec_from_file_location("_eval_intent_judge_catalogue", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_cases() -> list:
    """Read the intent-judge eval catalogue as a flat list of cases."""
    catalogue = _load_catalogue_module()

    cases = []
    for raw in catalogue.INTENT_JUDGE_TEST_CASES:
        cases.append(Case(
            name=raw.name,
            kind="single",
            text=raw.transcript,
            segments=((raw.transcript, False),),
            last_tts_text=raw.last_tts_text,
            in_hot_window=raw.in_hot_window,
            wake_timestamp=raw.wake_timestamp,
            expected_directed=raw.expected_directed,
            expected_stop=raw.expected_stop,
            expected_contains=_as_tuple(raw.expected_query_contains),
            expected_not_contains=_as_tuple(raw.expected_query_not_contains),
        ))
    for raw in catalogue.MULTI_SEGMENT_TEST_CASES:
        # The decision is made on the last segment that is not assistant echo,
        # which is the one the listener processes when speech ends.
        current = ""
        for text, is_during_tts in reversed(raw.segments):
            if not is_during_tts:
                current = text
                break
        cases.append(Case(
            name=raw.name,
            kind="multi",
            text=current,
            segments=tuple(tuple(segment) for segment in raw.segments),
            last_tts_text=raw.last_tts_text,
            in_hot_window=raw.in_hot_window,
            wake_timestamp=raw.wake_timestamp,
            expected_directed=raw.expected_directed,
            expected_stop=raw.expected_stop,
            expected_contains=_as_tuple(raw.expected_query_contains),
            expected_not_contains=_as_tuple(raw.expected_query_not_contains),
            aliases=_as_tuple(raw.aliases),
        ))
    return cases


def _is_pure_echo(text: str, last_tts_text: str) -> bool:
    """The listener's echo rule: similar enough, and no longer than the reply."""
    if not last_tts_text:
        return False
    from rapidfuzz import fuzz

    similarity = fuzz.partial_ratio(text.lower(), last_tts_text.lower())
    tts_words = len(last_tts_text.split())
    text_words = len(text.split())
    return (
        similarity >= ECHO_SIMILARITY_THRESHOLD
        and text_words <= max(tts_words * ECHO_LENGTH_FACTOR, tts_words + ECHO_LENGTH_MARGIN)
    )


def judge_by_rules(case: Case) -> Verdict:
    """Decide directedness without a model, the way the listener does today."""
    from jarvis.config import get_default_config
    from jarvis.listening.wake_detection import (
        extract_query_after_wake,
        is_stop_command,
        is_wake_word_detected,
    )

    defaults = get_default_config()
    wake_word = defaults["wake_word"]
    aliases = sorted(set(defaults["wake_aliases"]) | {wake_word} | set(case.aliases))
    text = case.text.lower()

    if is_stop_command(text, list(defaults["stop_commands"]), defaults["stop_command_fuzzy_ratio"]):
        return Verdict(directed=False, query="", stop=True)

    if case.in_hot_window:
        if _is_pure_echo(text, case.last_tts_text):
            return Verdict(directed=False, query="", stop=False)
        # No wake word is needed inside the window, so the whole utterance is
        # the query. Rules cannot tell a follow-up from a bystander's remark.
        return Verdict(directed=True, query=case.text, stop=False)

    if is_wake_word_detected(text, wake_word, aliases, defaults["wake_fuzzy_ratio"]):
        return Verdict(
            directed=True,
            query=extract_query_after_wake(text, wake_word, aliases),
            stop=False,
        )

    return Verdict(directed=False, query="", stop=False)


def score(case: Case, verdict: Optional[Verdict]) -> bool:
    """Apply the catalogue's own pass criteria to a verdict."""
    if verdict is None:
        return False
    if verdict.directed != case.expected_directed:
        return False
    if verdict.stop != case.expected_stop:
        return False
    query = (verdict.query or "").lower()
    if any(needle.lower() not in query for needle in case.expected_contains):
        return False
    if query and any(needle.lower() in query for needle in case.expected_not_contains):
        return False
    return True


def _judge_by_llm(case: Case, model: str) -> Optional[Verdict]:
    from jarvis.listening.intent_judge import IntentJudge, IntentJudgeConfig
    from jarvis.listening.transcript_buffer import TranscriptSegment

    judge = IntentJudge(IntentJudgeConfig(
        assistant_name="Jarvis",
        aliases=list(case.aliases),
        model=model,
        timeout_sec=10.0,
    ))
    if not judge.available:
        return None

    segments = [
        TranscriptSegment(
            text=text,
            start_time=1000.0 + index * 2.0,
            end_time=1002.0 + index * 2.0,
            energy=0.01,
            is_during_tts=is_during_tts,
            processed=False,
        )
        for index, (text, is_during_tts) in enumerate(case.segments)
    ]
    judgment = judge.judge(
        segments=segments,
        wake_timestamp=case.wake_timestamp,
        last_tts_text=case.last_tts_text,
        last_tts_finish_time=999.0 if case.last_tts_text else 0.0,
        in_hot_window=case.in_hot_window,
        current_text=case.text,
    )
    if judgment is None:
        return None
    return Verdict(
        directed=bool(judgment.directed),
        query=judgment.query or "",
        stop=bool(judgment.stop),
    )


def _summarise(cases, verdicts) -> dict:
    passed = [case.name for case, verdict in zip(cases, verdicts) if score(case, verdict)]
    failed = [case.name for case, verdict in zip(cases, verdicts) if not score(case, verdict)]
    by_kind = {}
    for kind in sorted({case.kind for case in cases}):
        subset = [(c, v) for c, v in zip(cases, verdicts) if c.kind == kind]
        by_kind[kind] = round(
            sum(score(c, v) for c, v in subset) / len(subset), 4
        )
    return {
        "passed": len(passed),
        "failed": len(failed),
        "accuracy": round(len(passed) / len(cases), 4),
        "accuracy_by_kind": by_kind,
        "failed_cases": failed,
    }


def main() -> int:
    cases = load_cases()

    print("🎙️ Voice intent baseline")
    print(f"   🔢 Catalogue: {len(cases)} cases "
          f"({sum(c.kind == 'single' for c in cases)} single, "
          f"{sum(c.kind == 'multi' for c in cases)} multi-segment)")

    rules = _summarise(cases, [judge_by_rules(case) for case in cases])
    print(f"   📐 Deterministic rules: {rules['accuracy']:.2%} "
          f"({rules['passed']}/{len(cases)})")

    print(f"   🧠 Intent judge ({JUDGE_MODEL}) …")
    llm_verdicts = []
    for case in cases:
        llm_verdicts.append(_judge_by_llm(case, JUDGE_MODEL))
    if all(verdict is None for verdict in llm_verdicts):
        print("   ❌ The judge answered nothing at all. Without its score there is")
        print("      no gap to record, so no baseline is written.")
        return 1
    judge = _summarise(cases, llm_verdicts)
    print(f"   🧠 Intent judge: {judge['accuracy']:.2%} ({judge['passed']}/{len(cases)})")

    judge["model"] = JUDGE_MODEL
    report = {
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "cases": len(cases),
        "catalogue": "evals/test_intent_judge.py",
        "rules": rules,
        "intent_judge": judge,
        "gap_percentage_points": round(
            (judge["accuracy"] - rules["accuracy"]) * 100, 2
        ),
    }

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print(f"   📉 Gap: {report['gap_percentage_points']:.2f} percentage points")
    print(f"   💾 Written to {BASELINE_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
