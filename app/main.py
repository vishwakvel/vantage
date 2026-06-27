"""FastAPI application factory.

Usage:
    from app.main import create_app, app

``create_app()`` returns a fresh FastAPI instance each call — useful for test
isolation.  ``app`` is the module-level instance used by uvicorn.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Construct and return a configured FastAPI application.

    Lifespan stubs (startup/shutdown) are defined here; populated in later
    plans as services come online.

    The v1 router is imported with a guard so that the empty
    ``app/api/v1/__init__.py`` in this plan does not break the factory.
    Plan 01-05 populates the router and removes the guard.
    """

    @asynccontextmanager
    async def lifespan(application: FastAPI):  # type: ignore[type-arg]
        # Startup — populated in plan 01-05 (DB / Redis connections)
        yield
        # Shutdown — populated in plan 01-05

    application = FastAPI(
        title="Vantage",
        version="0.1.0",
        description=(
            "Multi-agent LangGraph platform that produces "
            "institutional-quality investment research memos."
        ),
        lifespan=lifespan,
    )

    @application.get("/health", tags=["health"])
    async def health() -> dict:  # type: ignore[type-arg]
        """Liveness probe — returns 200 OK when the app is running."""
        return {"status": "ok"}

    # Router is wired in plan 01-05; guard prevents import errors on the
    # empty __init__.py that exists at this stage of the build.
    try:
        from app.api.v1 import router as v1_router  # noqa: PLC0415

        application.include_router(v1_router, prefix="/api/v1")
    except ImportError:
        pass  # Router not yet wired — populated in plan 01-05

    return application


# Module-level instance for uvicorn / ASGI servers.
app = create_app()
