"""Roles and permissions.

The database (``roles``/``role_permissions``) is the runtime source of truth. The mapping
below is the canonical default: the baseline migration seeds it, and a test asserts the
database and this module agree, so drift is caught in CI.
"""

from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    ADMIN = "admin"
    ANALYST = "analyst"
    AUDITOR = "auditor"


class Permission(StrEnum):
    USERS_MANAGE = "users:manage"
    RULES_READ = "rules:read"
    RULES_CONFIGURE = "rules:configure"
    SCORING_CONFIGURE = "scoring:configure"
    MODELS_READ = "models:read"
    MODELS_ACTIVATE = "models:activate"
    TRANSACTIONS_READ = "transactions:read"
    INGESTION_IMPORT = "ingestion:import"
    DETECTION_RUN = "detection:run"
    ALERTS_READ = "alerts:read"
    ALERTS_WORK = "alerts:work"
    ALERTS_ASSIGN_OTHERS = "alerts:assign_others"
    CASES_READ = "cases:read"
    CASES_WORK = "cases:work"
    AUDIT_READ = "audit:read"
    REPORTS_READ = "reports:read"


P = Permission

DEFAULT_ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.ADMIN: frozenset(
        {
            P.USERS_MANAGE,
            P.RULES_READ,
            P.RULES_CONFIGURE,
            P.SCORING_CONFIGURE,
            P.MODELS_READ,
            P.MODELS_ACTIVATE,
            P.TRANSACTIONS_READ,
            P.INGESTION_IMPORT,
            P.DETECTION_RUN,
            P.ALERTS_READ,
            P.ALERTS_ASSIGN_OTHERS,
            P.CASES_READ,
            P.AUDIT_READ,
            P.REPORTS_READ,
        }
    ),
    Role.ANALYST: frozenset(
        {
            P.RULES_READ,
            P.MODELS_READ,
            P.TRANSACTIONS_READ,
            P.INGESTION_IMPORT,
            P.DETECTION_RUN,
            P.ALERTS_READ,
            P.ALERTS_WORK,
            P.CASES_READ,
            P.CASES_WORK,
            P.REPORTS_READ,
        }
    ),
    Role.AUDITOR: frozenset(
        {
            P.RULES_READ,
            P.MODELS_READ,
            P.TRANSACTIONS_READ,
            P.ALERTS_READ,
            P.CASES_READ,
            P.AUDIT_READ,
            P.REPORTS_READ,
        }
    ),
}

# Permissions that may be granted individually on top of a role (e.g. lead analyst).
GRANTABLE_PERMISSIONS: frozenset[Permission] = frozenset({P.ALERTS_ASSIGN_OTHERS})

# Roles that may receive each grantable permission — admins cannot grant themselves
# adjudication powers, and auditors stay read-only.
GRANTABLE_TO: dict[Permission, frozenset[Role]] = {
    P.ALERTS_ASSIGN_OTHERS: frozenset({Role.ANALYST}),
}
