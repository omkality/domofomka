"""Миграции на пустой/существующей БД, повторный запуск и атомарный отказ DDL."""

from concurrent.futures import ThreadPoolExecutor

import psycopg
import pytest
from psycopg.rows import dict_row
from yoyo import read_migrations

from src.infrastructure.database import migrate
from src.infrastructure.database.catalog import adopt_baseline, definition
from src.infrastructure.database.resources import migration_sql
from src.infrastructure.database.schema import require_schema


def test_queue_adoption_preserves_existing_records_and_sequences(isolated_database):
    with psycopg.connect(isolated_database) as conn:
        conn.execute(migration_sql("queue", "0001-queue"))
        conn.execute("""
            INSERT INTO bot_inbox(group_id,event_key,peer_id,kind,payload,occurred_at,created_at)
            VALUES (1,'saved',1,'message_new','{}',1,1)
        """)
        before = conn.execute("SELECT * FROM bot_inbox").fetchall()
    assert migrate.apply(isolated_database) == ["0001-queue", "0002-telemetry"]
    assert migrate.apply(isolated_database) == []
    with psycopg.connect(isolated_database, row_factory=dict_row) as conn:
        require_schema(conn, telemetry=True)
        assert [tuple(row.values()) for row in conn.execute("SELECT * FROM bot_inbox")] == before
        assert conn.execute("SELECT nextval('bot_inbox_id_seq') AS id").fetchone()["id"] == 2


def test_concurrent_migration_runners_apply_each_revision_once(isolated_database):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: migrate.apply(isolated_database), range(2)))
    assert sorted(m for result in results for m in result) == ["0001-queue", "0002-telemetry"]


def test_failed_ddl_rolls_back_the_whole_revision(isolated_database, tmp_path, monkeypatch):
    folder = tmp_path / "queue"
    folder.mkdir()
    (folder / "bad.sql").write_text("CREATE TABLE must_not_survive(id INT); SELECT invalid_column;", encoding="utf-8")
    monkeypatch.setattr(migrate, "ROOT", tmp_path)
    with pytest.raises(psycopg.errors.UndefinedColumn):
        migrate.apply(isolated_database)
    with psycopg.connect(isolated_database) as conn:
        assert conn.execute("SELECT to_regclass('public.must_not_survive')").fetchone()[0] is None
        assert conn.execute("SELECT count(*) FROM _yoyo_migration").fetchone()[0] == 0


@pytest.mark.parametrize("target", ["queue", "catalog"])
def test_explicit_schema_rollback_and_reapply(isolated_database, target):
    migrate.apply(isolated_database, target)
    migrations = read_migrations(str(migrate.ROOT / target))
    with migrate.connected(isolated_database) as backend, backend.lock():
        backend.rollback_migrations(backend.to_rollback(migrations))
    assert len(migrate.apply(isolated_database, target)) == 2


def test_catalog_adoption_checks_structure_and_preserves_codes(isolated_database):
    with psycopg.connect(isolated_database) as conn:
        conn.execute(migration_sql("catalog", "0001-catalog-tables"))
        conn.execute("INSERT INTO buildings(id) VALUES ('saved-building')")
        conn.execute("INSERT INTO codes VALUES ('saved-building','3','test','saved-code')")
        conn.execute(migration_sql("catalog", "0002-catalog-indexes"))
        conn.execute("INSERT INTO metadata VALUES ('catalog_schema_version','1'),('publication_state','ready')")
        before = definition(conn)
    adopt_baseline(isolated_database)
    assert migrate.apply(isolated_database, "catalog") == []
    with psycopg.connect(isolated_database) as conn:
        assert definition(conn) == before
        assert conn.execute("SELECT code FROM codes").fetchone()[0] == "saved-code"


def test_unknown_catalog_schema_is_never_silently_marked(isolated_database):
    with psycopg.connect(isolated_database) as conn:
        conn.execute(migration_sql("catalog", "0001-catalog-tables"))
        conn.execute(migration_sql("catalog", "0002-catalog-indexes"))
        conn.execute("INSERT INTO metadata VALUES ('catalog_schema_version','1'),('publication_state','ready')")
        conn.execute("ALTER TABLE buildings ADD COLUMN unexpected TEXT")
    with pytest.raises(RuntimeError, match="differs from the reviewed baseline"):
        adopt_baseline(isolated_database)


def test_runtime_refuses_an_unmigrated_database(isolated_database):
    with (
        psycopg.connect(isolated_database, row_factory=dict_row) as conn,
        pytest.raises(RuntimeError, match="migrations are required"),
    ):
        require_schema(conn)


def test_catalog_index_rollback_reapply_preserves_populated_codes(isolated_database):
    migrate.apply(isolated_database, "catalog", through="0001-catalog-tables")
    with psycopg.connect(isolated_database) as conn:
        conn.execute("INSERT INTO buildings(id) VALUES ('house')")
        conn.execute("INSERT INTO codes VALUES ('house','1','test','code')")
    migrate.apply(isolated_database, "catalog")
    migrations = read_migrations(str(migrate.ROOT / "catalog"))
    with migrate.connected(isolated_database) as backend, backend.lock():
        backend.rollback_migrations(backend.to_rollback(migrations)[:1])
    adopt_baseline(isolated_database)
    assert migrate.apply(isolated_database, "catalog") == ["0002-catalog-indexes"]
    with psycopg.connect(isolated_database) as conn:
        assert conn.execute("SELECT code FROM codes").fetchone()[0] == "code"
        assert conn.execute("SELECT count(*) FROM entrances").fetchone()[0] == 1
