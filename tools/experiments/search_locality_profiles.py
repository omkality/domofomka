"""Лабораторные ограничения явного населённого пункта; рабочий поиск не меняется."""

import re
from functools import lru_cache
from pathlib import Path

from src.code_service.v2.normalize import address_words, parse_query, text

LOCALITY_PREFIX = re.compile(r"^(?:город|г|пос[её]лок|пос|п|деревня|село|пгт|рп)(?:\.\s*|\s+)", re.I)
STREET_PREFIX = re.compile(
    r"\b(?:улица|ул|проспект|пр-кт|переулок|пер|проезд|пр-д|шоссе|бульвар|микрорайон|мкрн?)\b", re.I
)


def locality_hint(message, known, *, comma_prefix=False, require_street_field=False):
    first = message.strip().split(",", 1)[0]
    marker = LOCALITY_PREFIX.match(first)
    if marker:
        rest = first[marker.end() :]
        street = STREET_PREFIX.search(rest)
        rest = rest[: street.start()] if street else parse_query(rest).search_text
        value = address_words(rest)
        return value or None
    if comma_prefix and "," in message:
        rest = message.split(",", 1)[1].lstrip()
        if require_street_field and (STREET_PREFIX.search(first) or not STREET_PREFIX.match(rest)):
            return None
        value = address_words(first)
        if value and known(value):
            return value
    return None


@lru_cache(maxsize=1)
def _known_localities(path, mtime):
    # Проверяется поле населённого пункта, а не совпадение слова в названии улицы.
    from src.code_service.v2.engine import Catalog

    with Catalog(path).connect() as db:
        return frozenset(row[0] for row in db.execute("SELECT DISTINCT city_key FROM streets"))


def known_locality(catalog, value):
    return value in _known_localities(catalog.path, Path(catalog.path).stat().st_mtime_ns)


def locality_matches(hint, row, aliases=()):
    if not hint:
        return True
    required = set(text(hint).split())
    places = [row["city"] + " " + row.get("lineage", "")]
    source_field = row.get("display", "").removeprefix(row["city"] + ", ").rsplit(", дом ", 1)[0]
    if row.get("id", "").startswith("legacy:") and LOCALITY_PREFIX.match(source_field):
        # Непривязанный источник может хранить посёлок вместо улицы. Его варианты
        # остаются раздельными: фильтр не должен выбрать один, потеряв чужой набор.
        places.append(source_field)
    for alias in aliases:
        words, street = alias.get("words", ""), alias["street"]
        if street and words.endswith(street):
            places.append(words[: -len(street)].strip())
    return any(required <= set(text(place).split()) for place in places)
