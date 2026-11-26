"""Alerts, alert transactions, append-only alert events; model contributions on assessments.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-26
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.add_column("risk_assessments", sa.Column("contributions", postgresql.JSONB()))
    op.execute("CREATE SEQUENCE alert_reference_seq START 1")

    op.create_table(
        "alerts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("reference", sa.String(16), nullable=False),
        sa.Column("customer_id", sa.Uuid(), nullable=False),
        sa.Column("primary_assessment_id", sa.Uuid(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("priority_score", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("assignee_id", sa.Uuid()),
        sa.Column("case_id", sa.Uuid()),
        sa.Column("resolution_reason", sa.String(40)),
        sa.Column("transaction_count", sa.Integer(), nullable=False),
        sa.Column("last_activity_at", TZ, nullable=False),
        sa.Column("opened_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("closed_at", TZ),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alerts")),
        sa.UniqueConstraint("reference", name=op.f("uq_alerts_reference")),
        sa.ForeignKeyConstraint(
            ["customer_id"], ["customers.id"], name=op.f("fk_alerts_customer_id_customers")
        ),
        sa.ForeignKeyConstraint(
            ["primary_assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_alerts_primary_assessment_id_risk_assessments"),
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"], ["users.id"], name=op.f("fk_alerts_assignee_id_users")
        ),
        sa.CheckConstraint(
            "status IN ('assigned', 'closed', 'escalated', 'in_review', 'new')",
            name=op.f("ck_alerts_status_valid"),
        ),
        sa.CheckConstraint(
            "severity IN ('critical', 'high', 'low', 'medium')",
            name=op.f("ck_alerts_severity_valid"),
        ),
        sa.CheckConstraint(
            "resolution_reason IS NULL OR resolution_reason IN ('duplicate_alert', "
            "'escalated_to_case', 'false_positive_explained', 'insufficient_information', "
            "'legitimate_customer_behaviour')",
            name=op.f("ck_alerts_resolution_valid"),
        ),
        sa.CheckConstraint(
            "(status IN ('closed', 'escalated')) = "
            "(resolution_reason IS NOT NULL AND closed_at IS NOT NULL)",
            name=op.f("ck_alerts_terminal_has_resolution"),
        ),
        sa.CheckConstraint(
            "status NOT IN ('assigned', 'in_review') OR assignee_id IS NOT NULL",
            name=op.f("ck_alerts_worked_has_assignee"),
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_alerts_version_positive")),
    )
    op.create_index("ix_alerts_queue", "alerts", ["status", "priority_score", "opened_at"])
    op.create_index(
        "ix_alerts_open_assignee",
        "alerts",
        ["assignee_id"],
        postgresql_where=sa.text("status IN ('new', 'assigned', 'in_review')"),
    )
    op.create_index("ix_alerts_customer_status", "alerts", ["customer_id", "status"])

    op.create_table(
        "alert_transactions",
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("linked_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("alert_id", "transaction_id", name=op.f("pk_alert_transactions")),
        sa.UniqueConstraint("transaction_id", name=op.f("uq_alert_transactions_transaction_id")),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"], name=op.f("fk_alert_transactions_alert_id_alerts")
        ),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_alert_transactions_transaction_id_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_alert_transactions_assessment_id_risk_assessments"),
        ),
    )

    op.create_table(
        "alert_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("alert_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("from_status", sa.String(16)),
        sa.Column("to_status", sa.String(16)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_events")),
        sa.ForeignKeyConstraint(
            ["alert_id"], ["alerts.id"], name=op.f("fk_alert_events_alert_id_alerts")
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], name=op.f("fk_alert_events_actor_id_users")
        ),
    )
    op.create_index("ix_alert_events_alert_time", "alert_events", ["alert_id", "created_at"])
    for table in ("alert_events", "alert_transactions"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
        )


def downgrade() -> None:
    for table in ("alert_events", "alert_transactions"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("alert_events")
    op.drop_table("alert_transactions")
    op.drop_table("alerts")
    op.execute("DROP SEQUENCE IF EXISTS alert_reference_seq")
    op.drop_column("risk_assessments", "contributions")
