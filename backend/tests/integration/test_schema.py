"""The migrations, the ORM models and the shared vocabularies must agree."""

from __future__ import annotations

import re

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import CheckConstraint, text
from sqlalchemy.engine import Engine

from sentinel.core.db import Base
from sentinel.core.vocabulary import CURRENCIES, Channel, MerchantCategory, TxnStatus, TxnType
from sentinel.main import app  # noqa: F401 - imports every module's models

pytestmark = pytest.mark.integration


def test_models_match_migrations(engine: Engine) -> None:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Models and migrations differ: {diff}"


def test_constraint_names_match_models(engine: Engine) -> None:
    """compare_metadata ignores CHECK constraints, so compare their names explicitly."""
    expected = {
        (table.name, str(c.name))
        for table in Base.metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint)
    }
    with engine.connect() as conn:
        actual = {
            (str(t), str(n))
            for t, n in conn.execute(
                text(
                    "SELECT conrelid::regclass::text, conname FROM pg_constraint "
                    "WHERE contype = 'c' AND connamespace = 'public'::regnamespace"
                )
            )
        }
    assert actual == expected


def test_migrations_downgrade_and_upgrade_cleanly(engine: Engine) -> None:
    from tests.conftest import alembic_config

    cfg = alembic_config()
    command.downgrade(cfg, "0001")
    command.upgrade(cfg, "head")
    test_constraint_names_match_models(engine)


@pytest.mark.parametrize(
    ("constraint", "vocabulary"),
    [
        ("ck_transactions_currency_valid", set(CURRENCIES)),
        ("ck_transactions_txn_type_valid", {v.value for v in TxnType}),
        ("ck_transactions_channel_valid", {v.value for v in Channel}),
        ("ck_transactions_category_valid", {v.value for v in MerchantCategory}),
        ("ck_transactions_status_valid", {v.value for v in TxnStatus}),
    ],
)
def test_database_checks_match_vocabulary(
    engine: Engine, constraint: str, vocabulary: set[str]
) -> None:
    with engine.connect() as conn:
        definition = conn.execute(
            text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
            {"n": constraint},
        ).scalar_one()
    assert set(re.findall(r"'([^']+)'", definition)) == vocabulary
