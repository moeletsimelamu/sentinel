"""Detection: rule definitions/versions/sets, scoring configs, risk assessments, rule hits.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-24

Seeds rule catalogue v1, rule set v1 and scoring config v1. The seed values are copied
(not imported) so the migration stays reproducible; a test asserts they match the code.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)
APPEND_ONLY = ["rule_versions", "rule_sets", "rule_set_members", "risk_assessments", "rule_hits"]

# (id, code, name, description, category, params, weight, severity, force_alert)
RULES = [
    (1, "LARGE_AMOUNT_ABSOLUTE", "Unusually large transaction",
     "Amount (converted to ZAR) at or above a fixed threshold.", "amount",
     {"min_amount_zar": 50000.0}, 30, "high", False),
    (2, "LARGE_AMOUNT_RELATIVE", "Spending far above customer's norm",
     "Amount is many times the customer's 90-day median completed amount.", "amount",
     {"min_multiple": 8.0, "min_history": 10}, 25, "medium", False),
    (3, "RAPID_REPEAT", "Rapid repeated transactions",
     "Several transactions in the same merchant category within 10 minutes.", "velocity",
     {"min_count": 3}, 20, "medium", False),
    (4, "VELOCITY_1H", "Unusual transaction frequency",
     "Many transactions by the customer in the preceding hour.", "velocity",
     {"min_prior_count": 6}, 20, "medium", False),
    (5, "VELOCITY_RATIO_24H", "Frequency far above customer's baseline",
     "Last-24h transaction count compared with the customer's 30-day daily average.",
     "velocity", {"min_ratio": 5.0, "min_prior_count_24h": 5}, 15, "medium", False),
    (6, "FAILED_ATTEMPTS", "Multiple failed transactions",
     "Declined or failed attempts by the customer in the preceding 24 hours.", "velocity",
     {"min_failed": 3}, 25, "high", False),
    (7, "IMPOSSIBLE_TRAVEL", "Implausible travel between transactions",
     "Distance from the previous located transaction implies an unrealistic speed.",
     "geography", {"min_speed_kmh": 900.0, "min_distance_km": 300.0}, 35, "high", True),
    (8, "NEW_LOCATION_HIGH_VALUE", "High value in a new city",
     "First transaction in this city for 90 days, at well above the usual amount.",
     "geography", {"min_multiple": 3.0, "min_history": 5}, 15, "low", False),
    (9, "UNUSUAL_HOUR", "Unusual time of day",
     "Hour the customer is rarely active, at above the usual amount.", "behaviour",
     {"min_rarity": 0.97, "min_multiple": 2.0, "min_history": 10}, 10, "low", False),
    (10, "NEW_DEVICE_HIGH_VALUE", "High value from a new device",
     "First use of this device in 90 days, at well above the usual amount.", "device",
     {"min_multiple": 3.0, "min_history": 5}, 20, "medium", False),
    (11, "HIGH_RISK_CATEGORY_SHIFT", "First use of a high-risk category",
     "First transaction in 90 days in a category often used to move funds out.",
     "behaviour", {"min_history": 5}, 20, "medium", False),
    (12, "BEHAVIOUR_SHIFT", "Change in spending mix",
     "Last-7-day category mix differs strongly from the 90-day pattern.", "behaviour",
     {"min_shift": 0.5, "min_history": 20}, 10, "low", False),
]  # fmt: skip

BANDS = [
    {"name": "low", "min_score": 0},
    {"name": "medium", "min_score": 40},
    {"name": "high", "min_score": 70},
    {"name": "critical", "min_score": 90},
]
SEED_REASON = "Initial catalogue v1 (Phase 4)"


def upgrade() -> None:
    op.add_column("processing_jobs", sa.Column("result", postgresql.JSONB()))
    op.add_column("import_batches", sa.Column("scoring_job_id", sa.Uuid()))
    op.create_foreign_key(
        op.f("fk_import_batches_scoring_job_id_processing_jobs"),
        "import_batches",
        "processing_jobs",
        ["scoring_job_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "rule_definitions",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_definitions")),
        sa.UniqueConstraint("code", name=op.f("uq_rule_definitions_code")),
    )
    op.create_table(
        "rule_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_id", sa.SmallInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("params", postgresql.JSONB(), nullable=False),
        sa.Column("weight", sa.SmallInteger(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("force_alert", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.Uuid()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_versions")),
        sa.UniqueConstraint("rule_id", "version", name=op.f("uq_rule_versions_rule_id_version")),
        sa.ForeignKeyConstraint(
            ["rule_id"],
            ["rule_definitions.id"],
            name=op.f("fk_rule_versions_rule_id_rule_definitions"),
        ),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_rule_versions_created_by_id_users")
        ),
        sa.CheckConstraint("version >= 1", name=op.f("ck_rule_versions_version_positive")),
        sa.CheckConstraint("weight BETWEEN 0 AND 100", name=op.f("ck_rule_versions_weight_range")),
        sa.CheckConstraint(
            "severity IN ('high', 'low', 'medium')", name=op.f("ck_rule_versions_severity_valid")
        ),
    )
    op.create_table(
        "rule_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.Uuid()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_sets")),
        sa.UniqueConstraint("version", name=op.f("uq_rule_sets_version")),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_rule_sets_created_by_id_users")
        ),
    )
    op.create_table(
        "rule_set_members",
        sa.Column("rule_set_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version_id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("rule_set_id", "rule_version_id", name=op.f("pk_rule_set_members")),
        sa.ForeignKeyConstraint(
            ["rule_set_id"],
            ["rule_sets.id"],
            name=op.f("fk_rule_set_members_rule_set_id_rule_sets"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["rule_versions.id"],
            name=op.f("fk_rule_set_members_rule_version_id_rule_versions"),
        ),
    )
    op.create_table(
        "scoring_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("w_rule", sa.Numeric(4, 3), nullable=False),
        sa.Column("w_model", sa.Numeric(4, 3), nullable=False),
        sa.Column("rule_cap", sa.SmallInteger(), nullable=False),
        sa.Column("bands", postgresql.JSONB(), nullable=False),
        sa.Column("alert_threshold", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("change_reason", sa.Text(), nullable=False),
        sa.Column("created_by_id", sa.Uuid()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scoring_configs")),
        sa.UniqueConstraint("version", name=op.f("uq_scoring_configs_version")),
        sa.ForeignKeyConstraint(
            ["created_by_id"], ["users.id"], name=op.f("fk_scoring_configs_created_by_id_users")
        ),
        sa.CheckConstraint(
            "w_rule BETWEEN 0 AND 1 AND w_model BETWEEN 0 AND 1 AND w_rule + w_model = 1",
            name=op.f("ck_scoring_configs_weights_valid"),
        ),
        sa.CheckConstraint(
            "rule_cap BETWEEN 1 AND 100", name=op.f("ck_scoring_configs_rule_cap_range")
        ),
        sa.CheckConstraint(
            "alert_threshold BETWEEN 0 AND 100",
            name=op.f("ck_scoring_configs_alert_threshold_range"),
        ),
    )
    op.create_index(
        "uq_scoring_configs_one_active",
        "scoring_configs",
        ["is_active"],
        unique=True,
        postgresql_where=sa.text("is_active"),
    )
    op.create_table(
        "risk_assessments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("transaction_id", sa.Uuid(), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("rule_score", sa.SmallInteger(), nullable=False),
        sa.Column("model_score", sa.Double()),
        sa.Column("model_version_id", sa.Uuid()),
        sa.Column("risk_score", sa.SmallInteger(), nullable=False),
        sa.Column("risk_category", sa.String(16), nullable=False),
        sa.Column("force_alert", sa.Boolean(), nullable=False),
        sa.Column("scoring_config_id", sa.Uuid(), nullable=False),
        sa.Column("rule_set_id", sa.Uuid(), nullable=False),
        sa.Column("feature_set_version", sa.String(32), nullable=False),
        sa.Column("features", postgresql.JSONB(), nullable=False),
        sa.Column("reasons", postgresql.JSONB(), nullable=False),
        sa.Column("job_id", sa.Uuid()),
        sa.Column("assessed_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_risk_assessments")),
        sa.ForeignKeyConstraint(
            ["transaction_id"],
            ["transactions.id"],
            name=op.f("fk_risk_assessments_transaction_id_transactions"),
        ),
        sa.ForeignKeyConstraint(
            ["scoring_config_id"],
            ["scoring_configs.id"],
            name=op.f("fk_risk_assessments_scoring_config_id_scoring_configs"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_set_id"],
            ["rule_sets.id"],
            name=op.f("fk_risk_assessments_rule_set_id_rule_sets"),
        ),
        sa.CheckConstraint(
            "mode IN ('combined', 'rules_only')", name=op.f("ck_risk_assessments_mode_valid")
        ),
        sa.CheckConstraint(
            "risk_score BETWEEN 0 AND 100", name=op.f("ck_risk_assessments_risk_score_range")
        ),
        sa.CheckConstraint(
            "rule_score BETWEEN 0 AND 100", name=op.f("ck_risk_assessments_rule_score_range")
        ),
    )
    op.create_index(
        "ix_risk_assessments_txn_time", "risk_assessments", ["transaction_id", "assessed_at"]
    )
    op.create_index(
        "ix_risk_assessments_assessed_at",
        "risk_assessments",
        ["assessed_at"],
        postgresql_using="brin",
    )
    op.create_table(
        "rule_hits",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("assessment_id", sa.Uuid(), nullable=False),
        sa.Column("rule_version_id", sa.Uuid(), nullable=False),
        sa.Column("rule_code", sa.String(64), nullable=False),
        sa.Column("weight_applied", sa.SmallInteger(), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("force_alert", sa.Boolean(), nullable=False),
        sa.Column("observed", postgresql.JSONB(), nullable=False),
        sa.Column("threshold", postgresql.JSONB(), nullable=False),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_rule_hits")),
        sa.ForeignKeyConstraint(
            ["assessment_id"],
            ["risk_assessments.id"],
            name=op.f("fk_rule_hits_assessment_id_risk_assessments"),
        ),
        sa.ForeignKeyConstraint(
            ["rule_version_id"],
            ["rule_versions.id"],
            name=op.f("fk_rule_hits_rule_version_id_rule_versions"),
        ),
    )
    op.create_index("ix_rule_hits_assessment_id", "rule_hits", ["assessment_id"])
    op.create_index("ix_rule_hits_rule_version_id", "rule_hits", ["rule_version_id"])
    op.create_index("ix_rule_hits_rule_code", "rule_hits", ["rule_code"])

    for table in APPEND_ONLY:
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION forbid_modification()"
        )
        op.execute(
            f"CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()"
        )

    _seed()


def _seed() -> None:
    conn = op.get_bind()
    rule_set_id = uuid.uuid4()
    conn.execute(
        sa.text("INSERT INTO rule_sets (id, version, change_reason) VALUES (:id, 1, :reason)"),
        {"id": rule_set_id, "reason": SEED_REASON},
    )
    for rid, code, name, desc, category, params, weight, severity, force in RULES:
        conn.execute(
            sa.text(
                "INSERT INTO rule_definitions (id, code, name, description, category) "
                "VALUES (:id, :code, :name, :desc, :category)"
            ),
            {"id": rid, "code": code, "name": name, "desc": desc, "category": category},
        )
        version_id = uuid.uuid4()
        conn.execute(
            sa.text(
                "INSERT INTO rule_versions (id, rule_id, version, params, weight, severity, "
                "force_alert, is_active, change_reason) VALUES (:id, :rule_id, 1, "
                "CAST(:params AS jsonb), :weight, :severity, :force, true, :reason)"
            ),
            {
                "id": version_id,
                "rule_id": rid,
                "params": json.dumps(params),
                "weight": weight,
                "severity": severity,
                "force": force,
                "reason": SEED_REASON,
            },
        )
        conn.execute(
            sa.text("INSERT INTO rule_set_members VALUES (:set_id, :version_id)"),
            {"set_id": rule_set_id, "version_id": version_id},
        )
    conn.execute(
        sa.text(
            "INSERT INTO scoring_configs (id, version, w_rule, w_model, rule_cap, bands, "
            "alert_threshold, is_active, change_reason) VALUES (:id, 1, 0.5, 0.5, 100, "
            "CAST(:bands AS jsonb), 70, true, :reason)"
        ),
        {"id": uuid.uuid4(), "bands": json.dumps(BANDS), "reason": SEED_REASON},
    )


def downgrade() -> None:
    for table in APPEND_ONLY:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_no_truncate ON {table}")
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.drop_table("rule_hits")
    op.drop_table("risk_assessments")
    op.drop_table("scoring_configs")
    op.drop_table("rule_set_members")
    op.drop_table("rule_sets")
    op.drop_table("rule_versions")
    op.drop_table("rule_definitions")
    op.drop_constraint(
        op.f("fk_import_batches_scoring_job_id_processing_jobs"),
        "import_batches",
        type_="foreignkey",
    )
    op.drop_column("import_batches", "scoring_job_id")
    op.drop_column("processing_jobs", "result")
