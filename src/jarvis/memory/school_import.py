"""Explicit, bounded import of Felix's school notes into graph memory."""

from __future__ import annotations

import argparse
import hashlib
import sys
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import load_settings
from ..debug import debug_log
from ..llm import Tier, resolve_model
from .graph import BRANCH_SCHOOL, GraphMemoryStore
from .graph_ops import extract_graph_memories, place_graph_facts
from .vault.index import VaultIndex, get_vault_index


SCHOOL_NOTE_ROOTS = ("05 - Schule", "Daily/Todo Liste.md")
SCHOOL_IMPORT_MAX_NOTES = 100
SCHOOL_IMPORT_MAX_CHARS_PER_NOTE = 20_000
_SCHOOL_IMPORTER_ID = "school"
_SCHOOL_IMPORT_VERSION = "1"
_SCHOOL_EXTRACTION_FOCUS = (
    "Extract only durable facts about Felix's schooling: subjects, teachers, "
    "homework, exam dates, marks, timetable, and academic progress. Ignore "
    "every item unrelated to schooling. Treat a school-specific fact as "
    "SCHOOL even when it is grammatically phrased as a fact about Felix."
)


@dataclass(frozen=True)
class SchoolImportResult:
    """Aggregate outcome of one school-note import run."""

    notes_processed: int
    facts_stored: int
    duplicates_skipped: int
    notes_unchanged: int = 0
    errors: int = 0


def import_school_notes(
    *,
    store: GraphMemoryStore,
    cfg,
    index: VaultIndex | None = None,
    max_notes: int = SCHOOL_IMPORT_MAX_NOTES,
    max_chars_per_note: int = SCHOOL_IMPORT_MAX_CHARS_PER_NOTE,
    picker_model: str | None = None,
) -> SchoolImportResult:
    """Read the explicit school sources and file extracted facts in School.

    The vault side is read-only. Every extracted fact is passed to the graph's
    shared branch-pinned placement, Unicode-aware dedupe, merge, and split path.
    """
    if not getattr(cfg, "obsidian_read_enabled", True):
        raise ValueError("Obsidian vault reading is disabled")
    vault_path = getattr(cfg, "obsidian_vault_path", None)
    if not vault_path:
        raise ValueError("Obsidian vault path is not configured")

    vault_index = index or get_vault_index(
        vault_path,
        getattr(cfg, "obsidian_memory_folder", "Jarvis") or "Jarvis",
        getattr(cfg, "obsidian_index_max_file_kb", 512),
    )
    notes = vault_index.read_notes(
        SCHOOL_NOTE_ROOTS,
        max_notes=max_notes,
        max_chars_per_note=max_chars_per_note,
    )
    debug_log(
        f"school import: processing {len(notes)} bounded vault note(s)",
        "memory",
    )

    facts_stored = 0
    duplicates_skipped = 0
    notes_unchanged = 0
    errors = 0
    resolved_picker = picker_model or resolve_model(cfg, Tier.FAST)
    chat_model = getattr(cfg, "llm_chat_model", None) or getattr(
        cfg, "ollama_chat_model", "",
    )
    timeout_sec = float(getattr(cfg, "llm_chat_timeout_sec", 30.0))
    thinking = bool(getattr(cfg, "llm_thinking_enabled", False))

    for note in notes:
        try:
            content_hash = hashlib.sha256(
                f"{_SCHOOL_IMPORT_VERSION}\0{note.body}".encode("utf-8")
            ).hexdigest()
            if store.import_source_is_current(
                _SCHOOL_IMPORTER_ID, note.path, content_hash,
            ):
                notes_unchanged += 1
                debug_log(
                    f"school import: unchanged source skipped ({note.path})",
                    "memory",
                )
                continue
            debug_log(
                f"school import: extracting from {note.path} "
                f"({len(note.body)} chars)",
                "memory",
            )
            extracted = extract_graph_memories(
                summary=note.body,
                cfg=cfg,
                chat_model=chat_model,
                timeout_sec=timeout_sec,
                thinking=thinking,
                focus=_SCHOOL_EXTRACTION_FOCUS,
                untrusted_data=True,
            )
            school_facts = [(BRANCH_SCHOOL, fact) for _, fact in extracted]
            placed = place_graph_facts(
                store=store,
                facts=school_facts,
                cfg=cfg,
                chat_model=chat_model,
                timeout_sec=timeout_sec,
                thinking=thinking,
                picker_model=resolved_picker,
            )
            facts_stored += len(placed.stored)
            duplicates_skipped += placed.skipped
            store.mark_import_source(
                _SCHOOL_IMPORTER_ID, note.path, content_hash,
            )
        except Exception as exc:
            errors += 1
            debug_log(
                f"school import: note failed ({note.path}): "
                f"{type(exc).__name__}",
                "memory",
            )

    debug_log(
        f"school import: stored {facts_stored}, skipped "
        f"{duplicates_skipped}, unchanged {notes_unchanged}, errors {errors}",
        "memory",
    )
    return SchoolImportResult(
        notes_processed=len(notes),
        facts_stored=facts_stored,
        duplicates_skipped=duplicates_skipped,
        notes_unchanged=notes_unchanged,
        errors=errors,
    )


def main() -> int:
    """Run the explicit school import against the configured local store."""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    parser = argparse.ArgumentParser(description="Import school notes into graph memory.")
    parser.add_argument(
        "--vault",
        help="Absolute Obsidian vault path for this read-only import run.",
    )
    args = parser.parse_args()

    cfg = load_settings()
    if args.vault:
        vault_override = Path(args.vault).expanduser()
        if not vault_override.is_dir():
            print("📚 School memory import", flush=True)
            print("   ⚠️ The supplied Obsidian vault path is not a directory.", flush=True)
            return 1
        cfg = replace(cfg, obsidian_vault_path=str(vault_override))
    print("📚 School memory import", flush=True)
    if not getattr(cfg, "obsidian_vault_path", None):
        print("   ⚠️ Obsidian vault path is not configured.", flush=True)
        return 1
    if not getattr(cfg, "obsidian_read_enabled", True):
        print("   ⚠️ Obsidian vault reading is disabled.", flush=True)
        return 1

    store = GraphMemoryStore(cfg.db_path)
    try:
        result = import_school_notes(store=store, cfg=cfg)
    finally:
        store.close()

    print(f"   📝 Notes processed: {result.notes_processed}", flush=True)
    print(f"   🧠 Facts stored: {result.facts_stored}", flush=True)
    print(f"   ♻️ Duplicates skipped: {result.duplicates_skipped}", flush=True)
    print(f"   ⏭️ Unchanged notes skipped: {result.notes_unchanged}", flush=True)
    if result.errors:
        print(f"   ⚠️ Notes with errors: {result.errors}", flush=True)
        return 1
    print("   ✅ School memory import complete.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
