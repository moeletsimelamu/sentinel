"""Assembles all module routers under ``/api/v1``."""

from __future__ import annotations

from fastapi import APIRouter

from sentinel.api import health
from sentinel.modules.audit.router import router as audit_router
from sentinel.modules.auth.router import auth_router, users_router
from sentinel.modules.ingestion.router import imports_router, ingest_router
from sentinel.modules.transactions.router import router as transactions_router

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(health.router)
api_router.include_router(auth_router)
api_router.include_router(users_router)
api_router.include_router(audit_router)
api_router.include_router(imports_router)
api_router.include_router(ingest_router)
api_router.include_router(transactions_router)
