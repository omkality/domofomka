"""Чтение поставляемых вместе с приложением SQL-ресурсов."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent


def read_sql(name: str) -> str:
    """Прочитать именованную схему без обращения к рабочему каталогу процесса."""
    return (ROOT / "schemas" / name).read_text(encoding="utf-8")


def migration_sql(target: str, revision: str) -> str:
    """Прочитать SQL одной неизменяемой ревизии миграции."""
    return (ROOT / "migrations" / target / (revision + ".sql")).read_text(encoding="utf-8")
