"""Baseline: identity & access, refresh tokens, append-only audit log.

Revision ID: 0001
Revises:
Create Date: 2026-11-23

Roles and their default permissions are seeded here as reference data. The mapping is
copied (not imported) so this migration stays reproducible even if application code
changes later; a test checks it matches ``sentinel.core.permissions``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = [
    (1, "admin", "Manages users, detection rules, scoring and models. Does not adjudicate."),
    (2, "analyst", "Reviews alerts and investigates cases."),
    (3, "auditor", "Read-only access to investigations, decisions, audit and model history."),
]

ROLE_PERMISSIONS = {
    "admin": [
        "users:manage",
        "rules:read",
        "rules:configure",
        "scoring:configure",
        "models:read",
        "models:activate",
        "transactions:read",
        "ingestion:import",
        "detection:run",
        "alerts:read",
        "alerts:assign_others",
        "cases:read",
        "audit:read",
        "reports:read",
    ],
    "analyst": [
        "rules:read",
        "models:read",
        "transactions:read",
        "ingestion:import",
        "detection:run",
        "alerts:read",
        "alerts:work",
        "cases:read",
        "cases:work",
        "reports:read",
    ],
    "auditor": [
        "rules:read",
        "models:read",
        "transactions:read",
        "alerts:read",
        "cases:read",
        "audit:read",
        "reports:read",
    ],
}

TZ = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.SmallInteger(), nullable=False),
        sa.Column("name", sa.String(32), nullable=False),
        sa.Column("description", sa.String(255), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_roles"),
        sa.UniqueConstraint("name", name="uq_roles_name"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
        sa.Column("permission", sa.String(64), nullable=False),
        sa.PrimaryKeyConstraint("role_id", "permission", name="pk_role_permissions"),
        sa.ForeignKeyConstraint(
            ["role_id"],
            ["roles.id"],
            ondelete="CASCADE",
            name="fk_role_permissions_role_id_roles",
        ),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(254), nullable=False),
        sa.Column("full_name", sa.String(120), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role_id", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("failed_login_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("locked_until", TZ),
        sa.Column("last_login_at", TZ),
        sa.Column("password_changed_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.ForeignKeyConstraint(["role_id"], ["roles.id"], name="fk_users_role_id_roles"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
        sa.CheckConstraint(
            "failed_login_count >= 0", name="ck_users_failed_login_count_non_negative"
        ),
        sa.CheckConstraint("version >= 1", name="ck_users_version_positive"),
    )
    op.create_table(
        "user_permission_grants",
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("permission", sa.String(64), nullable=False),
        sa.Column("granted_by_id", sa.Uuid()),
        sa.Column("granted_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.PrimaryKeyConstraint("user_id", "permission", name="pk_user_permission_grants"),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            ondelete="CASCADE",
            name="fk_user_permission_grants_user_id_users",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by_id"], ["users.id"], name="fk_user_permission_grants_granted_by_id_users"
        ),
    )
    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("issued_at", TZ, nullable=False),
        sa.Column("expires_at", TZ, nullable=False),
        sa.Column("revoked_at", TZ),
        sa.Column("replaced_by_id", sa.Uuid()),
        sa.PrimaryKeyConstraint("id", name="pk_refresh_tokens"),
        sa.UniqueConstraint("token_hash", name="uq_refresh_tokens_token_hash"),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE", name="fk_refresh_tokens_user_id_users"
        ),
        sa.ForeignKeyConstraint(
            ["replaced_by_id"],
            ["refresh_tokens.id"],
            ondelete="SET NULL",
            name="fk_refresh_tokens_replaced_by_id_refresh_tokens",
        ),
    )
    op.create_index("ix_refresh_tokens_family_id", "refresh_tokens", ["family_id"])
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("occurred_at", TZ, nullable=False, server_default=sa.func.now()),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("actor_role", sa.String(32)),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("entity_id", sa.String(64)),
        sa.Column("before", postgresql.JSONB()),
        sa.Column("after", postgresql.JSONB()),
        sa.Column("request_id", sa.String(64)),
        sa.Column("ip_hash", sa.String(64)),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
        sa.ForeignKeyConstraint(
            ["actor_id"], ["users.id"], ondelete="RESTRICT", name="fk_audit_log_actor_id_users"
        ),
    )
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id", "occurred_at"])
    op.create_index("ix_audit_log_actor", "audit_log", ["actor_id", "occurred_at"])
    op.create_index("ix_audit_log_action", "audit_log", ["action", "occurred_at"])

    # ---- append-only enforcement (reused by later event/assessment tables) ----
    op.execute(
        """
        CREATE FUNCTION forbid_modification() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'table % is append-only (% not allowed)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END;
        $$;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_append_only
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION forbid_modification();
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_truncate
        BEFORE TRUNCATE ON audit_log
        FOR EACH STATEMENT EXECUTE FUNCTION forbid_modification();
        """
    )

    # ---- reference data ----
    roles = sa.table(
        "roles",
        sa.column("id", sa.SmallInteger),
        sa.column("name", sa.String),
        sa.column("description", sa.String),
    )
    op.bulk_insert(roles, [{"id": i, "name": n, "description": d} for i, n, d in ROLES])
    role_ids = {name: i for i, name, _ in ROLES}
    role_permissions = sa.table(
        "role_permissions",
        sa.column("role_id", sa.SmallInteger),
        sa.column("permission", sa.String),
    )
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": role_ids[role], "permission": perm}
            for role, perms in ROLE_PERMISSIONS.items()
            for perm in perms
        ],
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_truncate ON audit_log")
    op.execute("DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log")
    op.drop_table("audit_log")
    op.execute("DROP FUNCTION IF EXISTS forbid_modification()")
    op.drop_table("refresh_tokens")
    op.drop_table("user_permission_grants")
    op.drop_table("users")
    op.drop_table("role_permissions")
    op.drop_table("roles")
