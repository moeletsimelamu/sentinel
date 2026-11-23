"""Route-by-role permission matrix.

1. Every API route must be public, authenticated-only (explicit allowlists below) or declare
   ``RequirePermission``. A new route without a permission fails this test.
2. For every permission-protected route and every role, the API must answer 403 exactly
   when the role lacks the permission, and 401 when unauthenticated.

Allowed roles may legitimately get 404/422 here (dummy ids, empty bodies) — the assertion
is only that authorisation did not block them.
"""

from __future__ import annotations

import enum
import inspect
import uuid
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.permissions import DEFAULT_ROLE_PERMISSIONS, Permission, Role
from sentinel.main import app
from sentinel.modules.auth.dependencies import RequirePermission, get_current_principal
from sentinel.modules.auth.models import Role as RoleRow
from tests.conftest import AuthHeaders

pytestmark = pytest.mark.integration

PUBLIC = {
    ("GET", "/api/v1/health"),
    ("GET", "/api/v1/health/ready"),
    ("POST", "/api/v1/auth/login"),
    ("POST", "/api/v1/auth/refresh"),
    ("POST", "/api/v1/auth/logout"),
}
AUTHENTICATED_ONLY = {
    ("GET", "/api/v1/auth/me"),
    ("POST", "/api/v1/auth/password"),
}


@dataclass(frozen=True)
class Endpoint:
    """An APIRoute with its full path and any router-level dependencies applied."""

    path: str
    route: APIRoute
    router_dependencies: tuple[Any, ...]


def _flatten(routes: list[Any], prefix: str = "", deps: tuple[Any, ...] = ()) -> list[Endpoint]:
    # FastAPI >= 0.16x keeps included routers as nested route objects instead of copying
    # their routes; walk them (duck-typed, since the wrapper class is private).
    out: list[Endpoint] = []
    for r in routes:
        if isinstance(r, APIRoute):
            out.append(Endpoint(prefix + r.path, r, deps))
        elif hasattr(r, "original_router") and hasattr(r, "include_context"):
            ctx = r.include_context
            out.extend(
                _flatten(
                    r.original_router.routes,
                    prefix + ctx.prefix,
                    deps + tuple(d.dependency for d in ctx.dependencies),
                )
            )
    return out


def _walk(dependant: Dependant) -> list[Any]:
    calls = []
    for sub in dependant.dependencies:
        calls.append(sub.call)
        calls.extend(_walk(sub))
    return calls


def _calls(ep: Endpoint) -> list[Any]:
    return [*ep.router_dependencies, *_walk(ep.route.dependant)]


def _permissions(ep: Endpoint) -> set[Permission]:
    return {c.permission for c in _calls(ep) if isinstance(c, RequirePermission)}


def _routes() -> list[tuple[str, Endpoint]]:
    endpoints = _flatten(list(app.routes))
    return [(m, ep) for ep in endpoints for m in sorted(ep.route.methods)]


def _concrete_path(ep: Endpoint) -> str:
    path = ep.path
    for param in ep.route.dependant.path_params:
        annotation = param.field_info.annotation
        if annotation is uuid.UUID:
            value = str(uuid.uuid4())
        elif inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
            value = str(next(iter(annotation)).value)
        else:
            value = "1"
        path = path.replace("{" + param.name + "}", value)
    return path


PROTECTED = [(m, ep) for m, ep in _routes() if (m, ep.path) not in PUBLIC | AUTHENTICATED_ONLY]


def test_route_discovery_finds_the_api() -> None:
    found = {(m, ep.path) for m, ep in _routes()}
    assert found >= PUBLIC | AUTHENTICATED_ONLY
    assert ("GET", "/api/v1/users") in found and ("GET", "/api/v1/audit-log") in found


def test_every_route_is_classified() -> None:
    unclassified = []
    for method, ep in _routes():
        key = (method, ep.path)
        perms = _permissions(ep)
        if key in PUBLIC:
            assert not perms and get_current_principal not in _calls(ep), key
        elif key in AUTHENTICATED_ONLY:
            assert get_current_principal in _calls(ep), key
        elif len(perms) != 1:
            unclassified.append(key)
    assert not unclassified, f"Routes without exactly one RequirePermission: {unclassified}"


@pytest.mark.parametrize(("method", "ep"), PROTECTED, ids=lambda x: getattr(x, "path", x))
def test_unauthenticated_requests_are_rejected(
    client: TestClient, method: str, ep: Endpoint
) -> None:
    resp = client.request(method, _concrete_path(ep), json={})
    assert resp.status_code == 401


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize(("method", "ep"), PROTECTED, ids=lambda x: getattr(x, "path", x))
def test_role_matrix(
    client: TestClient, auth_headers: AuthHeaders, method: str, ep: Endpoint, role: Role
) -> None:
    (required,) = _permissions(ep)
    resp = client.request(method, _concrete_path(ep), json={}, headers=auth_headers(role))
    if required in DEFAULT_ROLE_PERMISSIONS[role]:
        assert resp.status_code not in (401, 403), resp.text
    else:
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["details"]["required_permission"] == required.value


def test_database_role_permissions_match_code(db: Session) -> None:
    roles = db.scalars(select(RoleRow)).all()
    in_db = {Role(r.name): {p.permission for p in r.permissions} for r in roles}
    in_code = {role: {p.value for p in perms} for role, perms in DEFAULT_ROLE_PERMISSIONS.items()}
    assert in_db == in_code


def test_separation_of_duties() -> None:
    admin = DEFAULT_ROLE_PERMISSIONS[Role.ADMIN]
    analyst = DEFAULT_ROLE_PERMISSIONS[Role.ANALYST]
    auditor = DEFAULT_ROLE_PERMISSIONS[Role.AUDITOR]
    # Admins configure detection but never adjudicate its output.
    assert Permission.ALERTS_WORK not in admin and Permission.CASES_WORK not in admin
    # Analysts cannot tune the detection that feeds their own queue.
    assert (
        not {Permission.RULES_CONFIGURE, Permission.SCORING_CONFIGURE, Permission.MODELS_ACTIVATE}
        & analyst
    )
    # Auditors are read-only.
    assert all(p.value.endswith(":read") for p in auditor)
