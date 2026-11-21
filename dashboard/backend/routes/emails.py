"""Gmail-backed endpoints for the Life page Inbox block."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException

from .. import calendar_sync, gmail_sync

router = APIRouter(prefix="/api/emails", tags=["emails"])


@router.get("/status")
def status():
    return {
        "configured": calendar_sync.is_configured(),
        "connected": calendar_sync.is_connected() and gmail_sync.has_gmail_scope(),
        "can_modify": gmail_sync.can_modify(),
    }


@router.get("/recent")
async def recent():
    if not gmail_sync.has_gmail_scope():
        # 200 with empty list + connected flag handled by frontend, but a 401 here
        # is more explicit and matches how calendar/sync signals "reconnect needed".
        raise HTTPException(
            status_code=401,
            detail="Gmail scope not granted. Reconnect Google from settings.",
        )
    try:
        return await asyncio.to_thread(gmail_sync.list_recent_messages, 15)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail list failed: {e}")


@router.get("/{message_id}")
async def get_one(message_id: str, allow_images: bool = False):
    if not gmail_sync.has_gmail_scope():
        raise HTTPException(status_code=401, detail="Gmail scope not granted.")
    try:
        return await asyncio.to_thread(
            gmail_sync.get_message, message_id, allow_images=allow_images
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Gmail fetch failed: {e}")
