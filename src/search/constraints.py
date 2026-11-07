"""Address identity checks shared by catalog and OpenSearch ranking."""

import re
from collections.abc import Iterable
from dataclasses import replace

from src.search.normalize import (
    NAME_LABELS,
    STOP,
    Query,
    address_words,
    building_kind_matches,
    legacy_house_matches,
    parse_query,
    street_kind,
    text,
)

LITERAL_NAME_NOISE = frozenset(word for word in set(STOP) - NAME_LABELS if len(word) > 1) | {"им", "имени"}
NAME_KINDS = frozenset(street_kind(word) for word in NAME_LABELS)


def has_misaligned_source_fields(row: dict) -> bool:
    """Не выдавать непроверенную улицу из поля города за адрес v2."""
    return (
        not row["guid"]
        and not row.get("id", "").startswith("address:")
        and bool(
            re.search(
                r"\b(?:ул|улица|проспект|пр-кт|шоссе|бульвар|набережная|переулок|проезд)\b", row["city"], re.I
            )
        )
    )


def literal_name_tokens(value: str) -> frozenset[str]:
    return frozenset(
        street_kind(word) if word in NAME_LABELS else word
        for word in text(value).split()
        if word not in LITERAL_NAME_NOISE
    )


def street_name_keys(row: dict, street: str | None = None) -> set[frozenset[str]]:
    """Сравнить полное имя с типом элемента, сохраняя инициалы и значимые слова."""
    name = literal_name_tokens(row["street"] if street is None else street)
    prefix = row["display"].removeprefix(row["city"] + ", ").rsplit(", дом ", 1)[0]
    own_type = prefix.removesuffix(row["street"])
    kind = street_kind(own_type)
    labels = frozenset([kind]) if kind in NAME_KINDS else literal_name_tokens(own_type)
    return {name, name | labels} - {frozenset()}


def complete_name_match(query: Query, row: dict, aliases: Iterable[dict] = ()) -> bool:
    tokens = literal_name_tokens(query.name_words or query.words)
    names = [(row["city"], row["street"])]
    for alias in aliases:
        street = alias["street"]
        words = alias.get("words", "")
        names.append((words.removesuffix(street).strip() if words.endswith(street) else "", street))
    for city, street in names:
        for name in street_name_keys(row, street):
            if tokens - (literal_name_tokens(city) - name) != name:
                continue
            # A word can be noise in free text and still belong to the stored
            # street name. Do not call a partial name complete after dropping it.
            required = {
                street_kind(word)
                for word in text(street).split()
                if word in LITERAL_NAME_NOISE and word not in {"им", "имени"}
            }
            if not required or required <= {
                street_kind(word) for word in text(query.search_text or query.raw).split()
            }:
                return True
    return False


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
    # B/M and initials belong to the name even when query cleanup treats them
    # as noise. They cannot make a partial street look like its complete name.
    street = set(text(row["street"]).split()) - {"им", "имени"}
    return tokens in [
        street,
        set(address_words(row["city"]).split()) | street,
        *(set(text(alias).split()) for alias in row.get("_aliases", [])),
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


def locality_prefix_match(query: Query, row: dict) -> frozenset[str]:
    """Полная улица и сокращённый город позволяют уточнение, но не автоматический выбор."""
    tokens = literal_name_tokens(query.name_words or query.words)
    city = literal_name_tokens(row["city"])
    for street in street_name_keys(row):
        remaining = tokens - city - street
        if not remaining or not all(
            word.isalpha() and len(word) >= 4 and any(full.startswith(word) and full != word for full in city)
            for word in remaining
        ):
            continue
        expanded = tokens - remaining | {full for full in city if any(full.startswith(word) for word in remaining)}
        alternate = replace(query, words=" ".join(expanded), name_words=" ".join(expanded))
        if complete_name_match(alternate, row):
            return frozenset(remaining)
    return frozenset()


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

def legacy_source_matches(
    message: str, city: str, kind: str, street: str, house: str, *, whole_location: bool = False
) -> bool:
    """Проверять каждую исходную группу, прежде чем включать её коды в fallback v2."""
    address = f"{city}, {kind} {street}, дом {house}"
    if not legacy_result_matches(message, address, whole_location=whole_location):
        return False
    query, document = parse_query(message), parse_query(address)
    names = {word for word in document.words.split() if word.isalpha()}
    names.update(word for word in text(kind).split() if word.isalpha())
    words = [word for word in query.words.split() if word.isalpha()]
    if set(words) <= names:
        return True
    # Accept missing/extra spaces only when all letters partition into complete
    # words of this actual source. A city prefix is not a complete locality.
    # Digit/house identity remains the responsibility of legacy_result_matches.
    letters = "".join(words)
    positions = {0}
    for start in range(len(letters)):
        if start in positions:
            positions.update(start + len(word) for word in names if letters.startswith(word, start))
    return len(letters) in positions
