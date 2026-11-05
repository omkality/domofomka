import re
import unicodedata
from dataclasses import dataclass

ORDINALS = {
    "перв": 1,
    "втор": 2,
    "трет": 3,
    "четверт": 4,
    "пят": 5,
    "шест": 6,
    "седьм": 7,
    "восьм": 8,
    "девят": 9,
    "десят": 10,
    "одиннадцат": 11,
    "двенадцат": 12,
    "тринадцат": 13,
    "четырнадцат": 14,
    "пятнадцат": 15,
    "шестнадцат": 16,
    "семнадцат": 17,
    "восемнадцат": 18,
    "девятнадцат": 19,
    "двадцат": 20,
    "тридцат": 30,
    "сороков": 40,
    "пятидесят": 50,
    "шестидесят": 60,
    "семидесят": 70,
    "восьмидесят": 80,
    "девяност": 90,
    "сот": 100,
}
TENS = {
    "двадцать": 20,
    "тридцать": 30,
    "сорок": 40,
    "пятьдесят": 50,
    "шестьдесят": 60,
    "семьдесят": 70,
    "восемьдесят": 80,
    "девяносто": 90,
}


def ordinals(value: str) -> str:
    roots = "|".join(sorted(ORDINALS, key=len, reverse=True))
    pattern = rf"\b(?:({'|'.join(TENS)})\s+)?({roots})(?:ый|ий|ой|ая|яя|ое|ее|ого|его|ой|ей|ую|юю|ом|ем|ых|их)\b"

    def number(match: re.Match) -> str:
        return str(TENS.get(match[1], 0) + ORDINALS[match[2]])

    return re.sub(pattern, number, value)


def text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = ordinals(value)
    value = re.sub(r"\bпр[\s.-]*кт\b", "проспект", value)
    value = re.sub(r"\bб[.-]р\b", "бульвар", value)
    value = re.sub(r"\bмск\b", "москва", value)
    value = re.sub(r"\b(?:спб|питер|санктпетербург)\b", "санкт петербург", value)
    value = re.sub(r"\b(\d+)\s*[- ]?\s*(?:ая|я|й|ый|ой)\b", r"\1", value)
    return re.sub(r"\s+", " ", re.sub(r"[^а-яa-z0-9/]+", " ", value)).strip()


def house_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = re.sub(r"корп(?:ус)?\.?", "к", value)
    value = re.sub(r"стр(?:оение)?\.?", "с", value)
    value = re.sub(r"\b(?:дом|д)\.?\s*", "", value)
    return re.sub(r"[^а-яa-z0-9/]+", "", value)


STOP = [
    "адрес",
    "адресу",
    "аллея",
    "б",
    "бульвар",
    "г",
    "город",
    "д",
    "дом",
    "домофон",
    "домофона",
    "код",
    "коды",
    "наб",
    "набережная",
    "нужен",
    "нужна",
    "обл",
    "область",
    "пер",
    "переулок",
    "пл",
    "площадь",
    "по",
    "подскажите",
    "пожалуйста",
    "пр",
    "пркт",
    "проезд",
    "просп",
    "проспект",
    "р",
    "российская",
    "россия",
    "ул",
    "улица",
    "федерация",
    "ш",
    "шоссе",
]
STOP.extend(
    [
        "квартал",
        "микрорайон",
        "мкр",
        "мкрн",
        "территория",
        "тер",
        "поселок",
        "деревня",
        "жилой",
        "комплекс",
        "массив",
        "военный",
        "городок",
        "тупик",
        "тракт",
    ]
)


def address_words(value: str) -> str:
    return " ".join(w for w in text(value).split() if w not in STOP)


def street_kind(value: str) -> str:
    value = re.sub(r"[^а-я]", "", value.casefold())
    return {
        "улица": "ул",
        "ул": "ул",
        "проспект": "пркт",
        "пркт": "пркт",
        "просп": "пркт",
        "переулок": "пер",
        "пер": "пер",
        "проезд": "проезд",
        "прд": "проезд",
        "шоссе": "ш",
        "ш": "ш",
        "бульвар": "б-р",
        "бр": "б-р",
        "набережная": "наб",
        "наб": "наб",
        "площадь": "пл",
        "пл": "пл",
        "микрорайон": "мкр",
        "мкр": "мкр",
    }.get(value, value)


@dataclass(frozen=True)
class Query:
    raw: str
    words: str
    house: str | None
    entrance: str | None
    kind: str | None = None


def parse_query(value: str) -> Query:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = ordinals(value)
    kinds = re.findall(
        r"\b(?:улица|ул|проспект|просп|пр-кт|переулок|пер|проезд|пр-д|шоссе|бульвар|набережная|площадь|микрорайон)\b",
        value,
    )
    kind = street_kind(kinds[-1]) if kinds else None
    entrance_match = re.search(r"(?:под[ъь]?езд|под[.]|пд[.]?)\s*№?\s*(\d+)", value)
    entrance = entrance_match[1] if entrance_match else None
    value = re.sub(r"(?:под[ъь]?езд|под[.]|пд[.]?)\s*№?\s*\d+", " ", value)
    value = re.sub(r"(?:квартира|кв[.]?|этаж|эт[.]?)\s*№?\s*\d+", " ", value)
    value = re.sub(r"корп(?:ус)?\.?\s*", "к", value)
    value = re.sub(r"стр(?:оение)?\.?\s*", "с", value)
    value = re.sub(r"(?<=\d)[,;]\s*(?=[кс]\s*\d)", " ", value)
    # Last number is the building: numeric street names (1905 года, 1-я) stay in words.
    candidates = list(re.finditer(r"(?<![\w/])\d+(?:/\d+)?\s*[а-яa-z]?(?:\s*[кс]\s*\d+[а-яa-z]?){0,2}(?!\w)", value))
    if not candidates:
        return Query(value, address_words(value), None, entrance, kind)
    match = candidates[-1]
    house = house_key(match[0])
    words = address_words(value[: match.start()] + " " + value[match.end() :])
    return Query(value, words, house, entrance, kind)
