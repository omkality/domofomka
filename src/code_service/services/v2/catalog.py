"""Создание и завершение конкретных читателей каталога."""

import asyncio

from src.code_service.config import settings
from src.code_service.services.v2.postgres import PostgresCatalog

CATALOG_BACKEND = "postgres"
_postgres_readers: dict = {}
_postgres_guards: dict = {}


async def close_catalog() -> None:
    """Закрыть текущий читатель каталога при остановке API."""
    loop = asyncio.get_running_loop()
    guard = _postgres_guards.get(loop)
    if guard is not None:
        async with guard:
            cached = _postgres_readers.pop(loop, None)
            if cached is not None:
                await cached[1].close()
        _postgres_guards.pop(loop, None)


async def get_catalog() -> PostgresCatalog:
    """Вернуть PostgreSQL-читатель опубликованной ревизии рабочего v2."""
    if not settings.catalog_database_url:
        raise ValueError("CATALOG_DATABASE_URL required for PostgreSQL catalog")
    config = (
        settings.catalog_database_url.get_secret_value(),
        settings.search_min_similarity,
        settings.search_ambiguity_margin,
        settings.catalog_connect_timeout_seconds,
        settings.catalog_statement_timeout_ms,
        settings.catalog_lock_timeout_seconds,
        settings.catalog_operation_timeout_seconds,
        settings.catalog_revision,
        settings.search_candidate_limit,
    )
    loop = asyncio.get_running_loop()
    guard = _postgres_guards.setdefault(loop, asyncio.Lock())
    async with guard:
        cached = _postgres_readers.get(loop)
        if cached is None or cached[0] != config:
            if cached is not None:
                await cached[1].close()
            reader = PostgresCatalog(
                *config[:3],
                connect_timeout_seconds=config[3],
                statement_timeout_ms=config[4],
                lock_timeout_seconds=config[5],
                operation_timeout_seconds=config[6],
                revision=config[7],
                candidate_limit=config[8],
            )
            _postgres_readers[loop] = (config, reader)
        return _postgres_readers[loop][1]
