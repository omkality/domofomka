"""Версионируемый словарь адресного языка; грамматические правила остаются в normalize."""

import json
from importlib.resources import files
from types import MappingProxyType

_DATA = json.loads(files(__package__).joinpath("russian.json").read_text(encoding="utf8"))
ORDINALS = MappingProxyType(_DATA["ordinals"])
TENS = MappingProxyType(_DATA["tens"])
STREET_KINDS = MappingProxyType(_DATA["street_kinds"])
CITY_ALIASES = MappingProxyType(_DATA["city_aliases"])
STOP = tuple(_DATA["stop"])
NAME_LABELS = frozenset(_DATA["name_labels"])
DATE_WORDS = tuple(_DATA["date_words"])
del _DATA
