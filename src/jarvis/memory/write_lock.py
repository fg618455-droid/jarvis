"""Single-writer serialisation for memory mutations.

Diary summaries and graph nodes are read, sent through an LLM round trip,
and written back by several independent threads: the ambient digest
worker, the reply-turn pipeline, and the control centre's manual editing
endpoints. Each individual store call is already atomic, but the
read-transform-write span across the LLM call is not — two threads racing
the same diary day or the same graph node can each read the pre-write
value, compute independently, and the second write silently discards the
first. Holding this lock for the full span turns concurrent mutations into
one writer at a time.

Reentrant so a caller that already holds the lock (e.g. one memory
mutation invoking another) does not deadlock against itself.
"""

from __future__ import annotations

import threading

MEMORY_WRITE_LOCK = threading.RLock()
