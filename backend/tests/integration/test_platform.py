"""Health, error envelope, request ids, security headers, CLI seeding."""

from __future__ import annotations

import pytest
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from sentinel.modules.auth.models import User
from tests.conftest import alembic_config

pytestmark = pytest.mark.integration


def test_liveness_and_readiness(client: TestClient) -> None:
    assert client.get("/api/v1/health").json()["status"] == "ok"
    ready = client.get("/api/v1/health/ready")
    assert ready.status_code == 200
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    assert ready.json()["migration"] == head


def test_error_envelope_and_request_id(client: TestClient) -> None:
    resp = client.get("/api/v1/does-not-exist")
    assert resp.status_code == 404
    err = resp.json()["error"]
    assert err["code"] == "not_found"
    assert err["request_id"] == resp.headers["x-request-id"]


def test_incoming_request_id_is_propagated_if_safe(client: TestClient) -> None:
    ok = client.get("/api/v1/health", headers={"X-Request-ID": "trace-abc-12345"})
    assert ok.headers["x-request-id"] == "trace-abc-12345"
    unsafe = client.get("/api/v1/health", headers={"X-Request-ID": "bad id\nwith newline"})
    assert unsafe.headers["x-request-id"] != "bad id\nwith newline"


def test_security_headers_present(client: TestClient) -> None:
    resp = client.get("/api/v1/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["cache-control"] == "no-store"


def test_validation_error_shape(client: TestClient) -> None:
    resp = client.post("/api/v1/auth/login", json={"email": "x"})
    assert resp.status_code == 422
    err = resp.json()["error"]
    assert err["code"] == "validation_failed"
    assert {tuple(d["loc"]) for d in err["details"]} >= {("body", "password")}


def test_seed_demo_users_is_idempotent(db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel import cli

    class _Factory:
        def __call__(self) -> Session:
            return db

    # Reuse the test transaction; closing it is a no-op for the wrapper.
    monkeypatch.setattr(cli, "get_session_factory", lambda: _Factory())
    monkeypatch.setattr(db, "close", lambda: None)
    monkeypatch.setenv("SENTINEL_DEMO_PASSWORD", "Demo-Password-2026!")

    assert cli.main(["seed-demo-users"]) == 0
    assert cli.main(["seed-demo-users"]) == 0
    emails = set(db.scalars(select(User.email).where(User.email.like("%@sentinel.local"))))
    assert emails == {e for e, *_ in cli.DEMO_USERS}


def test_seed_requires_password(monkeypatch: pytest.MonkeyPatch) -> None:
    from sentinel import cli

    monkeypatch.delenv("SENTINEL_DEMO_PASSWORD", raising=False)
    assert cli.main(["seed-demo-users"]) == 2
