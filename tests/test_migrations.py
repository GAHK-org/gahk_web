"""The Alembic migrations: they make the schema the models describe (like Django's
`makemigrations --check`, a model changed without a migration fails here), and they go down and up
again. The test database itself is made by them (conftest's engine fixture)."""

from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.runtime.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.pool import NullPool

from gahk_web import db
from gahk_web.config import DatabaseConfig
from gahk_web.models import Base

ROUND_TRIP_SCHEMA = "migration_round_trip"


def differences(engine: Engine) -> list[object]:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_server_default": True})
        return list(compare_metadata(context, Base.metadata))


def test_the_models_and_the_migrations_agree(engine: Engine) -> None:
    assert differences(engine) == [], "the models changed: write a migration (task migration -- '...')"


@pytest.fixture
def scratch_schema(db_config: DatabaseConfig) -> Iterator[Engine]:
    """An engine whose tables land in a schema of their own, away from the other tests' tables."""
    params = {**db.conninfo(db_config), "options": f"-c TimeZone=UTC -c search_path={ROUND_TRIP_SCHEMA}"}
    engine = create_engine(db.URL, poolclass=NullPool, connect_args=params)
    drop = text(f"DROP SCHEMA IF EXISTS {ROUND_TRIP_SCHEMA} CASCADE")
    with engine.begin() as connection:
        connection.execute(drop)
        connection.execute(text(f"CREATE SCHEMA {ROUND_TRIP_SCHEMA}"))
    yield engine
    with engine.begin() as connection:
        connection.execute(drop)
    engine.dispose()


def test_the_migrations_go_down_and_up_again(scratch_schema: Engine) -> None:
    engine = scratch_schema
    assert db.migrate(engine) == ["0001"]
    assert db.migrate(engine) == []  # nothing left to do
    with engine.connect() as connection:
        assert connection.execute(text("SELECT id FROM site_state")).scalars().all() == [1]

    config = db.alembic_config()
    with engine.connect() as connection:
        config.attributes["connection"] = connection
        command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]

    assert db.migrate(engine) == ["0001"]
    assert differences(engine) == []
