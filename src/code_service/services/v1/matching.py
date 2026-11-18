"""Неизменённые правила сопоставления адресов v1, без доступа к БД и настройкам."""

import json
import re
from collections.abc import Callable, Iterable
from importlib.resources import files

STREET_TYPES = tuple(json.loads(files(__package__).joinpath("russian.json").read_text(encoding="utf8"))["street_types"])
type Address = tuple[str, str, str, str]


def compact_address(value: str) -> str:
    """Применить прежние сокращения и разделители входного запроса v1."""
    return (
        value.lower()
        .replace("ё", "е")
        .replace(",", "")
        .replace(" дом ", " ")
        .replace("строение", "с")
        .replace("корпус", "к")
        .replace(" ", "")
        .replace("-", "")
    )


def select_addresses(
    message: str, addresses: Iterable[Address], accept_source: Callable[[str, str, str, str], bool] | None
) -> set[Address] | None:
    """Выбрать одну идентичность, используя полный адрес для разрешения коллизий."""
    groups: dict[Address, set[Address]] = {}
    for address in set(addresses):
        if accept_source is None or accept_source(*address):
            city, kind, street, house = address
            key = tuple(" ".join(value.lower().split()) for value in (city, kind, street, house))
            groups.setdefault(key, set()).add(address)
    if not groups:
        return set()

    query = compact_address(message)
    exact = []
    for (city, kind, street, house), group in groups.items():
        forms = (
            f"{city} {street} {house}",
            f"{city} {kind} {street} {house}",
            f"{city} {street} {kind} {house}",
            f"{street} {house} {city}",
            f"{kind} {street} {house} {city}",
            f"{street} {kind} {house} {city}",
        )
        if city.strip() and any(query == compact_address(form) for form in forms):
            exact.append(group)
    return exact[0] if len(exact) == 1 else None


def address_exists(msg: str, city: str, street: str, house: str, street_type: str) -> bool:
    """Проверить совместимость сообщения с полями исходного адреса v1."""
    msg = compact_address(msg)
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
    """Отобрать исходные строки по слову города или улицы."""
    return word in city.lower() or word in street.lower()
