"""Google OAuth2 auth flow endpoints."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from .. import calendar_sync

router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/google")
def google_auth_initiate():
    if not calendar_sync.is_configured():
        raise HTTPException(503, "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET not set")
    return {"auth_url": calendar_sync.get_auth_url()}


@router.get("/google/status")
def google_auth_status():
    return {
        "configured": calendar_sync.is_configured(),
        "connected": calendar_sync.is_connected(),
    }


@router.get("/google/callback")
async def google_callback(code: str, state: str):
    try:
        calendar_sync.exchange_and_store(code, state)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, f"Token exchange failed: {e}")
    return RedirectResponse("/app/settings")


@router.post("/google/disconnect")
def google_disconnect():
    calendar_sync.disconnect()
    return {"ok": True}
