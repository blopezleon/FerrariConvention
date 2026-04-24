"""SCALPEL telemetry dashboard server.

Usage:
    python -m scalpel.dashboard.server          # default port 8765
    DASHBOARD_PORT=9000 python -m scalpel.dashboard.server

Tails two log files (created once Cowrie + scalpel are running):
    var/log/cowrie/scalpel.jsonl   per-command tier + latency
    var/log/cowrie/cowrie.json     session / login events from Cowrie

Endpoints:
    GET /              → dashboard HTML
    GET /events        → SSE stream  (one JSON object per "data:" line)
    GET /api/snapshot  → current aggregated stats as JSON
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from pathlib import Path
from typing import AsyncIterator

try:
    import uvicorn
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
except ImportError:
    raise SystemExit(
        "Dashboard deps missing. Run:  pip install fastapi uvicorn"
    )

# ── Paths ─────────────────────────────────────────────────────────────────────
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
_SCALPEL_LOG = _REPO_ROOT / "var" / "log" / "cowrie" / "scalpel.jsonl"
_COWRIE_LOG = _REPO_ROOT / "var" / "log" / "cowrie" / "cowrie.json"
_INDEX_HTML = _HERE / "static" / "index.html"

# ── In-memory state ────────────────────────────────────────────────────────────
_tier_counts: dict[int, int] = {1: 0, 2: 0, 3: 0}
_tier_latencies: dict[int, deque] = {
    1: deque(maxlen=500),
    2: deque(maxlen=500),
    3: deque(maxlen=500),
}
_recent_commands: deque = deque(maxlen=100)
_recent_alerts: deque = deque(maxlen=30)
_session_count = 0
_login_attempts = 0

# SSE subscriber queues — one asyncio.Queue per connected browser tab.
_subscribers: set[asyncio.Queue] = set()


# ── SSE broadcast ──────────────────────────────────────────────────────────────
def _push(event_type: str, data: dict) -> None:
    payload = json.dumps({"type": event_type, **data})
    dead: set[asyncio.Queue] = set()
    for q in _subscribers:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            dead.add(q)
    _subscribers.difference_update(dead)


# ── Log tailers ────────────────────────────────────────────────────────────────
async def _tail_scalpel() -> None:
    global _tier_counts
    while not _SCALPEL_LOG.exists():
        await asyncio.sleep(1)

    with _SCALPEL_LOG.open("r") as f:
        f.seek(0, 2)  # start from tail — snapshot endpoint handles history
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            tier = ev.get("tier", 0)
            lat = ev.get("latency_ms", 0.0)
            cmd = ev.get("cmd", "")
            sid = ev.get("session_id", "")
            outcome = ev.get("outcome", "ok")

            _tier_counts[tier] = _tier_counts.get(tier, 0) + 1
            if tier in _tier_latencies:
                _tier_latencies[tier].append(lat)

            entry = {
                "ts": ev.get("ts", time.time()),
                "tier": tier,
                "cmd": cmd,
                "latency_ms": lat,
                "session_id": sid,
                "outcome": outcome,
            }
            _recent_commands.appendleft(entry)
            if tier == 3:
                _recent_alerts.appendleft(entry)

            _push("command", entry)


async def _tail_cowrie() -> None:
    global _session_count, _login_attempts
    while not _COWRIE_LOG.exists():
        await asyncio.sleep(1)

    with _COWRIE_LOG.open("r") as f:
        f.seek(0, 2)
        while True:
            line = f.readline()
            if not line:
                await asyncio.sleep(0.05)
                continue
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue

            eid = ev.get("eventid", "")
            if eid in ("cowrie.login.success", "cowrie.login.failed"):
                _login_attempts += 1
                _push("login", {
                    "eventid": eid,
                    "src_ip": ev.get("src_ip", ""),
                    "username": ev.get("username", ""),
                    "total": _login_attempts,
                })
            elif eid == "cowrie.session.connect":
                _session_count += 1
                _push("session", {
                    "src_ip": ev.get("src_ip", ""),
                    "total": _session_count,
                })


# ── FastAPI ────────────────────────────────────────────────────────────────────
app = FastAPI(title="SCALPEL Dashboard", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def _startup() -> None:
    asyncio.create_task(_tail_scalpel())
    asyncio.create_task(_tail_cowrie())


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return _INDEX_HTML.read_text(encoding="utf-8")


@app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    def _pct(data: deque, p: float) -> float:
        lst = sorted(data)
        if not lst:
            return 0.0
        return lst[min(int(len(lst) * p / 100), len(lst) - 1)]

    return JSONResponse({
        "tier_counts": _tier_counts,
        "latency": {
            str(t): {
                "p50": round(_pct(_tier_latencies[t], 50), 3),
                "p95": round(_pct(_tier_latencies[t], 95), 3),
            }
            for t in (1, 2, 3)
        },
        "recent_commands": list(_recent_commands)[:50],
        "recent_alerts": list(_recent_alerts)[:20],
        "session_count": _session_count,
        "login_attempts": _login_attempts,
    })


@app.get("/events")
async def sse() -> StreamingResponse:
    async def _stream() -> AsyncIterator[str]:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        _subscribers.add(q)
        try:
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=20)
                    yield f"data: {payload}\n\n"
                except asyncio.TimeoutError:
                    yield 'data: {"type":"ping"}\n\n'
        except asyncio.CancelledError:
            pass
        finally:
            _subscribers.discard(q)

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Entry point ────────────────────────────────────────────────────────────────
def main() -> None:
    port = int(os.environ.get("DASHBOARD_PORT", 8765))
    print(f"SCALPEL dashboard → http://localhost:{port}")
    uvicorn.run(
        "scalpel.dashboard.server:app",
        host="0.0.0.0",
        port=port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
