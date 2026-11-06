import re
import unicodedata
from dataclasses import dataclass

from src.telemetry import measured

DASHES = str.maketrans(dict.fromkeys("‐‑‒–—−", "-"))
HOUSE_LETTER = r"[а-яa-z]{1,2}(?:\s*-\s*[а-яa-z]{1,2})?"
HOUSE_NUMBER = rf"\d+(?:\s*-?\s*{HOUSE_LETTER})?"
HOUSE_RANGE = rf"{HOUSE_NUMBER}(?:\s*-\s*{HOUSE_NUMBER})*"
HOUSE_PATTERN = (
    rf"(?<![\w/-])(?:(?:к|вл)\s*)?{HOUSE_RANGE}(?:\s*/\s*{HOUSE_RANGE})?"
    rf"(?:\s*[кс]\s*(?:{HOUSE_RANGE}|{HOUSE_LETTER})){{0,2}}(?![\w/-])"
)

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
    value = re.sub(r"\bкв[.-]л\b", "квартал", value)
    value = re.sub(r"\bмск\b", "москва", value)
    value = re.sub(r"\b(?:спб|питер|санктпетербург)\b", "санкт петербург", value)
    value = re.sub(r"\b(\d+)\s*[- ]?\s*(?:ая|я|й|ый|ой)\b", r"\1", value)
    return re.sub(r"\s+", " ", re.sub(r"[^а-яa-z0-9/]+", " ", value)).strip()


def house_key(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").translate(DASHES)
    value = re.sub(r"корп(?:ус)?\.?", "к", value)
    value = re.sub(r"стр(?:оение)?\.?", "с", value)
    value = re.sub(r"^(?:дом|д)\.?\s*", "", value)
    value = re.sub(r"[^а-яa-z0-9/-]+", "", value)
    # A separator before a letter is spelling; a numeric range identifies another house.
    return re.sub(r"(?<=\d)-(?=[а-яa-z])", "", value)


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
        "мкрн": "мкр",
        "квартал": "квартал",
        "квл": "квартал",
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
    search_text: str = ""


def building_kind_matches(query: Query, row: dict) -> bool:
    if not query.kind:
        return True
    display_kind = street_kind(row["display"].removeprefix(row["city"] + ", ").split(" ", 1)[0])
    if display_kind == query.kind:
        return True
    # «Большая Набережная» can be an official street name; a shared type word alone is insufficient.
    return query.kind in {street_kind(word) for word in row["street"].split()} and (
        " " + text(row["street"]) + " " in " " + text(query.raw) + " "
    )


@measured("search.normalize")
def parse_query(value: str) -> Query:
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").translate(DASHES)
    # Country is a structured prefix, while «Российская» can be a street name.
    value = re.sub(r"^\s*российская\s+федерация\b[\s,;]*", "", value)
    # Separate an attached house number, but preserve 62к1 / 4с2 components.
    value = re.sub(
        r"(?<=[а-яa-z])(?=\d)",
        lambda m: (
            ""
            if value[m.start() - 1] in "кс" and re.search(r"\d+[а-яa-z]{0,2}\s*$", value[: m.start() - 1])
            else " "
        ),
        value,
    )
    value = re.sub(r"(?<=\d)(?=[а-яa-z]{3,}\b)", " ", value)
    value = re.sub(r"\b(\d+)(?=(?:марта|мая|года|лет)\b)", r"\1 ", value)
    value = ordinals(value, protect_street_numbers=True)
    markers = list(
        re.finditer(
            r"\b(?:улица|ул|проспект|просп|пр-кт|переулок|пер|проезд|пр-д|шоссе|бульвар|"
            r"набережная|площадь|микрорайон|мкр|мкрн|аллея|ал|тупик|туп)\b",
            value,
        )
    )
    # A named street can contain «квартал»; otherwise it is an explicit address element type.
    if not markers or all(street_kind(marker[0]) == "мкр" for marker in markers):
        markers.extend(re.finditer(r"\b(?:квартал|кв-л)\b", value))
        markers.sort(key=lambda marker: marker.start())
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
    # A separator after the corpus/structure marker is not a range between houses.
    value = re.sub(r"(?<=\d)([кс])\s*-\s*(?=\d)", r"\1", value)
    # Last number is the building: numeric street names (1905 года, 1-я) stay in words.
    candidates = list(re.finditer(HOUSE_PATTERN, value))

    def unmark(words: str) -> str:
        return re.sub(r"__ordinal_(\d+)__", r"\1", words)

    if not candidates:
        return Query(
            unmark(value),
            address_words(unmark(value)),
            None,
            entrance,
            kind,
            unmark(value),
        )
    match = candidates[-1]
    house = house_key(match[0])
    search_text = unmark(value[: match.start()] + " " + value[match.end() :])
    words = address_words(search_text)
    return Query(unmark(value), words, house, entrance, kind, search_text)


def legacy_house_matches(message: str, address: str) -> bool:
    query = parse_query(message)
    requested = query.house
    returned = house_key(address.rsplit(", дом ", 1)[-1])
    if not requested or requested == returned:
        return True
    # Neither legacy whitespace folding nor an opaque-number exception may join a range.
    if "-" in requested or "-" in returned:
        return False
    # Historical locality labels may prefix a complete number; its components must still agree.
    if not re.fullmatch(HOUSE_PATTERN, returned):
        labeled = re.fullmatch(r"[а-яa-z]+(\d.*)", returned)
        return bool(labeled and labeled[1] == requested and re.fullmatch(HOUSE_PATTERN, labeled[1]))
    # A quarter may be part of the legacy street, with a standalone corpus as the house.
    if returned.startswith("к") and requested.endswith(returned):
        quarter = requested[: -len(returned)]
        prefix = address.rsplit(", дом ", 1)[0]
        if quarter and re.match(r"\d", quarter) and house_key(prefix).endswith(quarter):
            return True
    if returned.startswith("к") and re.fullmatch(r"к\d+[а-яa-z]?", returned):
        prefix = house_key(address.rsplit(", дом ", 1)[0])
        quarter = re.search(r"\b(?:квартал|микрорайон|мкр)\s+(\d+[а-яa-z]?)\b", query.raw)
        corpus = re.search(r"(?<![\w/-])к\s*" + re.escape(returned[1:]) + r"(?![\w/-])", query.raw)
        if quarter and corpus and house_key(quarter[1]) == requested and prefix.endswith(requested):
            return True
    # Preserve legacy spaced-digit input, without accepting the tail of another house/corpus.
    return any(
        house_key(match[0]) == returned
        for match in re.finditer(r"(?<![\w/-])\d+(?:[ \t]+\d+)+(?![\w/-])", query.raw)
    )
