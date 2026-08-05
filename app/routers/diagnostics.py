"""Operational visibility into the search backend.

Kept out of /health on purpose. /health is unauthenticated and polled by the
container healthcheck every few seconds; these endpoints issue real upstream
queries, so wiring them into a liveness probe would manufacture the rate
limiting they exist to detect — and would leak backend detail to anyone who
can reach the port.
"""

import time

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.auth import verify_api_key
from app.config import settings
from app.models.schemas import EngineStatus, EngineStatusResponse
from app.rate_limit import limiter

logger = structlog.get_logger(__name__)
router = APIRouter()

# Broad enough that any healthy general-purpose engine has something to say,
# specific enough that a nonsense-filtering engine still matches. A query
# nobody has results for would make every engine look silent.
DEFAULT_PROBE_QUERY = "kubernetes ingress controller"


@router.get("/engines", response_model=EngineStatusResponse)
@limiter.limit(settings.rate_limit.search_rate)
async def engine_status(
    request: Request,
    q: str = Query(
        default=DEFAULT_PROBE_QUERY,
        description="Query used to exercise the engines",
    ),
    probe: str | None = Query(
        default=None,
        description=(
            "Comma-separated engine names to test individually, e.g. "
            "'mojeek,yahoo,qwant'. Also works for engines disabled in the "
            "SearXNG config, so candidates can be evaluated before enabling "
            "them. Omit to report on the currently configured set instead."
        ),
    ),
    api_key: str | None = Depends(verify_api_key),
) -> EngineStatusResponse:
    start = time.perf_counter()
    backend = request.app.state.search_backend

    names = [n.strip() for n in probe.split(",") if n.strip()] if probe else []
    if names and len(names) > 25:
        raise HTTPException(422, "probe accepts at most 25 engines per call")

    try:
        if names:
            health = await backend.probe_engines(q, names)
            mode = "probe"
        else:
            health = await backend.engine_status(q)
            mode = "configured"
    except NotImplementedError:
        raise HTTPException(
            501, "The active search backend does not expose engine diagnostics"
        )
    except Exception as e:
        logger.error("engine_status_failed", error=str(e))
        raise HTTPException(503, f"Could not reach the search backend: {e}")

    def to_model(h) -> EngineStatus:
        return EngineStatus(name=h.name, results=h.results, ok=h.ok, reason=h.reason)

    delivering = [to_model(h) for h in health if h.ok and h.results > 0]
    failing = [to_model(h) for h in health if not h.ok]
    # Reachable but empty-handed for this query. Worth separating from
    # `failing`: nothing is broken, so it warrants no action — unless an
    # engine sits here across several different queries, which is how a
    # quietly degrading engine looks before it starts erroring outright.
    silent = [to_model(h) for h in health if h.ok and h.results == 0]

    elapsed = time.perf_counter() - start
    logger.info(
        "engine_status",
        mode=mode,
        delivering=len(delivering),
        failing=len(failing),
        silent=len(silent),
    )
    return EngineStatusResponse(
        query=q,
        mode=mode,
        delivering=delivering,
        failing=failing,
        silent=silent,
        response_time=round(elapsed, 3),
    )
