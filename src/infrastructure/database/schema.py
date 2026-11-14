"""Проверка совместимости рабочей схемы без загрузки механизма миграций."""

from psycopg import Connection
from psycopg.rows import DictRow


def require_schema(conn: Connection[DictRow], telemetry: bool = False) -> None:
    """Проверить ревизию без DDL и записи служебных таблиц yoyo."""
    exists = conn.execute(r"""
        SELECT
          to_regclass('public._yoyo_migration') AS name
        """).fetchone()["name"]
    if not exists:
        raise RuntimeError("Database migrations are required before starting the bot")
    required = ["0001-queue", *(["0002-telemetry"] if telemetry else [])]
    rows = conn.execute(
        r"""
        SELECT
          migration_id
        FROM
          _yoyo_migration
        WHERE
          migration_id = any(%s)
        """,
        (required,),
    ).fetchall()
    if {row["migration_id"] for row in rows} != set(required):
        raise RuntimeError("Database schema does not match this bot version")
