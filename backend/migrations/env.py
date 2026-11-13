from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

import quiz_app.models  # noqa: F401  # model importによりAlembic metadataを登録する。
from quiz_app.config import get_settings

config = context.config
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    """DBへ接続せず、review用SQLを生成する。"""

    context.configure(
        url=get_settings().database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """明示commandからだけmigrationを適用する。"""

    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = get_settings().database_url
    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
