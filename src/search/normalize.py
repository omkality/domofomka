import re
import unicodedata
from dataclasses import dataclass

from src.telemetry import measured

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


def ordinals(value: str, *, protect_street_numbers: bool = False) -> str:
    value = re.sub(r"\bтреть(?:я|е|его|ей|ю|ем|и|их)\b", "третий", value)
    roots = "|".join(sorted(ORDINALS, key=len, reverse=True))
    pattern = rf"\b(?:({'|'.join(TENS)})\s+)?({roots})(?:ый|ий|ой|ая|яя|ое|ее|ого|его|ой|ей|ую|юю|ом|ем|ых|их)\b"

    def house_role(position: int) -> bool:
        return bool(
            re.search(
                r"\b(?:дом|д|под[ъь]?езд|под|пд|квартира|кв|этаж|эт|корпус|корп|строение|стр)\.?\s*№?\s*$",
                value[:position],
            )
        )

    def number(match: re.Match) -> str:
        result = str(TENS.get(match[1], 0) + ORDINALS[match[2]])
        if protect_street_numbers and not house_role(match.start()):
            return f"__ordinal_{result}__"
        return result

    value = re.sub(pattern, number, value)
    if protect_street_numbers:
        value = re.sub(
            r"\b(\d+)\s*[- ]?\s*(?:ая|я|й|ый|ой)\b",
            lambda m: m[1] if house_role(m.start()) else f"__ordinal_{m[1]}__",
            value,
        )
        value = re.sub(
            r"\b(\d+)(\s+(?:января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря|года|лет|летия)\b)",
            r"__ordinal_\1__\2",
            value,
        )
    return value


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
    "россия",
    "ул",
    "улица",
    "федерация",
    "ш",
    "шоссе",
]
STOP.extend(
    [
        "ал",
        "туп",
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
        "аллея": "ал",
        "ал": "ал",
        "тупик": "туп",
        "туп": "туп",
    }.get(value, value)


@dataclass(frozen=True)
class Query:
    raw: str
    words: str
    house: str | None
    entrance: str | None
    kind: str | None = None


@measured("search.normalize")
def parse_query(value: str) -> Query:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    # Country is a structured prefix, while «Российская» can be a street name.
    value = re.sub(r"^\s*российская\s+федерация\b[\s,;]*", "", value)
    # Separate an attached house number, but preserve 62к1 / 4с2 components.
    value = re.sub(
        r"(?<=[а-яa-z])(?=\d)",
        lambda m: "" if m.start() >= 2 and value[m.start() - 1] in "кс" and value[m.start() - 2].isdigit() else " ",
        value,
    )
    value = re.sub(r"(?<=\d)(?=[а-яa-z]{3,}\b)", " ", value)
    value = re.sub(r"\b(\d+)(?=(?:марта|мая|года|лет)\b)", r"\1 ", value)
    value = ordinals(value, protect_street_numbers=True)
    markers = list(
        re.finditer(
            r"\b(?:улица|ул|проспект|просп|пр-кт|переулок|пер|проезд|пр-д|шоссе|бульвар|"
            r"набережная|площадь|микрорайон|аллея|ал|тупик|туп)\b",
            value,
        )
    )
    kind = street_kind(markers[-1][0]) if markers else None
    if markers:
        from src.search.localities import clean_structured_prefix

        value = clean_structured_prefix(value, markers[-1].start())
    entrance_match = re.search(r"(?:под[ъь]?езд|под[.]|пд[.]?)\s*№?\s*(\d+)", value)
    entrance = entrance_match[1] if entrance_match else None
    value = re.sub(r"(?:под[ъь]?езд|под[.]|пд[.]?)\s*№?\s*\d+", " ", value)
    value = re.sub(r"(?:квартира|кв[.]?|этаж|эт[.]?)\s*№?\s*\d+", " ", value)
    value = re.sub(r"(?<![а-яa-z])корп(?:ус)?[.,;\s]*(?=\d|[а-я](?![а-я]))", "к", value)
    value = re.sub(r"(?<![а-яa-z])стр(?:оение)?[.,;\s]*(?=\d|[а-я](?![а-я]))", "с", value)
    value = re.sub(r"(?<=\d)\s*[,;]\s*(?=[кс]\s*(?:\d|[а-я]\b))", " ", value)
    # Do not let a separated к/с become a house suffix and the component become a new house.
    value = re.sub(r"(?<=\d)\s*([кс])\s*(?=\d|[а-я](?![а-я]))", r"\1", value)
    # Last number is the building: numeric street names (1905 года, 1-я) stay in words.
    candidates = list(
        re.finditer(
            r"(?<![\w/])(?:(?:к|вл)\s*)?\d+(?:\s*-\s*\d+)?(?:\s*[а-яa-z](?=\s*/))?"
            r"(?:\s*/\s*\d+)?\s*[а-яa-z]?(?:\s*[кс]\s*(?:\d+[а-яa-z]?|[а-я])){0,2}(?!\w)",
            value,
        )
    )

    def unmark(words: str) -> str:
        return re.sub(r"__ordinal_(\d+)__", r"\1", words)

    if not candidates:
        return Query(unmark(value), address_words(unmark(value)), None, entrance, kind)
    match = candidates[-1]
    house = house_key(match[0])
    words = address_words(unmark(value[: match.start()] + " " + value[match.end() :]))
    return Query(unmark(value), words, house, entrance, kind)


def legacy_house_matches(message: str, address: str) -> bool:
    requested = parse_query(message).house
    returned = house_key(address.rsplit(", дом ", 1)[-1])
    if not requested or requested == returned:
        return True
    # Historical quarter/estate/locality encodings are opaque to the v2 parser.
    if not re.fullmatch(r"\d+(?:/\d+)?[а-яa-z]?(?:[кс](?:\d+[а-яa-z]?|[а-я])){0,2}", returned):
        return True
    presentation = message.casefold().replace("ё", "е").replace("корпус", "к").replace("строение", "с")
    presentation = re.sub(r"[\s,;-]+", "", presentation)
    return bool(re.search(r"(?<!\d)" + re.escape(returned) + r"(?![\w/])", presentation))
