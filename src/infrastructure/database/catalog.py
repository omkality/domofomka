"""Принятие опубликованного каталога под контроль миграций без перезагрузки данных."""

import json
from pathlib import Path

import psycopg

from src.infrastructure.database.migrate import ROOT, connected
from src.infrastructure.network import network_settings

TABLES = [
    "aliases",
    "alias_search",
    "buildings",
    "codes",
    "code_sources",
    "entrances",
    "metadata",
    "provenance",
    "streets",
    "street_houses",
    "vocabulary",
]


def definition(conn: psycopg.Connection) -> dict:
    """Получить структуру таблиц, ограничений и индексов без чтения адресов или кодов."""
    result = {}
    for section, query in [
        (
            "columns",
            r"""
        SELECT TABLE_NAME,
               COLUMN_NAME,
               data_type,
               is_nullable,
               column_default
        FROM information_schema.columns
        WHERE table_schema = 'public'
            AND TABLE_NAME = ANY(%s)
        ORDER BY TABLE_NAME,
                 ordinal_position
        """,
        ),
        (
            "constraints",
            r"""
        SELECT c.relname,
               pg_get_constraintdef(k.oid)
        FROM pg_constraint AS k
        JOIN pg_class AS c ON c.oid = k.conrelid
        JOIN pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = 'public'
            AND c.relname = ANY(%s)
        ORDER BY c.relname,
                 pg_get_constraintdef(k.oid)
        """,
        ),
        (
            "indexes",
            r"""
        SELECT tablename,
               indexname,
               indexdef
        FROM pg_indexes
        WHERE schemaname = 'public'
            AND tablename = ANY(%s)
        ORDER BY tablename,
                 indexname
        """,
        ),
    ]:
        result[section] = [list(row) for row in conn.execute(query, (TABLES,)).fetchall()]
    return result


def adopt_baseline(dsn: str) -> None:
    """Отметить базовые миграции лишь при точном совпадении опубликованной схемы."""
    from yoyo import read_migrations

    expected = json.loads((Path(__file__).parent / "catalog-baseline.json").read_text(encoding="utf-8"))
    with psycopg.connect(dsn, connect_timeout=network_settings.queue_db_connect_timeout_seconds) as conn:
        conn.execute("SET TRANSACTION READ ONLY")
        tracked = conn.execute("SELECT to_regclass('public._yoyo_migration')").fetchone()[0]
        if tracked:
            applied = {row[0] for row in conn.execute("SELECT migration_id FROM _yoyo_migration")}
            if "0001-catalog-tables" in applied:
                return
        if definition(conn) != expected:
            raise RuntimeError("Catalog differs from the reviewed baseline; automatic adoption is forbidden")
        metadata = dict(conn.execute(r"""
            SELECT KEY,
                   value
            FROM metadata
            WHERE KEY IN ('publication_state',
                          'catalog_schema_version')
            """))
        if metadata != {"publication_state": "ready", "catalog_schema_version": "1"}:
            raise RuntimeError("Only a published schema version 1 can be adopted")
    migrations = read_migrations(str(ROOT / "catalog"))
    baseline = migrations.__class__([m for m in migrations if m.id in {"0001-catalog-tables", "0002-catalog-indexes"}])
    with connected(dsn) as backend, backend.lock(timeout=network_settings.queue_db_connect_timeout_seconds):
        backend.mark_migrations(backend.to_apply(baseline))
