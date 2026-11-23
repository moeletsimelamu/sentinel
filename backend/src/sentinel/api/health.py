from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from sentinel import __version__
from sentinel.core.db import get_db

router = APIRouter(prefix="/health", tags=["health"])


@router.get("")
def liveness() -> dict[str, str]:
    """Process is up. Does not touch the database."""
    return {"status": "ok", "version": __version__}


@router.get("/ready")
def readiness(db: Session = Depends(get_db)) -> JSONResponse:
    """Database reachable and migrations applied."""
    try:
        db.execute(text("SELECT 1"))
        revision = db.execute(text("SELECT version_num FROM alembic_version")).scalar()
    except SQLAlchemyError:
        return JSONResponse(status_code=503, content={"status": "unavailable", "database": "down"})
    return JSONResponse(content={"status": "ready", "database": "up", "migration": revision})
