"""Разбор русского адреса с сохранением числовых ролей и значимых названий."""

import re
import unicodedata
from dataclasses import dataclass

from src.code_service.services.v2.vocabulary import (
    CITY_ALIASES,
    DATE_WORDS,
    NAME_LABELS,
    ORDINALS,
    STOP,
    STREET_KINDS,
    TENS,
)
from src.infrastructure.telemetry import measured

DASHES = str.maketrans(dict.fromkeys("‐‑‒–—−", "-"))
HOUSE_LETTER = r"[а-яa-z]{1,2}(?:\s*-\s*[а-яa-z]{1,2})?"
HOUSE_NUMBER = rf"\d+(?:\s*-?\s*{HOUSE_LETTER})?"
HOUSE_RANGE = rf"{HOUSE_NUMBER}(?:\s*-\s*{HOUSE_NUMBER})*"
HOUSE_PATTERN = (
    rf"(?<![\w/-])(?:(?:к|вл)\s*)?{HOUSE_RANGE}(?:\s*/\s*{HOUSE_RANGE})?"
    rf"(?:\s*[кс]\s*(?:{HOUSE_RANGE}|{HOUSE_LETTER})){{0,2}}(?![\w/-])"
)


def ordinals(value: str, *, protect_street_numbers: bool = False, lexical_forms: dict | None = None) -> str:
    """Преобразовать порядковые номера улиц, сохранив роль числовых частей адреса."""
    third_forms = {}
    shift = 0

    def third(match: re.Match) -> str:
        """Сохранить исходную форму третьего номера улицы и заменить её канонической."""
        nonlocal shift
        third_forms[match.start() + shift] = match[0]
        shift += len("третий") - len(match[0])
        return "третий"

    value = re.sub(r"\bтреть(?:я|е|его|ей|ю|ем|и|их)\b", third if lexical_forms is not None else "третий", value)
    roots = "|".join(sorted(ORDINALS, key=len, reverse=True))
    pattern = rf"\b(?:({'|'.join(TENS)})\s+)?({roots})(?:ый|ий|ой|ая|яя|ое|ее|ого|его|ой|ей|ую|юю|ом|ем|ых|их)\b"

    def house_role(position: int) -> bool:
        """Не перепутать порядковый номер улицы с номером дома."""
        return bool(
            re.search(
                r"\b(?:дом|д|под[ъь]?езд|под|пд|квартира|кв|этаж|эт|корпус|корп|строение|стр)\.?\s*№?\s*$",
                value[:position],
            )
        )

    def number(match: re.Match) -> str:
        """Подготовить числовую часть текущего адресного совпадения."""
        result = str(TENS.get(match[1], 0) + ORDINALS[match[2]])
        if protect_street_numbers and not house_role(match.start()):
            if lexical_forms is not None:
                marker = f"__ordinal_{result}_{len(lexical_forms)}__"
                original = match[0]
                third_form = third_forms.get(match.end() - len("третий"))
                if third_form and original.endswith("третий"):
                    original = original.removesuffix("третий") + third_form
                lexical_forms[marker] = original
                return marker
            return f"__ordinal_{result}__"
        return result

    value = re.sub(pattern, number, value)
    if protect_street_numbers:

        def numeric(match: re.Match) -> str:
            """Сохранить числовую часть адреса в выбранном преобразовании текста."""
            if house_role(match.start()):
                return match[1]
            if lexical_forms is not None:
                marker = f"__ordinal_{match[1]}_{len(lexical_forms)}__"
                lexical_forms[marker] = match[0]
                return marker
            return f"__ordinal_{match[1]}__"

        value = re.sub(
            r"\b(\d+)\s*[- ]?\s*(?:ая|я|й|ый|ой)\b",
            numeric,
            value,
        )
        value = re.sub(
            rf"\b(\d+)(\s+(?:{'|'.join(DATE_WORDS)})\b)",
            r"__ordinal_\1__\2",
            value,
        )
    return value


def text(value: str, *, preserve_unicode_letters: bool = False) -> str:
    """Нормализовать регистр, Unicode, сокращения и разделители текста адреса."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    value = ordinals(value)
    value = re.sub(r"\bпр[\s.-]*кт\b", "проспект", value)
    value = re.sub(r"\bб[.-]р\b", "бульвар", value)
    value = re.sub(r"\bкв[.-]л\b", "квартал", value)
    value = re.sub(r"\b(?:" + "|".join(map(re.escape, CITY_ALIASES)) + r")\b", lambda m: CITY_ALIASES[m[0]], value)
    value = re.sub(r"\b(\d+)\s*[- ]?\s*(?:ая|я|й|ый|ой)\b", r"\1", value)
    separators = r"(?:[^\w/]|_)+" if preserve_unicode_letters else r"[^а-яa-z0-9/]+"
    return re.sub(r"\s+", " ", re.sub(separators, " ", value)).strip()


def house_key(value: str) -> str:
    """Получить каноническую запись дома, корпуса, строения и буквы."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").translate(DASHES)
    value = re.sub(r"корп(?:ус)?\.?", "к", value)
    value = re.sub(r"стр(?:оение)?\.?", "с", value)
    value = re.sub(r"^(?:дом|д)\.?\s*", "", value)
    value = re.sub(r"[^а-яa-z0-9/-]+", "", value)
    # A separator before a letter is spelling; a numeric range identifies another house.
    return re.sub(r"(?<=\d)-(?=[а-яa-z])", "", value)


# These can be either address labels or words in an official name. Preserve
# them separately from compatibility tokens; a candidate must explain them.


def indexed_name_words(city: str, street: str) -> str:
    """Structured keys already contain names, not free-form query syntax."""
    return " ".join((city + " " + street).split())


def query_name_words(value: str) -> str:
    # A locality label before an explicit street field is syntax. The same
    # word after it (or without a street field) can belong to the name.
    """Сохранить значимые слова названия отдельно от служебных меток адреса."""
    marker = re.search(r"\b(?:улица|ул|проспект|пр-кт|переулок|пер|проезд|пр-д|шоссе|бульвар)\b", value)
    if marker:
        prefix = re.sub(
            r"\b(?:деревня|жилой|комплекс|массив|микрорайон|мкрн?|территория)\b", " ", value[: marker.start()]
        )
        value = prefix + value[marker.start() :]
    noise = set(STOP) - NAME_LABELS
    return " ".join(w for w in text(value, preserve_unicode_letters=True).split() if w not in noise)


def protect_numbered_area(value: str) -> tuple[str, dict[str, str]]:
    """Keep a numbered quarter/microdistrict out of the house-number matcher."""
    marker = r"(?:квартал|кв[.-]л|микрорайон|мкрн?\.?)"
    number = r"\d+[а-яa-z]?"
    saved = {}

    def replace(match: re.Match) -> str:
        """Заменить распознанную адресную форму, сохранив её числовую роль."""
        prefix = match.string[: match.start()]
        if re.search(r"\b(?:улица|ул|переулок|пер|проезд|пр-д)\b", prefix):
            # In «улица Северный Квартал, 20» the word is part of the street name.
            return match[0]
        if re.search(r"(?:__area_\d+__|\b(?:дом|д)\.?)[\s,]*$", prefix):
            return match[0]
        if match[0].startswith(("квартал", "кв", "микрорайон", "мкр")) and re.search(
            r"\b[а-я]+(?:ый|ий|ой|ая|яя|ое|ее)[\s,]*$", prefix
        ):
            # A postfix type belongs to its named area: «Северный квартал, 3».
            return match[0]
        raw = match["number"]
        if not match[0].startswith(("квартал", "кв", "микрорайон", "мкр")):
            raw = re.sub(r"[йя]$", "", raw)
        token = f"__area_{len(saved)}__"
        saved[token] = raw
        start, end = match.span("number")
        tail = match[0][end - match.start() :]
        if not match[0].startswith(("квартал", "кв", "микрорайон", "мкр")):
            tail = re.sub(r"^-?(?:ая|я|й|ый|ой)\s*", " ", tail)
        return match[0][: start - match.start()] + " " + token + " " + tail

    value = re.sub(rf"(?<![\w/-])(?P<number>{number})\s*(?:-?(?:ая|я|й|ый|ой)\s*)?(?={marker}\b)", replace, value)
    value = re.sub(
        rf"\b{marker}\s*(?P<number>{number})(?=\s|[,;]|$|[кс]\s*\d|корп|строен|стр\.)",
        replace,
        value,
    )
    return value, saved


def address_words(value: str, *, preserve_unicode_letters: bool = False) -> str:
    """Получить поисковые слова адреса без известных служебных слов."""
    return " ".join(w for w in text(value, preserve_unicode_letters=preserve_unicode_letters).split() if w not in STOP)


def street_kind(value: str) -> str:
    """Привести тип улицы к каноническому обозначению."""
    value = re.sub(r"[^а-я]", "", value.casefold())
    return STREET_KINDS.get(value, value)


@dataclass(frozen=True)
class Query:
    """Разобранный запрос с раздельными ограничениями адреса и числовых ролей."""

    raw: str
    words: str
    house: str | None
    entrance: str | None
    kind: str | None = None
    search_text: str = ""
    name_words: str = ""
    quarter: str | None = None


def building_kind_matches(query: Query, row: dict) -> bool:
    """Проверить совместимость явно указанного типа улицы с кандидатом."""
    if not query.kind:
        return True
    display_kind = street_kind(row["display"].removeprefix(row["city"] + ", ").split(" ", 1)[0])
    if display_kind == query.kind:
        return True
    # Generic «улица» is often prepended to an explicitly named highway/avenue.
    # The candidate must still explain every meaningful name token separately.
    if (
        query.kind == "ул"
        and display_kind in {"ш", "пркт", "проезд", "пер", "б-р", "пл"}
        and any(street_kind(word) == display_kind for word in re.findall(r"[а-я]+(?:-[а-я]+)?", query.raw))
    ):
        return True
    # «Большая Набережная» can be an official street name; a shared type word alone is insufficient.
    return query.kind in {street_kind(word) for word in text(row["street"]).split()} and (
        " " + text(row["street"]) + " " in " " + text(query.raw) + " "
    )


@measured("search.normalize")
def parse_query(value: str, *, preserve_lexical_ordinals: bool = False) -> Query:
    """Разобрать текст на название, дом, подъезд и значимые адресные ограничения."""
    value = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е").translate(DASHES)
    # Country is a structured prefix, while «Российская» can be a street name.
    value = re.sub(r"^\s*российская\s+федерация\b[\s,;]*", "", value)
    value, area_numbers = protect_numbered_area(value)
    # Separate an attached house number, but preserve 62к1 / 4с2 components.
    value = re.sub(
        r"(?<=[а-яa-z])(?=\d)",
        lambda m: (
            "" if value[m.start() - 1] in "кс" and re.search(r"\d+[а-яa-z]{0,2}\s*$", value[: m.start() - 1]) else " "
        ),
        value,
    )
    value = re.sub(r"(?<=\d)(?=[а-яa-z]{3,}\b)", " ", value)
    value = re.sub(r"\b(\d+)(?=(?:марта|мая|года|лет)\b)", r"\1 ", value)
    lexical_forms = {} if preserve_lexical_ordinals else None
    value = ordinals(value, protect_street_numbers=True, lexical_forms=lexical_forms)
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
    explicit_street = next((marker for marker in markers if street_kind(marker[0]) == "ул"), None)
    if explicit_street:
        kind = "ул"
    if area_numbers:
        last_area = max(value.index(marker) for marker in area_numbers)
        if not markers or markers[-1].start() < last_area:
            kind = "квартал" if re.search(r"\b(?:квартал|кв[.-]л)\b", value) else "мкр"
    if markers:
        from src.code_service.services.v2.localities import clean_structured_prefix

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
    if area_numbers:
        candidates.extend(re.finditer(rf"(?<![\w/-])с\s*{HOUSE_RANGE}(?![\w/-])", value))
        candidates.sort(key=lambda match: match.start())

    def unmark(words: str, lexical: bool = False) -> str:
        """Восстановить защищённые формы адреса после выделения номера дома."""
        words = re.sub(
            r"__ordinal_(\d+)(?:_\d+)?__",
            lambda m: lexical_forms.get(m[0], m[1]) if lexical and lexical_forms is not None else m[1],
            words,
        )
        for marker, number in area_numbers.items():
            words = words.replace(marker, number)
        return words

    if not candidates:
        return Query(
            unmark(value),
            address_words(unmark(value)),
            None,
            entrance,
            kind,
            unmark(value, lexical=True),
            query_name_words(unmark(value)),
            next(iter(area_numbers.values()), None),
        )
    match = candidates[-1]
    house = house_key(match[0])
    if area_numbers and re.fullmatch(r"[кс]\d+[а-яa-z]?", house) and not explicit_street:
        kind = "квартал" if re.search(r"\b(?:квартал|кв[.-]л)\b", value) else "мкр"
    lexical = value[: match.start()] + " " + value[match.end() :]
    search_text = unmark(lexical, lexical=True)
    words = address_words(unmark(lexical))
    return Query(
        unmark(value),
        words,
        house,
        entrance,
        kind,
        search_text,
        query_name_words(unmark(lexical)),
        next(iter(area_numbers.values()), None),
    )


def legacy_house_matches(message: str, address: str) -> bool:
    """Проверить, что исходный номер v1 обозначает тот же дом."""
    query = parse_query(message)
    requested = query.house
    returned = house_key(address.rsplit(", дом ", 1)[-1])
    if query.quarter and not requested:
        return False
    if query.quarter and query.quarter not in text(address.rsplit(", дом ", 1)[0]).split():
        return False
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
        house_key(match[0]) == returned for match in re.finditer(r"(?<![\w/-])\d+(?:[ \t]+\d+)+(?![\w/-])", query.raw)
    )
