"""Local migration rehearsal on an isolated old-shape SQLite database.

This complements, but does not replace, a PostgreSQL restore rehearsal before deployment.
"""

import importlib.util
from io import StringIO
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_postgresql_migration_compiles_without_a_live_connection():
    path = Path(__file__).parents[1] / "migrations/versions/20260924_0004_training_system.py"
    spec = importlib.util.spec_from_file_location("training_migration_sql", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        migration.upgrade()
    sql = output.getvalue()
    assert "CREATE TABLE catalog_protocols" in sql
    assert "ALTER TABLE workouts ADD COLUMN plan_id INTEGER" in sql
    assert "UNIQUE (dedupe_key)" in sql
    assert "DROP TABLE" not in sql


def test_additive_migration_preserves_records_and_enables_scheduled_outbox(tmp_path):
    path = Path(__file__).parents[1] / "migrations/versions/20260924_0004_training_system.py"
    spec = importlib.util.spec_from_file_location("training_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'migration.sqlite3'}")
    old = sa.MetaData()
    sa.Table("athletes", old, sa.Column("id", sa.Integer(), primary_key=True))
    sa.Table("messages", old, sa.Column("id", sa.Integer(), primary_key=True))
    for name in ("plans", "workouts"):
        sa.Table(
            name,
            old,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("athlete_id", sa.Integer(), sa.ForeignKey("athletes.id")),
            sa.Column("content", sa.JSON()),
        )
    sa.Table(
        "outbox",
        old,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_message_id", sa.Integer(), sa.ForeignKey("messages.id"), nullable=False),
        sa.Column("reply_to_message_id", sa.BigInteger(), nullable=False),
    )
    old.create_all(engine)
    with engine.begin() as connection:
        connection.execute(old.tables["athletes"].insert().values(id=1))
        connection.execute(old.tables["messages"].insert().values(id=1))
        connection.execute(
            old.tables["plans"]
            .insert()
            .values(id=1, athlete_id=1, content={"summary": "existing plan"})
        )
        connection.execute(
            old.tables["workouts"]
            .insert()
            .values(id=1, athlete_id=1, content={"actual": "do not change"})
        )
        connection.execute(
            old.tables["outbox"].insert().values(id=1, source_message_id=1, reply_to_message_id=99)
        )
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        inspector = sa.inspect(connection)
        assert {
            "training_preferences",
            "invitations",
            "catalog_protocols",
            "catalog_sources",
        } <= set(inspector.get_table_names())
        assert (
            connection.execute(sa.text("select scope from plans where id=1")).scalar_one() == "week"
        )
        assert (
            "do not change"
            in connection.execute(sa.text("select content from workouts where id=1")).scalar_one()
        )
        connection.execute(
            sa.text(
                "insert into outbox(id, source_message_id, reply_to_message_id, dedupe_key) "
                "values (2, NULL, NULL, 'morning:test')"
            )
        )
        assert connection.execute(sa.text("select count(*) from outbox")).scalar_one() == 2
    engine.dispose()
