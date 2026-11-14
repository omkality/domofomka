"""Неизменённые правила сопоставления адресов v1, без доступа к БД и настройкам."""

import json
import re
from importlib.resources import files

STREET_TYPES = tuple(json.loads(files(__package__).joinpath("russian.json").read_text(encoding="utf8"))["street_types"])


def address_exists(msg: str, city: str, street: str, house: str, street_type: str) -> bool:
    """Проверить совместимость сообщения с полями исходного адреса v1."""
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
    """Отобрать исходные строки по слову города или улицы."""
    return word in city.lower() or word in street.lower()
