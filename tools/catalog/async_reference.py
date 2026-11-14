"""Офлайн-адаптер эталона для сравнения асинхронных сценариев API без сервера."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from tools.catalog.sqlite_catalog import SqliteCatalog


class AsyncReferenceReader:
    """Воспроизвести сценарий поиска на синхронном эталоне только в tools/tests."""

    def __init__(self, reader: SqliteCatalog) -> None:
        """Сохранить эталон с собственными короткоживущими SQL-соединениями."""
        self.reader = reader

    @asynccontextmanager
    async def connect(self) -> AsyncIterator[None]:
        """Сохранить границу сценария; каждый файловый метод сам закрывает свою БД."""
        yield

    async def resolve(self, message: str, candidates: list[dict] | None = None) -> dict:
        """Выполнить эталонный поиск вне цикла событий проверяющего инструмента."""
        return await asyncio.to_thread(self.reader.resolve, message, candidates)

    async def load_candidates(self, ids: list[str], house: str) -> list[dict]:
        """Прочитать кандидатов офлайн-индекса для сравнительного прогона."""
        return await asyncio.to_thread(self.reader.load_candidates, ids, house)

    async def building(self, identity: str) -> dict | None:
        """Прочитать эталонный дом без переноса SQLite в рабочий API."""
        return await asyncio.to_thread(self.reader.building, identity)

    async def answer(self, building: dict, entrance: str | None = None) -> dict:
        """Прочитать коды для проверки того же формата ответа."""
        return await asyncio.to_thread(self.reader.answer, building, entrance)
