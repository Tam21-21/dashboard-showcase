"""SSE broadcaster for Life-tab events.

A simple in-process pub/sub fanned out to subscribed clients via per-client
asyncio.Queues. Backend code calls `broadcaster.publish(event_type, payload)`
directly from route handlers (e.g. expense_added, todo_toggled, calendar_synced)
to invalidate the matching React Query keys on the frontend.

No background polling — v2 trade/event polling was removed when newsbot v2 was
retired. The broadcaster is kept alive across the app lifespan only so
publish() has a place to fan messages out and stop() has something to await.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

log = logging.getLogger(__name__)

_MAX_QUEUE = 256


@dataclass
class Broadcaster:
    subscribers: set[asyncio.Queue] = field(default_factory=set)
    _stop: asyncio.Event = field(default_factory=asyncio.Event)

    async def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> None:
        msg = {"type": event_type, **payload}
        dead: list[asyncio.Queue] = []
        for q in list(self.subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                log.warning("SSE subscriber queue full, dropping client")
                dead.append(q)
        for q in dead:
            self.subscribers.discard(q)

    async def start(self) -> None:
        log.info("SSE broadcaster started")

    async def stop(self) -> None:
        self._stop.set()


broadcaster = Broadcaster()


async def sse_stream(q: asyncio.Queue) -> AsyncIterator[dict[str, Any]]:
    """Yield sse-starlette compatible dicts. Sends an initial 'ready' event."""
    yield {"event": "ready", "data": json.dumps({"ts": time.time()})}
    try:
        while True:
            msg = await q.get()
            evt = msg.get("type", "message")
            yield {"event": evt, "data": json.dumps(msg, default=str)}
    finally:
        broadcaster.unsubscribe(q)
