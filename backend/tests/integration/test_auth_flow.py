from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.core.config import get_settings
from sentinel.core.permissions import DEFAULT_ROLE_PERMISSIONS, Permission, Role
from sentinel.core.security import create_access_token
from sentinel.modules.audit.models import AuditLog
from sentinel.modules.auth.models import RefreshToken
from tests.conftest import TEST_PASSWORD, MakeUser, bearer, login

pytestmark = pytest.mark.integration

LOGIN = "/api/v1/auth/login"
REFRESH = "/api/v1/auth/refresh"
LOGOUT = "/api/v1/auth/logout"
ME = "/api/v1/auth/me"
COOKIE = get_settings().refresh_cookie_name


def _actions(db: Session) -> list[str]:
    return list(db.scalars(select(AuditLog.action).order_by(AuditLog.id)))


def test_login_returns_access_token_and_refresh_cookie(
    client: TestClient, make_user: MakeUser
) -> None:
    user = make_user(Role.ANALYST)
    resp = client.post(LOGIN, json={"email": user.email.upper(), "password": TEST_PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_type"] == "bearer"
    assert body["user"]["role"] == "analyst"
    assert set(body["user"]["permissions"]) == set(DEFAULT_ROLE_PERMISSIONS[Role.ANALYST])

    set_cookie = resp.headers["set-cookie"].lower()
    assert COOKIE in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=strict" in set_cookie
    assert "path=/api/v1/auth" in set_cookie


def test_me_requires_valid_token(client: TestClient, make_user: MakeUser) -> None:
    assert client.get(ME).status_code == 401
    assert client.get(ME, headers=bearer("not-a-jwt")).status_code == 401
    user = make_user(Role.AUDITOR)
    resp = client.get(ME, headers=bearer(login(client, user.email)))
    assert resp.status_code == 200
    assert resp.json()["email"] == user.email


def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    user = make_user()
    bad_pw = client.post(LOGIN, json={"email": user.email, "password": "wrong-password-1"})
    unknown = client.post(LOGIN, json={"email": "nobody@test.local", "password": "whatever-123"})
    assert bad_pw.status_code == unknown.status_code == 401
    assert bad_pw.json()["error"]["message"] == unknown.json()["error"]["message"]
    assert _actions(db).count("auth.login_failed") == 2


def test_account_locks_after_repeated_failures(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    user = make_user()
    max_failed = get_settings().max_failed_logins
    for _ in range(max_failed):
        r = client.post(LOGIN, json={"email": user.email, "password": "wrong-password-1"})
        assert r.status_code == 401
    # Correct password is now refused while the lock is in force.
    r = client.post(LOGIN, json={"email": user.email, "password": TEST_PASSWORD})
    assert r.status_code == 401
    db.refresh(user)
    assert user.locked_until is not None and user.locked_until > datetime.now(UTC)
    actions = _actions(db)
    assert "auth.account_locked" in actions and "auth.login_blocked" in actions

    # After the lock expires the correct password works and the counter resets.
    user.locked_until = datetime.now(UTC) - timedelta(seconds=1)
    db.flush()
    assert (
        client.post(LOGIN, json={"email": user.email, "password": TEST_PASSWORD}).status_code == 200
    )


def test_inactive_user_cannot_log_in(client: TestClient, make_user: MakeUser) -> None:
    user = make_user(is_active=False)
    r = client.post(LOGIN, json={"email": user.email, "password": TEST_PASSWORD})
    assert r.status_code == 401


def test_deactivation_takes_effect_on_existing_access_token(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    user = make_user()
    token = login(client, user.email)
    user.is_active = False
    db.flush()
    assert client.get(ME, headers=bearer(token)).status_code == 401


def test_expired_access_token_rejected(client: TestClient, make_user: MakeUser) -> None:
    user = make_user()
    past = datetime.now(UTC) - timedelta(hours=2)
    token, _ = create_access_token(get_settings(), user.id, "analyst", now=past)
    assert client.get(ME, headers=bearer(token)).status_code == 401


def test_token_for_nonexistent_user_rejected(client: TestClient) -> None:
    token, _ = create_access_token(get_settings(), uuid.uuid4(), "admin")
    assert client.get(ME, headers=bearer(token)).status_code == 401


def test_refresh_rotates_token(client: TestClient, make_user: MakeUser, db: Session) -> None:
    user = make_user()
    login(client, user.email)
    first = client.cookies.get(COOKIE)
    assert first

    r = client.post(REFRESH)
    assert r.status_code == 200
    second = client.cookies.get(COOKIE)
    assert second and second != first
    assert client.get(ME, headers=bearer(r.json()["access_token"])).status_code == 200

    tokens = db.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id)).all()
    assert len(tokens) == 2
    old = next(t for t in tokens if t.replaced_by_id is not None)
    assert old.revoked_at is not None


def test_refresh_token_reuse_revokes_whole_family(
    client: TestClient, make_user: MakeUser, db: Session
) -> None:
    user = make_user()
    login(client, user.email)
    stolen = client.cookies.get(COOKIE)
    assert client.post(REFRESH).status_code == 200  # legitimate rotation
    current = client.cookies.get(COOKIE)

    client.cookies.set(COOKIE, stolen, path="/api/v1/auth")  # attacker replays old token
    assert client.post(REFRESH).status_code == 401

    client.cookies.set(COOKIE, current, path="/api/v1/auth")  # legitimate user is logged out too
    assert client.post(REFRESH).status_code == 401
    assert "auth.refresh_reuse_detected" in _actions(db)
    tokens = db.scalars(select(RefreshToken).where(RefreshToken.user_id == user.id)).all()
    assert all(t.revoked_at is not None for t in tokens)


def test_refresh_without_cookie_or_with_garbage(client: TestClient) -> None:
    assert client.post(REFRESH).status_code == 401
    client.cookies.set(COOKIE, "garbage", path="/api/v1/auth")
    assert client.post(REFRESH).status_code == 401


def test_logout_revokes_refresh_token(client: TestClient, make_user: MakeUser, db: Session) -> None:
    user = make_user()
    login(client, user.email)
    token = client.cookies.get(COOKIE)
    r = client.post(LOGOUT)
    assert r.status_code == 204
    client.cookies.set(COOKIE, token, path="/api/v1/auth")
    assert client.post(REFRESH).status_code == 401
    assert "auth.logout" in _actions(db)


def test_logout_without_session_is_noop(client: TestClient) -> None:
    assert client.post(LOGOUT).status_code == 204


def test_change_password(client: TestClient, make_user: MakeUser) -> None:
    user = make_user()
    headers = bearer(login(client, user.email))
    old_refresh = client.cookies.get(COOKIE)

    weak = client.post(
        "/api/v1/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": "short"},
        headers=headers,
    )
    assert weak.status_code == 422

    wrong = client.post(
        "/api/v1/auth/password",
        json={"current_password": "not-my-password", "new_password": "A-much-better-pass-42"},
        headers=headers,
    )
    assert wrong.status_code == 401

    ok = client.post(
        "/api/v1/auth/password",
        json={"current_password": TEST_PASSWORD, "new_password": "A-much-better-pass-42"},
        headers=headers,
    )
    assert ok.status_code == 204
    # Existing refresh sessions are ended; new password works, old one does not.
    client.cookies.set(COOKIE, old_refresh, path="/api/v1/auth")
    assert client.post(REFRESH).status_code == 401
    assert (
        client.post(LOGIN, json={"email": user.email, "password": TEST_PASSWORD}).status_code == 401
    )
    login(client, user.email, "A-much-better-pass-42")


def test_grant_is_reflected_in_permissions(client: TestClient, make_user: MakeUser) -> None:
    user = make_user(Role.ANALYST, grants=(Permission.ALERTS_ASSIGN_OTHERS,))
    me = client.get(ME, headers=bearer(login(client, user.email))).json()
    assert Permission.ALERTS_ASSIGN_OTHERS.value in me["permissions"]


def test_login_rate_limited(client: TestClient, make_user: MakeUser) -> None:
    from sentinel.core.rate_limit import limiter

    limiter.enabled = True
    user = make_user()
    statuses = [
        client.post(LOGIN, json={"email": user.email, "password": "wrong-password-1"}).status_code
        for _ in range(11)
    ]
    assert statuses[-1] == 429
    assert 429 not in statuses[:10]
