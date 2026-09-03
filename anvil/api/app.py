"""The FastAPI application.

Two things about this file are deliberate.

**The console is served from here.** A single process, one port, no build step,
no CORS. A reviewer runs ``uvicorn anvil.main_api:app`` and has a working
interface. Splitting the front end into its own Node toolchain would look more
modern in the repository and would double the number of ways the demo can fail
in front of a panel, which is the wrong trade for something whose whole purpose
is to be seen working.

**Errors carry their taxonomy.** :mod:`anvil.core.errors` already knows each
failure's HTTP status and whether it is retryable. Translating that once here
means an endpoint never has to decide what a ``BudgetExhausted`` is worth, and a
client can branch on a stable ``code`` rather than parsing prose.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles

from anvil.api.routers import evidence, insight, operations, system
from anvil.api.security import (
    _basic,
    check_rate_limit,
    console_auth_enabled,
    require_console_auth,
    security_headers,
)
from anvil.api.state import initialise, reset
from anvil.core.config import get_settings
from anvil.core.errors import AnvilError
from anvil.core.logging import configure_logging, get_logger

_log = get_logger(__name__)
_STATIC = Path(__file__).parent / "static"

DESCRIPTION = """
An agentic control plane that recovers failed recurring-payment debits.

**The model decides. The ledger disposes.** Nothing the model says can move money:
every action must present a valid authorisation and pass a deterministic policy
evaluation before the executor will touch it.

This API needs no database and no credentials. It drives a seeded simulator in
process, so the whole system can be exercised from a clean checkout. The
endpoints under `insight` exist so the claims in the architecture document can be
falsified rather than taken on trust: schedule a retry and see the hours it
rejected, evaluate arbitrary facts against the live policy bundle, or try to make
a ledger posting fail to balance.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    state = await initialise()
    _log.info(
        "anvil_api_ready",
        mode=settings.mode.value,
        seed=state.seed,
        at_risk_cases=len(state.cases),
        pending_approvals=len(state.live),
        console_auth=console_auth_enabled(),
    )
    if not console_auth_enabled():
        _log.warning(
            "console_is_unauthenticated",
            detail=(
                "No ANVIL_CONSOLE_PASSWORD is set, so every endpoint is open. That is "
                "correct on localhost and wrong on a public host."
            ),
        )
    yield
    reset()


def create_app() -> FastAPI:
    app = FastAPI(
        title="Anvil",
        summary="Revenue recovery control plane for failed recurring payments.",
        description=DESCRIPTION,
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url=None,
    )

    app.middleware("http")(security_headers)

    def _guard(
        request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)
    ) -> None:
        """One dependency for both gates, applied to every route.

        Attached at the application level rather than per-router so that a new
        router cannot be added without it -- the failure mode of per-router
        security is an endpoint somebody forgot.
        """
        require_console_auth(request, credentials)
        check_rate_limit(request)

    app.router.dependencies.append(Depends(_guard))

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> PlainTextResponse:
        # A demonstration instance has no business in a search index.
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    @app.exception_handler(AnvilError)
    async def _anvil_error(request: Request, exc: AnvilError) -> JSONResponse:
        _log.warning("anvil_error", code=exc.code, path=request.url.path, message=exc.message)
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_dict()})

    app.include_router(system.router)
    app.include_router(insight.router)
    app.include_router(operations.router)
    app.include_router(evidence.router)

    if _STATIC.exists():
        app.mount("/static", StaticFiles(directory=_STATIC), name="static")

        @app.get("/", include_in_schema=False)
        async def console() -> Any:
            return FileResponse(_STATIC / "index.html")

    return app


app = create_app()
