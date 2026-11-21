"""Reserve exam routes: dispatch the reserve exam for an already-graded survivor, and read the
enumerated windows' own supply. Lane RE (6.4), docs/plans/night/2026-09-15_set9_night/PLAN.md
section 8.

Both routes carry no auth dependency, matching every other browser route in this backend
(dashboard/backend/CLAUDE.md: "Caddy basic auth handles humans at the edge"). No frontend consumes
this router tonight (the dry run's own leg calls it directly); the page is a sprint note for
October, named in this lane's own report.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from research_lab import universe as universe_rule

from .. import plain_refusals, reserve_exam, researchdb, reservedb, stage_runner, tracker

log = logging.getLogger("dashboard.reserve")

router = APIRouter(prefix="/api/research/reserve-exam", tags=["reserve-exam"])


class ReserveExamDispatchIn(BaseModel):
    strategy_id: int
    seed: int
    calendar_id: str = "real"  # matches PriceUniverseDispatchIn's own default; a dry run passes its own compressed calendar explicitly


def _window_label(window_start_ts: int, window_end_ts: int) -> str:
    """A human-readable name for a window (section 16's own "a window name" requirement), never
    the machine index alone: the two calendar dates a person actually cares about."""
    start = datetime.fromtimestamp(window_start_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    end = datetime.fromtimestamp(window_end_ts, tz=timezone.utc).strftime("%Y-%m-%d")
    return f"{start} to {end}"


@router.post("/dispatch", status_code=201)
def dispatch_reserve_exam(body: ReserveExamDispatchIn):
    """Every refusal below is a 409 whose detail is one reason word plus one sentence, per this
    lane's own criterion 6, never a bare stack trace or an opaque 500. The reason words match the
    ones this lane's own exceptions already carry in their own str(); a exception outside this
    module's own vocabulary (the real, unchanged stage_runner or the universe rule underneath it)
    is given the same "reason: sentence" shape here, at the one place a browser response is
    actually assembled, rather than by reaching back into those frozen files to add one.
    """
    now_ts = int(time.time())
    try:
        result = reserve_exam.dispatch_exam(body.strategy_id, body.seed, body.calendar_id, now_ts)
    except reserve_exam.ReserveExamError as exc:
        # Every exception this lane's own module raises already states its own reason word first
        # (dispatch_exam's own six classes, each built "reason-word: sentence").
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except tracker.ReserveCutCrossed as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except tracker.ChronologyRefused as exc:
        log.warning("chronology-refused: %s", exc)
        raise HTTPException(
            status_code=409, detail=f"chronology-refused: {plain_refusals.sentence(exc)}"
        ) from exc
    except tracker.DrawWindowMissing as exc:
        log.warning("draw-window-missing: %s", exc)
        raise HTTPException(
            status_code=409, detail=f"draw-window-missing: {plain_refusals.sentence(exc)}"
        ) from exc
    except tracker.RunLinkFailed as exc:
        log.warning("run-link-failed: %s", exc)
        raise HTTPException(
            status_code=409, detail=f"run-link-failed: {plain_refusals.sentence(exc)}"
        ) from exc
    except stage_runner.CandidateVersionMoved as exc:
        raise HTTPException(status_code=409, detail=f"candidate-version-moved: {exc}") from exc
    except stage_runner.LeadInUnavailable as exc:
        raise HTTPException(status_code=409, detail=f"lead-in-unavailable: {exc}") from exc
    except stage_runner.WarmupUnsatisfied as exc:
        raise HTTPException(status_code=409, detail=f"warmup-unsatisfied: {exc}") from exc
    except stage_runner.DailyStoreNotConfigured as exc:
        raise HTTPException(status_code=409, detail=f"daily-store-not-configured: {exc}") from exc
    except universe_rule.UniverseRuleError as exc:
        # The universe rule's own base class (ContaminationListEmpty/Inert/Unavailable,
        # UniverseParameterOwed, TooFewQualifyingNames): build_screen_universe propagates every
        # one of these UNCHANGED (stage_runner.py's own docstring), so this is the one place a
        # browser response for this route ever assembles their own reason word.
        reason = "".join(
            f"-{c.lower()}" if c.isupper() else c for c in type(exc).__name__
        ).lstrip("-")
        log.warning("%s: %s", reason, exc)
        if isinstance(exc, plain_refusals.PLAIN_UNIVERSE_REFUSALS):
            raise HTTPException(
                status_code=409, detail=f"{reason}: {plain_refusals.sentence(exc)}"
            ) from exc
        raise HTTPException(status_code=409, detail=f"{reason}: {exc}") from exc

    return {
        "exam": result,
        "window_name": _window_label(result["window_start_ts"], result["window_end_ts"]),
    }


@router.get("/supply")
def get_reserve_supply():
    """A read-only view of the enumerated reserve windows and which are already spent (AC-25's
    own supply, never a stored roster: recomputed on every call from the live provenance register
    and the live verdict store, exactly like reserve_exam.draw_window itself reads it)."""
    now_ts = int(time.time())
    with researchdb.get_conn() as conn:
        supply = reservedb.query_reserve_supply(conn, now_ts)
    windows = [
        {
            "index": w["index"],
            "name": _window_label(w["start_ts"], w["end_ts"]),
            "start_ts": w["start_ts"],
            "end_ts": w["end_ts"],
            "spent": w["spent"],
        }
        for w in supply["windows"]
    ]
    return {"enumerated": supply["enumerated"], "spent": supply["spent"], "windows": windows}
