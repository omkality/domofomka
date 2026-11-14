"""Проверка совместимости рабочей схемы без загрузки механизма миграций."""

from psycopg import AsyncConnection, Connection
from psycopg.rows import DictRow

EXISTS_SQL = """
SELECT
  to_regclass('public._yoyo_migration') AS name
"""

REVISIONS_SQL = """
SELECT
  migration_id
FROM
  _yoyo_migration
WHERE
  migration_id = any(%s)
"""


def _required(telemetry: bool) -> list[str]:
    """Выбрать обязательные ревизии очереди и включённой диагностики."""
    return ["0001-queue", *(["0002-telemetry"] if telemetry else [])]


def _validate(rows: list[DictRow], required: list[str]) -> None:
    """Отказать приложению, если обязательная миграция отсутствует."""
    if {row["migration_id"] for row in rows} != set(required):
        raise RuntimeError("Database schema does not match this bot version")


def require_schema(conn: Connection[DictRow], telemetry: bool = False) -> None:
    """Проверить ревизии в последовательных административных командах."""
    if not conn.execute(EXISTS_SQL).fetchone()["name"]:
        raise RuntimeError("Database migrations are required before starting the bot")
    required = _required(telemetry)
    _validate(conn.execute(REVISIONS_SQL, (required,)).fetchall(), required)


async def require_schema_async(conn: AsyncConnection[DictRow], telemetry: bool = False) -> None:
    """Проверить ту же схему асинхронного runtime без DDL и yoyo."""
    if not (await (await conn.execute(EXISTS_SQL)).fetchone())["name"]:
        raise RuntimeError("Database migrations are required before starting the bot")
    required = _required(telemetry)
    _validate(await (await conn.execute(REVISIONS_SQL, (required,))).fetchall(), required)
