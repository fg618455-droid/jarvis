"""Recall@3 of the hybrid memory search, measured against a fixed catalogue.

The hybrid search blends a vector half (60 %) with BM25 over the FTS5 index
(40 %). No subscription provider offers an embedding endpoint, so the vector
half goes away and retrieval becomes deterministic. This module records what
the hybrid search scores today so the size of that regression is a measured
number rather than an impression.

What is measured is the retrieval layer alone. Production feeds the search an
OR query over LLM-extracted keywords plus the same keywords as the embedded
text; the catalogue carries hand-written keywords per question in place of that
extractor, which disappears along with the local models. Including a live
extractor would mix two changes into one number.

Ranking uses ``PythonVectorStore`` directly rather than the global store
getter: the getter memoises one instance per process and the FAISS variant
pins 768 dimensions, either of which would leak between runs. Both stores
rank normalised vectors by cosine similarity, so the ordering is the same.

Run it with a live embedding backend:

    python -m evals.baselines.memory_recall

🧠 Writes ``docs/baselines/memory_recall_baseline.json``.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

BASELINE_PATH = REPO_ROOT / "docs" / "baselines" / "memory_recall_baseline.json"


@dataclass(frozen=True)
class Document:
    key: str
    date: str
    language: str
    topics: str
    summary: str


@dataclass(frozen=True)
class Question:
    query: str
    keywords: tuple[str, ...]
    expected: str
    kind: str  # "lexical" (words overlap) | "semantic" (paraphrase)


# A diary corpus in the two languages this household speaks. Entries overlap in
# vocabulary on purpose (three mention school, three mention the bike), so a
# query has to discriminate rather than just find the only match.
CORPUS: tuple[Document, ...] = (
    Document(
        "bike-gears", "2026-05-01", "de", "fahrrad, reparatur",
        "Felix hat das hintere Schaltwerk neu eingestellt, weil die Kette beim "
        "Hochschalten übersprang. Ein Ersatzzug liegt in der Werkstatt bereit.",
    ),
    Document(
        "bike-commute", "2026-05-02", "de", "fahrrad, schule",
        "Der Weg zur Schule mit dem Rad dauert zwölf Minuten, über den Feldweg "
        "sind es drei Minuten mehr, dafür ohne Ampeln.",
    ),
    Document(
        "guitar-lessons", "2026-05-03", "en", "music, practice",
        "Felix started weekly guitar lessons and is working through barre "
        "chords. His tutor asked him to practise twenty minutes a day.",
    ),
    Document(
        "maths-exam", "2026-05-04", "de", "schule, mathematik",
        "Die Schulaufgabe über quadratische Funktionen steht am zwölften Juni "
        "an. Felix will vor allem die Scheitelpunktform noch üben.",
    ),
    Document(
        "nas-migration", "2026-05-05", "en", "storage, backup",
        "Moved the household document archive onto the Synology and set the "
        "backup job to run at three in the morning so it never blocks the day.",
    ),
    Document(
        "sourdough", "2026-05-06", "de", "kochen, brot",
        "Der erste Sauerteig ist zu flach geraten. Das Anstellgut braucht eine "
        "wärmere Ecke in der Küche, sonst geht der Teig nicht auf.",
    ),
    Document(
        "knee-injury", "2026-05-07", "en", "health, sport",
        "Felix twisted his knee at football training. The physiotherapist told "
        "him to stay off it for two weeks and to swim instead.",
    ),
    Document(
        "monitor-savings", "2026-05-08", "de", "geld, technik",
        "Das Taschengeld wird neu aufgeteilt: eine feste Sparrate für einen "
        "zweiten Bildschirm, der Rest bleibt frei verfügbar.",
    ),
    Document(
        "croatia-holiday", "2026-05-09", "en", "travel, family",
        "The family booked a week on the Croatian coast in August. Felix is in "
        "charge of the packing list and wants to take the snorkel.",
    ),
    Document(
        "neighbour-dog", "2026-05-10", "de", "nachbarn, gesundheit",
        "Der Hund der Nachbarn löst bei Mama Niesanfälle aus, deshalb bleibt "
        "sie bei Besuchen lieber im Garten sitzen.",
    ),
    Document(
        "printer-network", "2026-05-11", "en", "hardware, setup",
        "Got the office printer onto the network after the driver install kept "
        "failing. The trick was giving it a fixed address in the router.",
    ),
    Document(
        "english-presentation", "2026-05-12", "de", "schule, englisch",
        "Für Englisch muss Felix ein Referat über erneuerbare Energien halten. "
        "Fünf Minuten, mit Handout, Abgabe eine Woche vorher.",
    ),
    Document(
        "gym-routine", "2026-05-13", "en", "sport, routine",
        "Settled on a three-day split at the gym: push, pull, legs, with the "
        "rest day on Wednesday because of the late lesson.",
    ),
    Document(
        "grandma-birthday", "2026-05-14", "de", "familie, geschenke",
        "Oma wird im Juli achtzig. Geplant ist ein Fotobuch von allen Enkeln, "
        "die Bilder müssen bis Ende Juni zusammen sein.",
    ),
    Document(
        "laptop-fan", "2026-05-15", "en", "hardware, repair",
        "The laptop got loud under load. Cleaning the dust out of the fan "
        "dropped it back to a whisper, so no new machine is needed yet.",
    ),
    Document(
        "swimming-pass", "2026-05-16", "de", "sport, schwimmen",
        "Die Zehnerkarte fürs Hallenbad ist noch bis September gültig, drei "
        "Besuche sind übrig.",
    ),
    Document(
        "coffee-machine", "2026-05-17", "en", "kitchen, repair",
        "The espresso machine needed descaling; the water in this area is hard "
        "enough that it wants doing every six weeks.",
    ),
    Document(
        "history-essay", "2026-05-18", "de", "schule, geschichte",
        "Der Aufsatz in Geschichte behandelt die Weimarer Republik und ist "
        "handschriftlich abzugeben, etwa acht Seiten.",
    ),
    Document(
        "plant-watering", "2026-05-19", "en", "home, routine",
        "Worked out the watering rhythm for the balcony: the tomatoes daily in "
        "the heat, the herbs every second day, the cactus almost never.",
    ),
    Document(
        "phone-contract", "2026-05-20", "de", "vertrag, handy",
        "Der Handyvertrag läuft im November aus. Kündigungsfrist ist drei "
        "Monate, also spätestens im August entscheiden.",
    ),
    Document(
        "chess-club", "2026-05-21", "en", "hobby, club",
        "Joined the chess club that meets on Thursdays. Felix plays the "
        "Sicilian and keeps losing the endgame rather than the opening.",
    ),
    Document(
        "car-service", "2026-05-22", "de", "auto, termin",
        "Der Wagen muss im Juni zur Inspektion, dabei sollen auch die "
        "Sommerreifen aufgezogen werden.",
    ),
    Document(
        "reading-list", "2026-05-23", "en", "books, reading",
        "Started keeping a reading list. Two novels and one non-fiction book a "
        "month turned out to be a pace he can actually hold.",
    ),
    Document(
        "window-draught", "2026-05-24", "de", "wohnung, handwerk",
        "Im Schlafzimmer zieht es am Fenster. Neue Dichtungen sind bestellt, "
        "der Rahmen selbst ist noch dicht.",
    ),
)


# Half the questions repeat words from their document, half only mean the same
# thing. The semantic half is what the vector component buys, and therefore
# what a keyword-only replacement is at risk of losing.
QUESTIONS: tuple[Question, ...] = (
    Question("Wann ist die Schulaufgabe über quadratische Funktionen?",
             ("Schulaufgabe", "quadratische", "Funktionen"), "maths-exam", "lexical"),
    Question("Wie lange dauert der Weg zur Schule mit dem Rad?",
             ("Weg", "Schule", "Rad"), "bike-commute", "lexical"),
    Question("What did the physiotherapist say about the knee?",
             ("physiotherapist", "knee"), "knee-injury", "lexical"),
    Question("When does the family fly to Croatia?",
             ("family", "Croatia"), "croatia-holiday", "lexical"),
    Question("Wann läuft der Handyvertrag aus?",
             ("Handyvertrag",), "phone-contract", "lexical"),
    Question("How often does the espresso machine need descaling?",
             ("espresso", "machine", "descaling"), "coffee-machine", "lexical"),
    Question("Worüber muss Felix das Referat in Englisch halten?",
             ("Referat", "Englisch"), "english-presentation", "lexical"),
    Question("Which day is the rest day in the gym split?",
             ("rest", "day", "gym", "split"), "gym-routine", "lexical"),
    Question("Wann muss der Wagen zur Inspektion?",
             ("Wagen", "Inspektion"), "car-service", "lexical"),
    Question("Welches Instrument lernt Felix gerade?",
             ("Instrument", "lernt"), "guitar-lessons", "semantic"),
    Question("Why can he not go running at the moment?",
             ("running", "moment"), "knee-injury", "semantic"),
    Question("Wofür spart Felix im Moment?",
             ("spart", "Felix"), "monitor-savings", "semantic"),
    Question("Warum ist das Brot nicht aufgegangen?",
             ("Brot", "aufgegangen"), "sourdough", "semantic"),
    Question("What made the laptop quiet again?",
             ("laptop", "quiet"), "laptop-fan", "semantic"),
    Question("Wer reagiert allergisch auf Tiere?",
             ("allergisch", "Tiere"), "neighbour-dog", "semantic"),
    Question("How often do the balcony tomatoes need water?",
             ("balcony", "tomatoes", "water"), "plant-watering", "semantic"),
    Question("Was ist für Omas achtzigsten geplant?",
             ("Oma", "achtzigsten", "geplant"), "grandma-birthday", "semantic"),
    Question("Which board game does Felix play on Thursdays?",
             ("board", "game", "Thursdays"), "chess-club", "semantic"),
)

Embedder = Callable[[str], Optional[list]]


def measure_recall(db_path, embed: Embedder, k: int = 3) -> dict:
    """Score Recall@k of the hybrid search over the fixed catalogue.

    ``embed`` maps a text to a vector; it takes the corpus entries and the
    questions alike. A ``None`` vector counts as an embedding failure and the
    query falls through to the FTS-only path, exactly as production does.
    """
    from jarvis.memory.db import Database
    from jarvis.utils.vector_store import PythonVectorStore

    db_path = Path(db_path)
    db = Database(str(db_path))
    db._python_vector_store = PythonVectorStore(str(db_path))

    key_to_id = {}
    for doc in CORPUS:
        summary_id = db.upsert_conversation_summary(
            date_utc=doc.date, summary=doc.summary, topics=doc.topics,
        )
        key_to_id[doc.key] = summary_id
        vector = embed(doc.summary)
        if vector is not None:
            db.upsert_summary_embedding(summary_id, vector)

    hits, missed = 0, []
    by_kind: dict[str, list[int]] = {}
    for question in QUESTIONS:
        # The shape production builds: an OR query over the extracted keywords
        # for FTS, the same keywords joined for the embedding.
        fts_query = " OR ".join(question.keywords[:5])
        vector = embed(" ".join(question.keywords))
        vec_json = json.dumps(vector) if vector is not None else None
        rows = db.search_hybrid(fts_query, vec_json, top_k=k)
        found = {row["id"] for row in rows}
        hit = key_to_id[question.expected] in found
        hits += hit
        if not hit:
            missed.append(question.query)
        by_kind.setdefault(question.kind, []).append(int(hit))

    db.close()
    return {
        "k": k,
        "questions": len(QUESTIONS),
        "corpus_size": len(CORPUS),
        "hits": hits,
        "misses": len(missed),
        "recall_at_k": round(hits / len(QUESTIONS), 4),
        # Split out because the vector half of the search is what carries the
        # paraphrased questions: that is the number a keyword-only replacement
        # has to be judged on.
        "recall_by_kind": {
            kind: round(sum(results) / len(results), 4)
            for kind, results in sorted(by_kind.items())
        },
        "missed_queries": missed,
    }


def main() -> int:
    import tempfile

    from jarvis.config import load_settings
    from jarvis.llm import get_embedding_backend

    cfg = load_settings()
    backend = get_embedding_backend(cfg)
    model = cfg.embedding_model

    print("🧠 Memory recall baseline")
    print(f"   🔢 Corpus: {len(CORPUS)} summaries, {len(QUESTIONS)} questions")
    print(f"   🧩 Embedding model: {model}")

    def embed(text: str):
        return backend.embed(text, model, timeout_sec=60.0)

    probe = embed("connectivity probe")
    if probe is None:
        print("   ❌ The embedding backend returned nothing. Without vectors this")
        print("      would silently measure FTS-only search, not the hybrid.")
        return 1
    print(f"   ✅ Backend reachable, {len(probe)} dimensions")

    with tempfile.TemporaryDirectory() as tmp:
        report = measure_recall(Path(tmp) / "baseline.db", embed, k=3)
        # The same catalogue with the vector half switched off, which is the
        # `elif safe_q` branch of search_hybrid. Phase 5 builds a richer
        # deterministic retrieval than this, so it is a floor rather than a
        # prediction, but it is a real code path measured on real data.
        fts_only = measure_recall(Path(tmp) / "fts_only.db", lambda _text: None, k=3)

    report.update({
        "fts_only": {
            "recall_at_k": fts_only["recall_at_k"],
            "recall_by_kind": fts_only["recall_by_kind"],
            "missed_queries": fts_only["missed_queries"],
        },
        "measured_at": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "retrieval": "hybrid",
        "vector_weight": 0.6,
        "fts_weight": 0.4,
        "embedding_model": model,
        "vector_store": "python",
        "query": "OR-joined keywords for FTS, joined keywords embedded",
        "note": (
            "The blend scores an FTS hit as 1/(1+bm25) * 0.4, and SQLite's "
            "bm25() is negative for a match, so the better a document matches "
            "the more the term subtracts. That is why the blend scores below "
            "its own FTS-only branch here. Compare a replacement against "
            "fts_only, not against the blend."
        ),
    })

    BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BASELINE_PATH.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"   📊 Recall@{report['k']} hybrid: {report['recall_at_k']:.2%} "
          f"({report['hits']}/{report['questions']})")
    for kind, value in report["recall_by_kind"].items():
        print(f"      • {kind}: {value:.2%}")
    print(f"   📊 Recall@{fts_only['k']} FTS only: {fts_only['recall_at_k']:.2%} "
          f"({fts_only['hits']}/{fts_only['questions']})")
    for kind, value in fts_only["recall_by_kind"].items():
        print(f"      • {kind}: {value:.2%}")
    print(f"   💾 Written to {BASELINE_PATH.relative_to(REPO_ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
