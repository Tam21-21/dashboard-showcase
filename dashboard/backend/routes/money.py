"""Money routes (lane MY1): the snapshot ingest, the two page reads, and the budget targets.

Thin: every decision lives in dashboard/backend/money.py. The ingest router has its own prefix and
its own key (X-Money-Ingest-Key), a different principal from the worker, so it does not sit under
/api/research/worker. Caddy lets /api/ingest/* past the login; this key is the only check there.
"""
from __future__ import annotations

import hmac
import os
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import money, money_refresh
from ..deps import get_user_conn

router = APIRouter(prefix="/api/money", tags=["money"])


def require_money_ingest_key(x_money_ingest_key: str = Header(default="")) -> None:
    """Constant-time comparison, modelled on routes/research.py require_worker_api_key."""
    expected = os.environ.get("MONEY_INGEST_API_KEY", "")
    if not expected:
        raise HTTPException(status_code=500, detail="The money ingest key is not set on the server.")
    if not hmac.compare_digest(x_money_ingest_key.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="The money ingest key is wrong.")


ingest_router = APIRouter(
    prefix="/api/ingest/money", tags=["money"], dependencies=[Depends(require_money_ingest_key)]
)


@ingest_router.post("/snapshots")
def post_snapshot(body: Any = Body(...), conn: sqlite3.Connection = Depends(get_user_conn)):
    try:
        result = money.ingest(conn, body)
    except money.MoneyInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result["duplicate"]:
        return JSONResponse(status_code=200, content={"duplicate": True})
    return JSONResponse(status_code=201, content=result)


@router.post("/refresh")
def post_refresh() -> dict:
    try:
        return money_refresh.fire()
    except money_refresh.RefreshError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc


@router.get("/overview")
def get_overview(conn: sqlite3.Connection = Depends(get_user_conn)):
    return money.overview(conn)


@router.get("/budget")
def get_budget(conn: sqlite3.Connection = Depends(get_user_conn)):
    return money.budget(conn)


class TargetBody(BaseModel):
    amount_eur: float


@router.get("/targets")
def get_targets(conn: sqlite3.Connection = Depends(get_user_conn)):
    return {"targets": money.list_targets(conn)}


@router.put("/targets/{slug}")
def put_target(slug: str, body: TargetBody, conn: sqlite3.Connection = Depends(get_user_conn)):
    try:
        money.set_target(conn, slug, body.amount_eur)
    except money.MoneyInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"slug": slug, "amount_eur": body.amount_eur}


@router.delete("/targets/{slug}")
def delete_target(slug: str, conn: sqlite3.Connection = Depends(get_user_conn)):
    try:
        money.delete_target(conn, slug)
    except money.MoneyInputError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except money.TargetNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"deleted": slug}
