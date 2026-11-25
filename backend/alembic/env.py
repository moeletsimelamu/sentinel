from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from sentinel.core.config import get_settings
from sentinel.core.db import Base
from sentinel.modules.audit import models as _audit_models  # noqa: F401 — register tables
from sentinel.modules.auth import models as _auth_models  # noqa: F401
from sentinel.modules.ingestion import models as _ingestion_models  # noqa: F401
from sentinel.modules.jobs import models as _jobs_models  # noqa: F401
from sentinel.modules.rules import models as _rules_models  # noqa: F401
from sentinel.modules.scoring import models as _scoring_models  # noqa: F401
from sentinel.modules.transactions import models as _transactions_models  # noqa: F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# An explicitly supplied URL (e.g. by the test suite) wins over the environment.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
