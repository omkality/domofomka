"""Address identity checks shared by catalog and OpenSearch ranking."""

import re
from collections.abc import Iterable

from src.search.normalize import (
    NAME_LABELS,
    Query,
    address_words,
    building_kind_matches,
    legacy_house_matches,
    parse_query,
    street_kind,
    text,
)


def name_constraints_match(query: Query, row: dict, aliases: Iterable[str] = ()) -> bool:
    """A removed name label must still be explained by this exact candidate."""
    required = {street_kind(word) for word in query.name_words.split() if word in NAME_LABELS}
    if not required:
        return True
    # Include the explicit address-element type. A user can write either
    # «проспект Мира» or «улица Большая Набережная» without losing name words.
    prefix = row.get("display", "").removeprefix(row.get("city", "") + ", ").rsplit(", дом ", 1)[0]
    own_type = prefix.removesuffix(row["street"]).strip(" ,")
    city_names = set(address_words(row.get("city", "").rsplit(",", 1)[-1]).split()) - {"дачный", "рабочий"}
    if city_names and city_names <= set(query.name_words.split()):
        required -= {street_kind(word) for word in text(row["city"]).split() if word in NAME_LABELS}
    documents = [
        row["street"],
        *aliases,
        *row.get("_alias_streets", []),
    ]
    return any(
        required <= ({street_kind(word) for word in text(doc + " " + own_type).split()} | {street_kind(own_type)})
        for doc in documents
    )


def full_name_match(query: Query, row: dict) -> bool:
    tokens = set(query.words.split())
    return tokens in [
        set(address_words(row["street"]).split()),
        set(address_words(row["city"] + " " + row["street"]).split()),
        *(set(address_words(alias).split()) for alias in row.get("_aliases", [])),
    ]


def whole_location_match(query: Query, city: str, street: str) -> bool:
    """Only an explicit complete locality plus street establishes this scope."""
    city_words, street_words = set(address_words(city).split()), set(address_words(street).split())
    if any(char.isalpha() and not re.fullmatch("[а-яa-z]", char) for char in query.search_text):
        return False
    return (
        any(word.isalpha() and len(word) >= 3 for word in city_words - street_words)
        and set(query.words.split()) == city_words | street_words
    )


def legacy_result_matches(message: str, address: str, *, whole_location: bool = False) -> bool:
    if not legacy_house_matches(message, address):
        return False
    prefix, separator, house = address.rpartition(", дом ")
    if not separator:
        return not whole_location
    match = re.search(
        r", (улица|ул\.|проспект|пр-кт|переулок|пер\.|проезд|пр-д|шоссе|бульвар|б-р|"
        r"набережная|наб\.|квартал|микрорайон|мкр\.|жилой комплекс|жилой массив|территория|"
        r"деревня|поселок|военный городок|аллея|площадь|тракт|тупик) (.+)$",
        prefix,
        re.I,
    )
    if not match:
        return not whole_location
    query = parse_query(message)
    row = {"city": prefix[: match.start()], "street": match[2], "house": house, "display": address}
    return (
        name_constraints_match(query, row)
        and building_kind_matches(query, row)
        and (not whole_location or whole_location_match(query, row["city"], row["street"]))
    )
