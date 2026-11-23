from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.orm import Session

from sentinel.core.config import Settings, get_settings
from sentinel.core.db import get_db
from sentinel.core.permissions import Permission, Role
from sentinel.core.rate_limit import limiter, login_limit
from sentinel.modules.audit.context import RequestContext, get_request_context
from sentinel.modules.auth.dependencies import RequirePermission, get_current_principal
from sentinel.modules.auth.schemas import (
    ChangePasswordRequest,
    GrantRequest,
    LoginRequest,
    MeResponse,
    Principal,
    TokenResponse,
    UserCreate,
    UserOut,
    UserPage,
    UserUpdate,
)
from sentinel.modules.auth.service import AuthService, IssuedTokens
from sentinel.modules.auth.users_service import UserAdminService

auth_router = APIRouter(prefix="/auth", tags=["auth"])
users_router = APIRouter(prefix="/users", tags=["users"])

REFRESH_COOKIE_PATH = "/api/v1/auth"


def _me(principal: Principal) -> MeResponse:
    return MeResponse(
        id=principal.id,
        email=principal.email,
        full_name=principal.full_name,
        role=principal.role,
        permissions=sorted(principal.permissions),
    )


def _token_response(response: Response, settings: Settings, tokens: IssuedTokens) -> TokenResponse:
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=tokens.refresh_token,
        max_age=settings.refresh_token_ttl_days * 86400,
        path=REFRESH_COOKIE_PATH,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
    )
    return TokenResponse(
        access_token=tokens.access_token,
        expires_at=tokens.access_expires_at,
        user=_me(tokens.principal),
    )


# ---------------------------------------------------------------------- auth


@auth_router.post("/login", response_model=TokenResponse)
@limiter.limit(login_limit)
def login(
    request: Request,
    response: Response,
    body: LoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> TokenResponse:
    tokens = AuthService(db, settings).login(body.email, body.password, ctx)
    return _token_response(response, settings, tokens)


@auth_router.post("/refresh", response_model=TokenResponse)
@limiter.limit(login_limit)
def refresh(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> TokenResponse:
    raw = request.cookies.get(settings.refresh_cookie_name)
    tokens = AuthService(db, settings).refresh(raw, ctx)
    return _token_response(response, settings, tokens)


@auth_router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request,
    response: Response,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> Response:
    AuthService(db, settings).logout(request.cookies.get(settings.refresh_cookie_name), ctx)
    response.status_code = status.HTTP_204_NO_CONTENT
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=REFRESH_COOKIE_PATH,
        secure=settings.cookie_secure,
        httponly=True,
        samesite="strict",
    )
    return response


@auth_router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(get_current_principal)) -> MeResponse:
    return _me(principal)


@auth_router.post("/password", status_code=status.HTTP_204_NO_CONTENT)
def change_password(
    body: ChangePasswordRequest,
    principal: Principal = Depends(get_current_principal),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> Response:
    AuthService(db, settings).change_password(
        principal, body.current_password, body.new_password, ctx
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --------------------------------------------------------------------- users

_manage = RequirePermission(Permission.USERS_MANAGE)


@users_router.get("", response_model=UserPage, dependencies=[Depends(_manage)])
def list_users(
    role: Role | None = None,
    is_active: bool | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UserPage:
    items, total = UserAdminService(db, settings).list(
        role=role, is_active=is_active, limit=limit, offset=offset
    )
    return UserPage(items=items, total=total, limit=limit, offset=offset)


@users_router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    by: Principal = Depends(_manage),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> UserOut:
    return UserAdminService(db, settings).create(body, by, ctx)


@users_router.get("/{user_id}", response_model=UserOut, dependencies=[Depends(_manage)])
def get_user(
    user_id: uuid.UUID,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UserOut:
    return UserAdminService(db, settings).get(user_id)


@users_router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID,
    body: UserUpdate,
    by: Principal = Depends(_manage),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> UserOut:
    return UserAdminService(db, settings).update(user_id, body, by, ctx)


@users_router.post("/{user_id}/grants", response_model=UserOut)
def grant_permission(
    user_id: uuid.UUID,
    body: GrantRequest,
    by: Principal = Depends(_manage),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> UserOut:
    return UserAdminService(db, settings).grant(user_id, body.permission, body.version, by, ctx)


@users_router.delete("/{user_id}/grants/{permission}", response_model=UserOut)
def revoke_permission(
    user_id: uuid.UUID,
    permission: Permission,
    version: int = Query(ge=1),
    by: Principal = Depends(_manage),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    ctx: RequestContext = Depends(get_request_context),
) -> UserOut:
    return UserAdminService(db, settings).revoke(user_id, permission, version, by, ctx)
