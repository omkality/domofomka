"""Рабочий v2 всегда выбирает PostgreSQL; SQLite остаётся офлайн-эталоном."""

from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from src.code_service.config import Settings, settings
from src.code_service.services.v2.catalog import CATALOG_BACKEND, close_catalog, get_catalog
from src.code_service.services.v2.postgres import PostgresCatalog


@pytest.mark.asyncio
async def test_postgres_factory_has_no_sqlite_configuration_or_connection_attempt(monkeypatch):
    monkeypatch.setattr(settings, "catalog_database_url", SecretStr("postgresql://unused@127.0.0.1:1/catalog"))
    monkeypatch.setattr(settings, "search_candidate_limit", 5)
    connect = AsyncMock(side_effect=AssertionError("Constructing a reader must not connect"))
    monkeypatch.setattr("psycopg.AsyncConnection.connect", connect)
    try:
        reader = await get_catalog()
        assert isinstance(reader, PostgresCatalog)
        assert reader.candidate_limit == 5
        assert CATALOG_BACKEND == "postgres"
        assert "search_backend" not in Settings.model_fields
        assert "search_catalog_path" not in Settings.model_fields
        connect.assert_not_awaited()
    finally:
        await close_catalog()


@pytest.mark.asyncio
async def test_missing_postgres_url_does_not_select_a_file_reader(monkeypatch):
    monkeypatch.setattr(settings, "catalog_database_url", None)
    with pytest.raises(ValueError, match="CATALOG_DATABASE_URL"):
        await get_catalog()
