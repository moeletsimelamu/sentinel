"""Ingestion: job queue, customers, import batches, row issues, transactions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-24

Vocabularies are copied (not imported) so this migration stays reproducible; a test
asserts the CHECK constraints match ``sentinel.core.vocabulary``.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)

CURRENCIES = ["EUR", "GBP", "USD", "ZAR"]
TXN_TYPES = ["atm_withdrawal", "purchase", "refund", "transfer_in", "transfer_out"]
CHANNELS = ["atm", "bank_transfer", "card_present", "mobile_app", "online"]
STATUSES = ["completed", "declined", "failed"]
CATEGORIES = [
    "atm_cash",
    "crypto_exchange",
    "electronics",
    "entertainment",
    "fuel",
    "gift_cards",
    "groceries",
    "health_pharmacy",
    "home_furniture",
    "money_transfer",
    "online_marketplace",
    "payroll",
    "restaurants",
    "retail_clothing",
    "subscriptions",
    "travel_airline",
    "travel_hotel",
    "utilities",
]


# Migration 0001 passed full check-constraint names through the metadata naming convention,
# which prefixed them a second time (``ck_users_ck_users_...``). Rename them to the names
# the models expect. Names in this migration use ``op.f()`` so they are taken verbatim.
_USERS_CHECKS = ["email_lowercase", "failed_login_count_non_negative", "version_positive"]


def _one_of(column: str, values: Iterable[str]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in sorted(values))})"


def upgrade() -> None:
    for name in _USERS_CHECKS:
        op.execute(
            f"ALTER TABLE users RENAME CONSTRAINT ck_users_ck_users_{name} TO ck_users_{name}"
        )

    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("run_after", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("locked_by", sa.String(128)),
        sa.Column("locked_at", TZ),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", TZ),
        sa.PrimaryKeyConstraint("id", name="pk_processing_jobs"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'dead')",
            name=op.f("ck_processing_jobs_status_valid"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts >= 1", name=op.f("ck_processing_jobs_attempts_valid")
        ),
    )
    op.create_index(
        "ix_processing_jobs_claimable",
        "processing_jobs",
        ["run_after"],
        postgresql_where=sa.text("status = 'queued'"),
    )
    op.create_index("ix_processing_jobs_status", "processing_jobs", ["status", "created_at"])

    op.create_table(
        "customers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_ref", sa.String(64), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_customers"),
        sa.UniqueConstraint("external_ref", name="uq_customers_external_ref"),
    )

    op.create_table(
        "import_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("source", sa.String(8), nullable=False),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("filename", sa.String(255)),
        sa.Column("file_sha256", sa.String(64)),
        sa.Column("file_size_bytes", sa.BigInteger()),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("accepted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejected_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("possible_duplicate_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "ignored_columns",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("file_error", postgresql.JSONB()),
        sa.Column("identical_to_batch_id", sa.Uuid()),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("uploaded_by_id", sa.Uuid()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", TZ),
        sa.Column("finished_at", TZ),
        sa.PrimaryKeyConstraint("id", name="pk_import_batches"),
        sa.ForeignKeyConstraint(
            ["identical_to_batch_id"],
            ["import_batches.id"],
            ondelete="SET NULL",
            name="fk_import_batches_identical_to_batch_id_import_batches",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["processing_jobs.id"],
            ondelete="SET NULL",
            name="fk_import_batches_job_id_processing_jobs",
        ),
        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"],
            ondelete="RESTRICT",
            name="fk_import_batches_uploaded_by_id_users",
        ),
        sa.CheckConstraint("source IN ('csv', 'api')", name=op.f("ck_import_batches_source_valid")),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'completed', 'rejected', 'failed')",
            name=op.f("ck_import_batches_status_valid"),
        ),
        sa.CheckConstraint(
            "row_count >= 0 AND accepted_count >= 0 AND rejected_count >= 0 "
            "AND duplicate_count >= 0 AND possible_duplicate_count >= 0",
            name=op.f("ck_import_batches_counts_non_negative"),
        ),
    )
    op.create_index("ix_import_batches_created_at", "import_batches", ["created_at"])
    op.create_index("ix_import_batches_file_sha256", "import_batches", ["file_sha256"])

    op.create_table(
        "import_files",
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("batch_id", name="pk_import_files"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            ondelete="CASCADE",
            name="fk_import_files_batch_id_import_batches",
        ),
    )

    op.create_table(
        "transactions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("external_id", sa.String(64), nullable=False),
        sa.Column("source_system", sa.String(32), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("amount", sa.Numeric(18, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False),
        sa.Column("txn_type", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("merchant_category", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("city", sa.String(64)),
        sa.Column("country", sa.String(2)),
        sa.Column("lat", sa.Double()),
        sa.Column("lon", sa.Double()),
        sa.Column("device_id_hash", sa.String(64)),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("ingested_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name="pk_transactions"),
        sa.UniqueConstraint(
            "source_system", "external_id", name="uq_transactions_source_external_id"
        ),
        sa.ForeignKeyConstraint(
            ["customer_id"],
            ["customers.id"],
            ondelete="RESTRICT",
            name="fk_transactions_customer_id_customers",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            ondelete="RESTRICT",
            name="fk_transactions_batch_id_import_batches",
        ),
        sa.CheckConstraint("amount > 0", name=op.f("ck_transactions_amount_positive")),
        sa.CheckConstraint(
            _one_of("currency", CURRENCIES), name=op.f("ck_transactions_currency_valid")
        ),
        sa.CheckConstraint(
            _one_of("txn_type", TXN_TYPES), name=op.f("ck_transactions_txn_type_valid")
        ),
        sa.CheckConstraint(
            _one_of("channel", CHANNELS), name=op.f("ck_transactions_channel_valid")
        ),
        sa.CheckConstraint(
            _one_of("merchant_category", CATEGORIES), name=op.f("ck_transactions_category_valid")
        ),
        sa.CheckConstraint(_one_of("status", STATUSES), name=op.f("ck_transactions_status_valid")),
        sa.CheckConstraint(
            "lat IS NULL OR lat BETWEEN -90 AND 90", name=op.f("ck_transactions_lat_range")
        ),
        sa.CheckConstraint(
            "lon IS NULL OR lon BETWEEN -180 AND 180", name=op.f("ck_transactions_lon_range")
        ),
        sa.CheckConstraint(
            "(lat IS NULL) = (lon IS NULL)", name=op.f("ck_transactions_coordinates_paired")
        ),
    )
    op.create_index("ix_transactions_customer_time", "transactions", ["customer_id", "occurred_at"])
    op.create_index("ix_transactions_time_id", "transactions", ["occurred_at", "id"])
    op.create_index("ix_transactions_batch_id", "transactions", ["batch_id"])
    op.create_index("ix_transactions_content_hash", "transactions", ["content_hash"])
    op.execute(
        """
        CREATE TRIGGER transactions_append_only
        BEFORE UPDATE OR DELETE ON transactions
        FOR EACH ROW EXECUTE FUNCTION forbid_modification();
        """
    )
    op.execute(
        """
        CREATE TRIGGER transactions_no_truncate
        BEFORE TRUNCATE ON transactions
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification();
        """
    )

    op.create_table(
        "import_row_issues",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("batch_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("external_id", sa.String(64)),
        sa.Column("transaction_id", sa.Uuid()),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("raw_row", postgresql.JSONB()),
        sa.PrimaryKeyConstraint("id", name="pk_import_row_issues"),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["import_batches.id"],
            ondelete="CASCADE",
            name="fk_import_row_issues_batch_id_import_batches",
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            ondelete="RESTRICT",
            name="fk_import_row_issues_transaction_id_transactions",
        ),
        sa.CheckConstraint(
            "outcome IN ('rejected', 'duplicate', 'possible_duplicate')",
            name=op.f("ck_import_row_issues_outcome_valid"),
        ),
    )
    op.create_index(
        "ix_import_row_issues_batch_row", "import_row_issues", ["batch_id", "row_number"]
    )


def downgrade() -> None:
    op.drop_table("import_row_issues")
    op.execute("DROP TRIGGER IF EXISTS transactions_no_truncate ON transactions")
    op.execute("DROP TRIGGER IF EXISTS transactions_append_only ON transactions")
    op.drop_table("transactions")
    op.drop_table("import_files")
    op.drop_table("import_batches")
    op.drop_table("customers")
    op.drop_table("processing_jobs")
    for name in _USERS_CHECKS:
        op.execute(
            f"ALTER TABLE users RENAME CONSTRAINT ck_users_{name} TO ck_users_ck_users_{name}"
        )
