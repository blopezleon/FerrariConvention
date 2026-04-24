"""Hot-path telemetry sink for the SCALPEL router.

record() does exactly two O(1), GIL-protected operations then returns:
  1. deque.append()       — in-memory ring buffer (~100 ns)
  2. queue.put_nowait()   — hands off to daemon writer thread (~100 ns)

All disk I/O stays in the background thread. The Twisted reactor thread
(handle_command hot path) never touches a file descriptor.
"""
from __future__ import annotations

import collections
import json
import queue
import threading
import time
from pathlib import Path

_MAXLEN = 1000

# In-process ring buffer — read by anything imported in the same process.
events: collections.deque = collections.deque(maxlen=_MAXLEN)

_write_queue: queue.SimpleQueue = queue.SimpleQueue()

# scalpel/dashboard/telemetry.py  →  repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]
LOG_PATH = _REPO_ROOT / "var" / "log" / "cowrie" / "scalpel.jsonl"


def record(
    tier: int,
    cmd: str,
    latency_ms: float,
    session_id: str,
    outcome: str = "ok",
) -> None:
    """Record one command dispatch. Never blocks the caller."""
    ev = {
        "ts": time.time(),
        "tier": tier,
        "cmd": cmd[:200],
        "latency_ms": round(latency_ms, 3),
        "session_id": session_id,
        "outcome": outcome,
    }
    events.append(ev)
    _write_queue.put_nowait(ev)


def _writer() -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", buffering=1) as f:  # line-buffered
        while True:
            ev = _write_queue.get()
            f.write(json.dumps(ev) + "\n")


threading.Thread(target=_writer, daemon=True, name="scalpel-tel").start()
