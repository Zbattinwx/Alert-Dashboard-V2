"""
Model training, health and backfill routes.

Lifted out of main.py, which had grown to 6,000 lines with 174 routes and
startup orchestration in one file. That shape has already cost something real:
the GLM lightning service sat three conditionals deep inside the radar startup,
so lightning silently required dashboard-side radar to be enabled and every
packaged build shipped with none at all.

Handlers here import their services inside the function body rather than at
module scope. That is not stylistic -- backend.main imports this module, and a
module-scope service import would close the loop back through it.
"""

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

router = APIRouter()

# NOTE: handlers below import services as `from ..services...`. One dot
# resolved to `backend` while these lived in main.py; here it would resolve
# to `backend.routers`. Because the imports are inside function bodies, a
# missed one fails only when the endpoint is CALLED -- not at import, and
# not in any route-table check.


@router.get("/api/model/scorecard")
async def model_scorecard(days: int = 14):
    """TBF Escalation Index -- live performance over the trailing `days`.

    Scored from the training archive itself: every collected row carries the
    probability AS SCORED AT THE TIME, and the labeller later fills in what
    actually happened, so a labelled row with a probability is a completed
    prediction. This is measured on the live population, not on the fixed
    historical holdout the trainer reports.
    """
    import asyncio as _a
    from pathlib import Path as _P
    from ..services.model_scorecard import scorecard, verdict
    from ..services.model_paths import runtime_data_dir, describe

    data_dir = runtime_data_dir()
    card = await _a.to_thread(
        scorecard, data_dir / "training_data.jsonl", data_dir, days)
    return {**card, "verdict": verdict(card), "models": describe()}


@router.get("/api/model/paths")
async def model_paths_status():
    """Where the models resolved from -- runtime copy, bundled seed, or missing.

    Exists because a model that fails to load used to be invisible: the failure
    logged as "running pure physics", which reads identically to "nothing has
    been trained yet". Make it inspectable.
    """
    from ..services.model_paths import describe
    out = describe()

    # Whether the LIVE tracker actually holds the models, not merely whether the
    # files resolve. These differ: sklearn missing from the bundle resolved the
    # paths fine and still loaded nothing. Absence of an error in a log is not
    # evidence of success -- that assumption is why this went unnoticed for
    # months -- so report the loaded state as a fact.
    try:
        from ..services.storm_tracking_service import get_storm_tracking_service
        svc = get_storm_tracking_service()
        if svc is None:
            out["tracker"] = {"running": False,
                              "note": "storm tracking is not running (nexrad_enabled?)"}
        else:
            out["tracker"] = {
                "running": True,
                "rotation_loaded": svc._rotation_model is not None,
                "severe_loaded": svc._severe_model is not None,
                "features": len(svc._rotation_model_features or []),
            }
    except Exception as e:  # noqa: BLE001
        out["tracker"] = {"running": False, "error": f"{type(e).__name__}: {e}"}

    # Detectors that have stopped producing anything. Empty means nothing has
    # failed -- a real answer, not an absence of data. These used to `return`
    # without a word, so a detector could be dead for months while the cell it
    # described simply reported no signature.
    try:
        from ..services.failure_log import snapshot as _degraded
        out["degraded"] = _degraded()
    except Exception as e:  # noqa: BLE001
        out["degraded"] = {"error": f"{type(e).__name__}: {e}"}
    return out


@router.get("/api/model/rotation/status")
async def rotation_model_status():
    """Current model, last cycle, and recent promote/reject history."""
    from ..services.model_training_service import get_model_training_service
    svc = get_model_training_service()
    if svc is None:
        return {"available": False}
    return {"available": True, **svc.status()}


@router.post("/api/model/rotation/retrain")
async def rotation_model_retrain(force: bool = True):
    """Run a cycle now.

    `force` skips the new-label floor and the severe-weather stand-down; it is
    the default here because a human asking for this has already decided.  The
    promotion gate still applies — this cannot push a worse model live.
    """
    from ..services.model_training_service import get_model_training_service
    svc = get_model_training_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="training service unavailable")
    return await svc.run_cycle(force=force)


@router.post("/api/model/rotation/rollback")
async def rotation_model_rollback():
    """Restore the previously promoted model and reload the tracker."""
    from ..services.model_training_service import get_model_training_service
    svc = get_model_training_service()
    if svc is None:
        raise HTTPException(status_code=503, detail="training service unavailable")
    result = svc.rollback()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "rollback failed"))
    await svc._reload_tracker()
    return result


# ── Training-data backfill (Model dashboard) ─────────────────────────────────

class BackfillStartRequest(BaseModel):
    days: list[str] = Field(..., description="Convective days, YYYY-MM-DD")
    sites: list[str] = Field(..., description="Radar sites, e.g. ['KILN','KIWX']")
    workers: int = Field(default=3, ge=1, le=12)
    full_day: bool = Field(default=False,
                           description="Replay the whole UTC day instead of the warned window")
    min_tor: int = Field(default=1, ge=0, le=100,
                         description="Skip a (site, day) with fewer tornado warnings than this. "
                                     "Positives come only from warned cells, so a site-day at 0 "
                                     "is all negatives -- 22% of the 2024 run was spent on those.")


@router.get("/api/model/backfill/candidates")
async def backfill_candidates(start: str, end: str, sites: str,
                              min_tor: int = 1):
    """Severe days worth replaying. `sites` is comma-separated."""
    from ..services.backfill_service import get_backfill_service
    site_list = [s.strip().upper() for s in sites.split(",") if s.strip()]
    if not site_list:
        raise HTTPException(status_code=400, detail="no sites given")
    try:
        # Hits IEM for the whole range, so keep it off the event loop.
        days = await asyncio.to_thread(
            get_backfill_service().find_days, start, end, site_list, min_tor)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"days": days, "sites": site_list}


@router.get("/api/model/backfill/status")
async def backfill_status():
    from ..services.backfill_service import get_backfill_service
    return get_backfill_service().status()


@router.post("/api/model/backfill/start")
async def backfill_start(req: BackfillStartRequest):
    from ..services.backfill_service import get_backfill_service
    result = get_backfill_service().start(
        days=req.days, sites=[s.upper() for s in req.sites],
        workers=req.workers, full_day=req.full_day, min_tor=req.min_tor)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "start failed"))
    return result


@router.post("/api/model/backfill/stop")
async def backfill_stop():
    from ..services.backfill_service import get_backfill_service
    result = get_backfill_service().stop()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "stop failed"))
    return result


@router.post("/api/model/backfill/merge")
async def backfill_merge():
    """Append the completed backfill rows onto the main training archive."""
    from ..services.backfill_service import get_backfill_service
    result = await asyncio.to_thread(get_backfill_service().merge_backfill)
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "merge failed"))
    return result


@router.get("/api/model/training/stats")
async def training_data_stats(which: str = "main"):
    """Label balance, month coverage and feature population for the archive."""
    from ..services.backfill_service import (
        BACKFILL_OUT, TRAINING_DATA, get_backfill_service,
    )
    path = BACKFILL_OUT if which == "backfill" else TRAINING_DATA
    # Streams a multi-hundred-MB file; cached inside the service.
    return await asyncio.to_thread(get_backfill_service().data_stats, path)


