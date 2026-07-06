"""API v1 router aggregating all domain sub-routers.

Import ``router`` from here and include it in the FastAPI application:

    from app.api.v1 import router as v1_router
    app.include_router(v1_router, prefix="/api/v1")
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.ingest import router as ingest_router
from app.api.v1.research import router as research_router
from app.api.v1.ws import router as ws_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(ingest_router)
router.include_router(research_router)
router.include_router(ws_router)

__all__ = ["router"]
