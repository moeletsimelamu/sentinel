"""Operational commands: ``python -m sentinel.cli <command>``.

seed-demo-users
    Creates one demo user per role (plus a lead analyst). The password is read from
    ``SENTINEL_DEMO_PASSWORD``; the command refuses to run when ``SENTINEL_ENV=production``.
    Idempotent: existing users are left untouched.
"""

from __future__ import annotations

import argparse
import os
import sys

from sentinel.core.config import get_settings
from sentinel.core.db import get_session_factory
from sentinel.core.permissions import Permission, Role
from sentinel.core.security import hash_password
from sentinel.modules.audit import service as audit
from sentinel.modules.audit.context import Actor, RequestContext
from sentinel.modules.auth import repository as repo
from sentinel.modules.auth.models import User, UserPermissionGrant
from sentinel.modules.auth.service import validate_password_policy

DEMO_USERS: list[tuple[str, str, Role, tuple[Permission, ...]]] = [
    ("admin@sentinel.local", "Ada Administrator", Role.ADMIN, ()),
    ("analyst@sentinel.local", "Ari Analyst", Role.ANALYST, ()),
    (
        "lead.analyst@sentinel.local",
        "Lee Lead-Analyst",
        Role.ANALYST,
        (Permission.ALERTS_ASSIGN_OTHERS,),
    ),
    ("auditor@sentinel.local", "Avery Auditor", Role.AUDITOR, ()),
]


def seed_demo_users() -> int:
    settings = get_settings()
    if settings.env == "production":
        print("Refusing to seed demo users in production.", file=sys.stderr)  # noqa: T201
        return 2
    password = os.environ.get("SENTINEL_DEMO_PASSWORD", "")
    if not password:
        print("Set SENTINEL_DEMO_PASSWORD (see .env.example).", file=sys.stderr)  # noqa: T201
        return 2

    created = 0
    with get_session_factory()() as db:
        for email, name, role_name, grants in DEMO_USERS:
            if repo.get_user_by_email(db, email) is not None:
                continue
            validate_password_policy(settings, password, email)
            role = repo.get_role_by_name(db, role_name.value)
            if role is None:
                print("Roles missing — run `alembic upgrade head` first.", file=sys.stderr)  # noqa: T201
                return 1
            user = User(
                email=email,
                full_name=name,
                password_hash=hash_password(password),
                role=role,
                role_id=role.id,
            )
            db.add(user)
            db.flush()
            for perm in grants:
                user.grants.append(UserPermissionGrant(user_id=user.id, permission=perm.value))
            audit.record(
                db,
                actor=Actor.system(),
                ctx=RequestContext.offline(),
                action="user.seeded",
                entity_type="user",
                entity_id=user.id,
                after={
                    "email": email,
                    "role": role_name.value,
                    "grants": [g.value for g in grants],
                },
            )
            created += 1
        db.commit()
    print(f"Demo users ready ({created} created, {len(DEMO_USERS) - created} already existed).")  # noqa: T201
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sentinel")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("seed-demo-users", help="Create demo admin/analyst/auditor accounts")
    args = parser.parse_args(argv)
    if args.command == "seed-demo-users":
        return seed_demo_users()
    return 1  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
