"""Shared fixtures.

Integration tests run against a real PostgreSQL database (``sentinel_test``) because the
behaviour under test — check constraints, triggers, ``FOR UPDATE`` — is Postgres-specific.
Each test runs inside a transaction that is rolled back afterwards; service-level
``commit()`` calls only release a SAVEPOINT (``join_transaction_mode="create_savepoint"``).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

TEST_DATABASE_URL = os.environ.get(
    "SENTINEL_TEST_DATABASE_URL",
    "postgresql+psycopg://sentinel:sentinel@localhost:5438/sentinel_test",
)
if not TEST_DATABASE_URL.rsplit("/", 1)[-1].endswith("_test"):
    raise RuntimeError("Refusing to run tests: database name must end with '_test'")

# Must be set before any sentinel module reads settings.
os.environ["SENTINEL_ENV"] = "test"
os.environ["SENTINEL_DATABASE_URL"] = TEST_DATABASE_URL
os.environ["SENTINEL_COOKIE_SECURE"] = "false"
os.environ["SENTINEL_LOG_JSON"] = "false"
os.environ["SENTINEL_LOG_LEVEL"] = "WARNING"

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from pwdlib import PasswordHash  # noqa: E402
from pwdlib.hashers.argon2 import Argon2Hasher  # noqa: E402
from sqlalchemy import create_engine, text  # noqa: E402
from sqlalchemy.engine import Engine, make_url  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from sentinel.core.db import get_db  # noqa: E402
from sentinel.core.permissions import Permission, Role  # noqa: E402
from sentinel.core.rate_limit import limiter  # noqa: E402
from sentinel.core.security import hash_password  # noqa: E402
from sentinel.modules.auth.models import User, UserPermissionGrant  # noqa: E402
from sentinel.modules.auth.repository import get_role_by_name  # noqa: E402

BACKEND_DIR = Path(__file__).resolve().parents[1]
TEST_PASSWORD = "Correct-Horse-Battery-9"
# Fixture users get a low-cost Argon2id hash: verification reads the parameters from the
# hash itself, so logins stay fast while production hashing (hash_password) is unchanged.
_TEST_PASSWORD_HASH = PasswordHash((Argon2Hasher(time_cost=1, memory_cost=8192),)).hash(
    TEST_PASSWORD
)
assert hash_password  # production hasher is still exercised by API-created users


def _ensure_database(url: str) -> None:
    parsed = make_url(url)
    admin = create_engine(parsed.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": parsed.database}
        ).scalar()
        if not exists:
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
    admin.dispose()


@pytest.fixture(scope="session")
def engine() -> Iterator[Engine]:
    _ensure_database(TEST_DATABASE_URL)
    eng = create_engine(TEST_DATABASE_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", TEST_DATABASE_URL.replace("%", "%%"))
    command.upgrade(cfg, "head")
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine: Engine) -> Iterator[Session]:
    conn = engine.connect()
    outer = conn.begin()
    session = Session(bind=conn, join_transaction_mode="create_savepoint", expire_on_commit=False)
    try:
        yield session
    finally:
        session.close()
        outer.rollback()
        conn.close()


@pytest.fixture(autouse=True)
def _rate_limiter() -> Iterator[None]:
    limiter.reset()
    limiter.enabled = False
    yield
    limiter.enabled = False
    limiter.reset()


@pytest.fixture
def client(db: Session) -> Iterator[TestClient]:
    from sentinel.main import app

    def _override() -> Iterator[Session]:
        yield db

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


MakeUser = Callable[..., User]


@pytest.fixture
def make_user(db: Session) -> MakeUser:
    counter = {"n": 0}

    def _make(
        role: Role = Role.ANALYST,
        *,
        email: str | None = None,
        grants: tuple[Permission, ...] = (),
        is_active: bool = True,
    ) -> User:
        counter["n"] += 1
        role_row = get_role_by_name(db, role.value)
        assert role_row is not None
        user = User(
            email=email or f"{role.value}{counter['n']}@test.local",
            full_name=f"Test {role.value.title()} {counter['n']}",
            password_hash=_TEST_PASSWORD_HASH,
            role=role_row,
            role_id=role_row.id,
            is_active=is_active,
        )
        db.add(user)
        db.flush()
        for p in grants:
            user.grants.append(UserPermissionGrant(user_id=user.id, permission=p.value))
        db.flush()
        return user

    return _make


def login(client: TestClient, email: str, password: str = TEST_PASSWORD) -> str:
    resp = client.post("/api/v1/auth/login", json={"email": email, "password": password})
    assert resp.status_code == 200, resp.text
    token: str = resp.json()["access_token"]
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


AuthHeaders = Callable[..., dict[str, str]]


@pytest.fixture
def auth_headers(client: TestClient, make_user: MakeUser) -> AuthHeaders:
    """``auth_headers(Role.ADMIN)`` → Authorization header for a fresh user of that role."""

    def _headers(role: Role, grants: tuple[Permission, ...] = ()) -> dict[str, str]:
        user = make_user(role, grants=grants)
        return bearer(login(client, user.email))

    return _headers
