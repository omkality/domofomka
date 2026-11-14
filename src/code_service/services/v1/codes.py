"""Совместимое чтение исходных адресов и кодов."""

import asyncio
import logging
import re
import sqlite3
from collections.abc import Callable

import aiosqlite

from src.code_service.config import settings
from src.code_service.services.v1 import index as source_index
from src.code_service.services.v1.matching import STREET_TYPES, address_exists, street_or_city_exists
from src.infrastructure.telemetry import note, search_result, span

logger = logging.getLogger(__name__)


async def get_data_from_db(msg: str, *, accept_source: Callable[[str, str, str, str], bool] | None = None) -> dict:
    """Получить совместимый результат исходного поиска v1."""
    with span("search.v1", kind="search", engine="v1", query=(msg or "")[:2000]) as detail:
        if accept_source:
            note(v1_validation="source_identity")
        result = await _get_data_from_db(msg, accept_source=accept_source)
        detail["result"] = search_result(
            dict(result, status="found" if result.get("data") else "not_found", engine="v1")
        )
        return result


async def _get_data_from_db(msg: str, *, accept_source: Callable[[str, str, str, str], bool] | None = None) -> dict:
    """Найти исходные адресные строки через индекс либо резервное сканирование."""
    if msg:
        try:
            options = {"accept_source": accept_source} if accept_source else {}
            indexed = await asyncio.to_thread(
                source_index.lookup, msg, settings.db_name, settings.legacy_index_path, **options
            )
            if indexed is not None:
                note(v1_backend="index")
                return indexed
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            logger.warning("Legacy candidate index unavailable; using source scan")
    note(v1_backend="source_scan")
    with span("search.v1.source_scan"):
        return await scan_data_from_db(msg, **({"accept_source": accept_source} if accept_source else {}))


async def scan_data_from_db(msg: str, *, accept_source: Callable[[str, str, str, str], bool] | None = None) -> dict:
    """Выполнить исходное чтение SQLite с прежними правилами совпадения адреса."""
    result: dict = {}

    if not msg:
        return result

    msg_array = re.split(r"[^а-я]", msg)

    for street_type in STREET_TYPES:
        if street_type in msg_array:
            msg_array.remove(street_type)

    if not msg_array:
        return result

    longest_word = max(msg_array, key=len).lower()

    try:
        async with aiosqlite.connect(settings.db_name) as connection:
            await connection.create_function("street_or_city_exists", 3, street_or_city_exists)

            query = r"""
                SELECT *
                FROM codes
                WHERE street_or_city_exists(?, city, street)
                """

            data = []
            connection.row_factory = aiosqlite.Row

            async with connection.execute(query, (longest_word,)) as cursor:
                async for row in cursor:
                    if address_exists(msg, row["city"], row["street"], row["house"], row["street_type"]):
                        data.append(row)

            if data:
                shortest_city = data[0][1]
                for _id, city, *_ in data:
                    if len(city) < len(shortest_city):
                        shortest_city = city

                checked = {}
                if accept_source:
                    first = next(row for row in data if shortest_city in row["city"])
                    if not accept_source(shortest_city, first["street_type"], first["street"], first["house"]):
                        return {}

                for _id, city, street_type, street, house, entrance, code_type, code in data:
                    if shortest_city in city:
                        if accept_source:
                            identity = (city, street_type, street, house)
                            if identity not in checked:
                                checked[identity] = accept_source(city, street_type, street, house)
                            if not checked[identity]:
                                continue
                        result.setdefault("address", f"{shortest_city}, {street_type} {street}, дом {house}")
                        result.setdefault("data", {})
                        result["data"].setdefault(entrance, []).append((code, code_type))

        return result

    except Exception:
        logger.exception("Legacy SQLite search failed")
        raise
