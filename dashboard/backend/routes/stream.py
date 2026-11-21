"""SSE /api/stream endpoint."""
from __future__ import annotations

from fastapi import APIRouter
from sse_starlette.sse import EventSourceResponse

from .. import sse

router = APIRouter(prefix="/api", tags=["stream"])


@router.get("/stream")
async def stream():
    q = await sse.broadcaster.subscribe()
    return EventSourceResponse(
        sse.sse_stream(q),
        # Keep-alive pings every 15s so proxies don't close idle connections
        ping=15,
    )
