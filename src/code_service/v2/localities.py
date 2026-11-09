"""Conservative official locality aliases for offline source reconciliation."""

import json
import re
from pathlib import Path

from src.code_service.v2.normalize import address_words, text

URBAN = re.compile(
    r"^(?:рабочий\s+пос[её]лок\s+|пос[её]лок\s+городского\s+типа\s+|"
    r"п\.?\s*г\.?\s*т(?:\.\s*|\s+)|р\.?\s*п(?:\.\s*|\s+))(.+)$",
    re.I,
)
SUFFIX = re.compile(
    r"\s+(?:городской\s+округ|муниципальный\s+округ|муниципальный\s+район|район|р-н|г\.?\s*о\.?)$", re.I
)


def urban_city(value: str) -> tuple[str, set[str]] | None:
    parts = value.split(",")
    town = URBAN.fullmatch(parts[-1].strip())
    if not town:
        return None
    name = text(town[1])
    if not name:
        return None
    prefix = " ".join(SUFFIX.sub("", part.strip()) for part in parts[:-1])
    return name, set(address_words(prefix + " " + name).split())


def clean_structured_prefix(value: str, street_position: int) -> str:
    """Only simplify explicit locality fields before a separate street segment."""
    boundary = value.rfind(",", 0, street_position)
    if boundary < 0:
        return value
    parts = []
    for field in value[:boundary].split(","):
        urban = URBAN.fullmatch(field.strip())
        parts.append(urban[1] if urban else SUFFIX.sub("", field.strip()))
    return ",".join(parts) + value[boundary:]


def load_facts(path: Path | None) -> dict:
    result = {}
    if path:
        for line in path.read_text(encoding="utf8").splitlines():
            record = json.loads(line)
            result.setdefault((record["region"], record["city_key"]), []).append(record)
    return result


def match(constraint: tuple[str, set[str]] | None, city: str, region: str, lineage: str, facts: dict) -> bool:
    if not constraint or constraint[0] != city:
        return False
    lineage_words = set(address_words(lineage).split())
    candidates = [row for row in facts.get((region, city), []) if set(row["admin_words"]) <= lineage_words]
    # Identical place names are never sufficient to choose a locality object.
    if len(candidates) != 1:
        return False
    record = candidates[0]
    if re.sub(r"\W+", "", record["kind"].casefold()) not in {"рп", "пгт"}:
        return False
    return constraint[1] <= set(record["admin_words"]) | set(record["municipal_words"])
