"""Parallel, fail-soft coordination of long-term memory sources."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Iterable

from .remio import MemoryHit


def retrieve_parallel(
    sources: Iterable[Callable[[], list[MemoryHit]]],
    *,
    timeout_sec: float,
) -> list[MemoryHit]:
    """Collect partial results without waiting for a stalled source."""
    functions = list(sources)
    if not functions:
        return []
    hits: list[MemoryHit] = []
    pool = ThreadPoolExecutor(max_workers=len(functions), thread_name_prefix="jarvis-memory")
    futures = [pool.submit(source) for source in functions]
    try:
        for future in as_completed(futures, timeout=max(0.01, timeout_sec)):
            try:
                hits.extend(future.result() or [])
            except Exception:
                continue
    except TimeoutError:
        pass
    finally:
        for future in futures:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
    return hits
