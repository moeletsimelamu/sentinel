"""Model registry: model versions and their evaluations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "model_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("version", sa.String(160), nullable=False),
        sa.Column("family", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("artefact_path", sa.String(300)),
        sa.Column("artefact_sha256", sa.String(64)),
        sa.Column("feature_schema_hash", sa.String(32), nullable=False),
        sa.Column("feature_set_version", sa.String(32), nullable=False),
        sa.Column("training_dataset_version", sa.String(80), nullable=False),
        sa.Column("selected_by_experiment", sa.Boolean(), nullable=False),
        sa.Column("manifest", postgresql.JSONB(), nullable=False),
        sa.Column("registered_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("activated_by_id", sa.Uuid()),
        sa.Column("activated_at", TZ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_versions")),
        sa.UniqueConstraint("version", name=op.f("uq_model_versions_version")),
        sa.ForeignKeyConstraint(
            ["activated_by_id"], ["users.id"], name=op.f("fk_model_versions_activated_by_id_users")
        ),
        sa.CheckConstraint(
            "family IN ('isolation_forest', 'logreg', 'random_forest', 'rules_baseline')",
            name=op.f("ck_model_versions_family_valid"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'registered', 'retired')",
            name=op.f("ck_model_versions_status_valid"),
        ),
        sa.CheckConstraint(
            "family = 'rules_baseline' OR "
            "(artefact_path IS NOT NULL AND artefact_sha256 IS NOT NULL)",
            name=op.f("ck_model_versions_artefact_required"),
        ),
    )
    op.create_index(
        "uq_model_versions_one_active",
        "model_versions",
        ["status"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )
    op.create_table(
        "model_evaluations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("model_version_id", sa.Uuid(), nullable=False),
        sa.Column("dataset_version", sa.String(80), nullable=False),
        sa.Column("split", sa.String(16), nullable=False),
        sa.Column("threshold_kind", sa.String(16), nullable=False),
        sa.Column("threshold", sa.Double()),
        sa.Column("metrics", postgresql.JSONB(), nullable=False),
        sa.Column("ci95", postgresql.JSONB()),
        sa.Column("curves", postgresql.JSONB()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_evaluations")),
        sa.ForeignKeyConstraint(
            ["model_version_id"],
            ["model_versions.id"],
            name=op.f("fk_model_evaluations_model_version_id_model_versions"),
        ),
        sa.CheckConstraint(
            "split IN ('holdout', 'test', 'validation')",
            name=op.f("ck_model_evaluations_split_valid"),
        ),
    )
    op.create_index(
        "ix_model_evaluations_model", "model_evaluations", ["model_version_id", "split"]
    )
    for stmt in (
        "CREATE TRIGGER model_evaluations_append_only BEFORE UPDATE OR DELETE ON "
        "model_evaluations FOR EACH ROW EXECUTE FUNCTION forbid_modification()",
        "CREATE TRIGGER model_evaluations_no_truncate BEFORE TRUNCATE ON model_evaluations "
        "FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification()",
    ):
        op.execute(stmt)
    op.create_foreign_key(
        op.f("fk_risk_assessments_model_version_id_model_versions"),
        "risk_assessments",
        "model_versions",
        ["model_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        op.f("fk_risk_assessments_model_version_id_model_versions"),
        "risk_assessments",
        type_="foreignkey",
    )
    op.execute("DROP TRIGGER IF EXISTS model_evaluations_no_truncate ON model_evaluations")
    op.execute("DROP TRIGGER IF EXISTS model_evaluations_append_only ON model_evaluations")
    op.drop_table("model_evaluations")
    op.drop_table("model_versions")
