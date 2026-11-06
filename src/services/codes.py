import asyncio
import logging
import re
import sqlite3
import traceback

import aiosqlite

from src.api.config import settings
from src.telemetry import note, search_result, span

logger = logging.getLogger(__name__)

street_types = [
    "улица",
    "проспект",
    "микрорайон",
    "переулок",
    "жилой комплекс",
    "бульвар",
    "тракт",
    "поселок",
    "проезд",
    "шоссе",
    "аллея",
    "площадь",
    "набережная",
    "квартал",
    "территория",
    "деревня",
    "военный городок",
    "жилой массив",
    "тупик",
]


def address_exists(msg: str, city: str, street: str, house: str, street_type: str) -> bool:
    msg = (
        msg.lower()
        .replace("ё", "е")
        .replace(",", "")
        .replace(" дом ", " ")
        .replace("строение", "с")
        .replace("корпус", "к")
        .replace(" ", "")
        .replace("-", "")
    )

    city = re.sub(r"\W", "", city).lower()
    street = street.lower().replace(",", "").replace("-", "").replace("  ", " ")
    street_split = street.split()
    house = house.lower()

    if all(word in msg for word in street_split):
        for word in street_split:
            msg = msg.replace(word, "", 1)

        if msg.count(house) == 1:
            msg = msg.replace(house, "")

            if msg:
                if street_type in msg:
                    msg = msg.replace(street_type, "")

                res = True
                for remaining_word in msg.split():
                    if remaining_word not in city or len(remaining_word) < 4:
                        res = False
                        break
            else:
                res = True
        else:
            res = False
    else:
        res = False

    return res


def street_or_city_exists(word: str, city: str, street: str) -> bool:
    return word in city.lower() or word in street.lower()


async def get_data_from_db(msg: str) -> dict:
    with span("search.v1", kind="search", engine="v1", query=(msg or "")[:2000]) as detail:
        result = await _get_data_from_db(msg)
        detail["result"] = search_result(
            dict(result, status="found" if result.get("data") else "not_found", engine="v1")
        )
        return result


async def _get_data_from_db(msg: str) -> dict:
    if msg:
        from src.services.legacy_index import lookup

        try:
            indexed = await asyncio.to_thread(lookup, msg, settings.db_name, settings.legacy_index_path)
            if indexed is not None:
                note(v1_backend="index")
                return indexed
        except (OSError, sqlite3.Error, ValueError, TypeError, KeyError):
            logger.warning("Legacy candidate index unavailable; using source scan")
    note(v1_backend="source_scan")
    with span("search.v1.source_scan"):
        return await scan_data_from_db(msg)


async def scan_data_from_db(msg: str) -> dict:
    result: dict = {}

    if not msg:
        return result

    msg_array = re.split(r"[^а-я]", msg)

    for street_type in street_types:
        if street_type in msg_array:
            msg_array.remove(street_type)

    if not msg_array:
        return result

    longest_word = max(msg_array, key=len).lower()

    try:
        async with aiosqlite.connect(settings.db_name) as connection:
            await connection.create_function("street_or_city_exists", 3, street_or_city_exists)

            query = f"""
                SELECT *
                FROM codes
                WHERE street_or_city_exists('{longest_word}', city, street)
            """

            data = []
            connection.row_factory = aiosqlite.Row

            async with connection.execute(query) as cursor:
                async for row in cursor:
                    if address_exists(
                        msg,
                        row["city"],
                        row["street"],
                        row["house"],
                        row["street_type"],
                    ):
                        data.append(row)

            if data:
                shortest_city = data[0][1]
                for _id, city, *_ in data:
                    if len(city) < len(shortest_city):
                        shortest_city = city

                for (
                    _id,
                    city,
                    street_type,
                    street,
                    house,
                    entrance,
                    code_type,
                    code,
                ) in data:
                    if shortest_city in city:
                        result.setdefault(
                            "address",
                            f"{shortest_city}, {street_type} {street}, дом {house}",
                        )
                        result.setdefault("data", {})
                        result["data"].setdefault(entrance, []).append((code, code_type))

        return result

    except Exception:
        logger.error(f"Database error: {traceback.format_exc()}")
        raise
