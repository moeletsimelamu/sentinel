from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.permissions import Permission, Role
from sentinel.modules.auth.models import RefreshToken
from tests.conftest import AuthHeaders, MakeUser, bearer, login

pytestmark = pytest.mark.integration

USERS = "/api/v1/users"


def _create(client: TestClient, headers: dict[str, str], **overrides: Any) -> Any:
    body = {
        "email": "jo.analyst@test.local",
        "full_name": "Jo Analyst",
        "role": "analyst",
        "password": "Plenty-long-pass-77",
    } | overrides
    return client.post(USERS, json=body, headers=headers)


def test_create_and_list_users(client: TestClient, auth_headers: AuthHeaders) -> None:
    admin = auth_headers(Role.ADMIN)
    created = _create(client, admin, email="  Jo.Analyst@Test.Local ")
    assert created.status_code == 201
    assert created.json()["email"] == "jo.analyst@test.local"
    assert created.json()["version"] == 1

    listing = client.get(f"{USERS}?role=analyst", headers=admin).json()
    assert "jo.analyst@test.local" in [u["email"] for u in listing["items"]]
    assert all(u["role"] == "analyst" for u in listing["items"])


def test_duplicate_email_conflict(client: TestClient, auth_headers: AuthHeaders) -> None:
    admin = auth_headers(Role.ADMIN)
    assert _create(client, admin).status_code == 201
    dup = _create(client, admin)
    assert dup.status_code == 409
    assert dup.json()["error"]["code"] == "email_taken"


def test_password_policy_enforced(client: TestClient, auth_headers: AuthHeaders) -> None:
    admin = auth_headers(Role.ADMIN)
    assert _create(client, admin, password="short").status_code == 422
    resp = _create(client, admin, password="jo.analyst-is-my-password")
    assert resp.status_code == 422


def test_invalid_email_rejected(client: TestClient, auth_headers: AuthHeaders) -> None:
    resp = _create(client, auth_headers(Role.ADMIN), email="not-an-email")
    assert resp.status_code == 422
    # Validation errors never echo submitted values (could be secrets).
    assert "Plenty-long-pass-77" not in resp.text


def test_stale_version_is_rejected(client: TestClient, auth_headers: AuthHeaders) -> None:
    admin = auth_headers(Role.ADMIN)
    user = _create(client, admin).json()
    url = f"{USERS}/{user['id']}"

    first = client.patch(url, json={"version": 1, "full_name": "Jo A."}, headers=admin)
    assert first.status_code == 200 and first.json()["version"] == 2

    # A second editor still holding version 1 must not silently overwrite.
    stale = client.patch(url, json={"version": 1, "full_name": "Jo B."}, headers=admin)
    assert stale.status_code == 409
    assert stale.json()["error"]["details"]["current_version"] == 2
    assert client.get(url, headers=admin).json()["full_name"] == "Jo A."


def test_admin_cannot_demote_or_deactivate_self(client: TestClient, make_user: MakeUser) -> None:
    me = make_user(Role.ADMIN)
    headers = bearer(login(client, me.email))
    url = f"{USERS}/{me.id}"
    assert (
        client.patch(url, json={"version": 1, "role": "auditor"}, headers=headers).status_code
        == 422
    )
    assert (
        client.patch(url, json={"version": 1, "is_active": False}, headers=headers).status_code
        == 422
    )


def test_deactivation_revokes_sessions(
    client: TestClient, auth_headers: AuthHeaders, make_user: MakeUser, db: Session
) -> None:
    target = make_user(Role.ANALYST)
    login(client, target.email)
    admin = auth_headers(Role.ADMIN)
    resp = client.patch(
        f"{USERS}/{target.id}", json={"version": 1, "is_active": False}, headers=admin
    )
    assert resp.status_code == 200 and resp.json()["is_active"] is False
    tokens = db.scalars(select(RefreshToken).where(RefreshToken.user_id == target.id)).all()
    assert tokens and all(t.revoked_at is not None for t in tokens)


def test_lead_analyst_grant_rules(
    client: TestClient, auth_headers: AuthHeaders, make_user: MakeUser
) -> None:
    admin = auth_headers(Role.ADMIN)
    analyst = make_user(Role.ANALYST)
    auditor = make_user(Role.AUDITOR)
    grant = {"version": 1, "permission": Permission.ALERTS_ASSIGN_OTHERS.value}

    ok = client.post(f"{USERS}/{analyst.id}/grants", json=grant, headers=admin)
    assert ok.status_code == 200
    assert ok.json()["grants"] == ["alerts:assign_others"]
    assert ok.json()["version"] == 2

    # Auditors stay read-only; non-grantable permissions are refused.
    assert client.post(f"{USERS}/{auditor.id}/grants", json=grant, headers=admin).status_code == 422
    bad = {"version": 2, "permission": Permission.USERS_MANAGE.value}
    assert client.post(f"{USERS}/{analyst.id}/grants", json=bad, headers=admin).status_code == 422

    revoked = client.delete(
        f"{USERS}/{analyst.id}/grants/alerts:assign_others?version=2", headers=admin
    )
    assert revoked.status_code == 200 and revoked.json()["grants"] == []


def test_role_change_drops_incompatible_grants(
    client: TestClient, auth_headers: AuthHeaders, make_user: MakeUser
) -> None:
    admin = auth_headers(Role.ADMIN)
    lead = make_user(Role.ANALYST, grants=(Permission.ALERTS_ASSIGN_OTHERS,))
    resp = client.patch(f"{USERS}/{lead.id}", json={"version": 1, "role": "auditor"}, headers=admin)
    assert resp.status_code == 200
    assert resp.json()["role"] == "auditor" and resp.json()["grants"] == []


def test_unknown_user_404(client: TestClient, auth_headers: AuthHeaders) -> None:
    resp = client.get(
        f"{USERS}/00000000-0000-7000-8000-000000000000", headers=auth_headers(Role.ADMIN)
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
