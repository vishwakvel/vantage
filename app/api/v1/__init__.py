"""API v1 router aggregating all domain sub-routers.

Import ``router`` from here and include it in the FastAPI application:

    from app.api.v1 import router as v1_router
    app.include_router(v1_router, prefix="/api/v1")
"""

from fastapi import APIRouter

from app.api.v1.auth import router as auth_router

router = APIRouter()
router.include_router(auth_router)

__all__ = ["router"]
