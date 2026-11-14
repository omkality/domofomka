"""Создание и завершение конкретных читателей каталога."""

import threading

from src.code_service.config import settings
from src.code_service.services.v2.engine import Catalog

_postgres_readers: dict = {}
_postgres_guard = threading.Lock()


def close_catalog() -> None:
    """Закрыть текущий читатель каталога при остановке API."""
    with _postgres_guard:
        for reader in _postgres_readers.values():
            reader.close()
        _postgres_readers.clear()


def get_catalog() -> Catalog:
    """Вернуть читатель опубликованной ревизии выбранного хранилища."""
    if settings.search_backend == "sqlite":
        return Catalog(
            settings.search_catalog_path,
            settings.search_min_similarity,
            settings.search_ambiguity_margin,
            candidate_limit=settings.search_candidate_limit,
        )
    if not settings.catalog_database_url:
        raise ValueError("CATALOG_DATABASE_URL required for PostgreSQL catalog")
    from src.code_service.services.v2.postgres import PostgresCatalog

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
    with _postgres_guard:
        if config not in _postgres_readers:
            for reader in _postgres_readers.values():
                reader.close()
            _postgres_readers.clear()
            _postgres_readers[config] = PostgresCatalog(
                *config[:3],
                connect_timeout_seconds=config[3],
                statement_timeout_ms=config[4],
                lock_timeout_seconds=config[5],
                operation_timeout_seconds=config[6],
                revision=config[7],
                candidate_limit=config[8],
            )
        return _postgres_readers[config]
