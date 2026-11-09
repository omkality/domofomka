"""Подтверждать тип внутри названия и явный субъект РФ только по уникальному дому ГАР.

Регион проверяется по корневым объектам того же XML-снимка. Номер дома, корпус,
строение и город не угадываются. Исходная база v1 сохраняется.
"""

import argparse
import collections
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from src.code_service.v2.normalize import street_kind, text
from tools.catalog.reconcile_street_order import apply
from tools.evaluation.quality_suite import address_fields

REGION_KINDS = {
    "респ": ("респ", "республика"),
    "обл": ("обл", "область"),
    "край": ("край",),
    "г": ("г", "город"),
    "ао": ("ао", "автономный округ"),
    "аобл": ("аобл", "автономная область"),
}
STREET_PREFIX = re.compile(
    r"^(улица|ул|проспект|просп|пркт|проезд|переулок|пер|шоссе|ш|бульвар|набережная|наб|площадь|аллея)\s+(.+)$"
)


def region_forms(facts: list[dict]) -> dict[str, set[str]]:
    forms = collections.defaultdict(set)
    for row in facts:
        name = text(row["name"])
        kinds = REGION_KINDS.get(text(row["kind"]), ())
        for kind in kinds:
            forms[text(kind + " " + name)].add(row["region"])
            forms[text(name + " " + kind)].add(row["region"])
            if re.search(r"(?:^|\s)" + re.escape(kind) + r"(?:\s|$)", name):
                forms[name].add(row["region"])
    return dict(forms)


def explicit_region_city(value: str, forms: dict[str, set[str]]) -> tuple[str, str] | None:
    parts = [text(part) for part in value.split(",")]
    if parts and parts[0] in {"россия", "российская федерация"}:
        parts = parts[1:]
    # Другие административные поля нельзя молча отбросить.
    if len(parts) != 2:
        return None
    regions = forms.get(parts[0], set())
    if len(regions) != 1:
        return None
    city = re.sub(r"^(?:город|г)\s+", "", parts[1])
    return (next(iter(regions)), city) if city else None


def mappings(db: sqlite3.Connection, facts: list[dict]) -> tuple[list[dict], dict]:
    db.row_factory = sqlite3.Row
    forms = region_forms(facts)
    rows = db.execute("""SELECT b.* FROM buildings b JOIN
        (SELECT DISTINCT building_id FROM codes) c ON c.building_id=b.id WHERE b.guid IS NULL""").fetchall()
    result = []
    skipped = collections.Counter()
    reasons = collections.Counter()
    for row in rows:
        try:
            kind = street_kind(address_fields(row["display"])["kind"])
        except ValueError:
            skipped["unparsed_source"] += 1
            continue
        region_city = explicit_region_city(row["city"], forms)
        city, region = (region_city[1], region_city[0]) if region_city else (row["city_key"], None)
        street = row["street_key"]
        embedded = STREET_PREFIX.fullmatch(street)
        if not region_city and not embedded:
            continue

        def official(name: str, required_kind: str, city=city, region=region, house=row["house"]) -> list:
            candidates = db.execute(
                "SELECT id,guid,display,region FROM buildings WHERE city_key=? AND street_key=? "
                "AND house=? AND guid IS NOT NULL",
                (city, name, house),
            ).fetchall()
            found = []
            for candidate in candidates:
                if region and candidate["region"] != region:
                    continue
                try:
                    target_kind = street_kind(address_fields(candidate["display"])["kind"])
                except ValueError:
                    continue
                if target_kind == required_kind:
                    found.append(candidate)
            return found

        exact = official(street, kind)
        reason = "explicit_region"
        # Существующее буквальное название приоритетно; тип в имени может быть его частью.
        if not exact and embedded:
            literal_street = db.execute(
                "SELECT display,region FROM buildings WHERE city_key=? AND street_key=? AND guid IS NOT NULL",
                (city, street),
            )
            literal_exists = False
            for candidate in literal_street:
                if region and candidate["region"] != region:
                    continue
                try:
                    if street_kind(address_fields(candidate["display"])["kind"]) == kind:
                        literal_exists = True
                        break
                except ValueError:
                    continue
            if literal_exists:
                # Даже отсутствующий в ГАР номер не позволяет переехать на другую улицу.
                skipped["literal_street_without_house"] += 1
                continue
            embedded_kind = street_kind(embedded[1])
            if kind not in {"ул", embedded_kind}:
                skipped["conflicting_embedded_type"] += 1
                continue
            exact = official(embedded[2], embedded_kind)
            reason = "embedded_type" if not region_city else "explicit_region_and_embedded_type"
        if len(exact) != 1:
            skipped["non_unique_house_or_type"] += 1
            continue
        if not region_city and reason != "embedded_type":
            continue
        target = exact[0]
        result.append(
            {
                "source": row["id"],
                "target": target["id"],
                "guid": target["guid"],
                "source_address": row["display"],
                "target_address": target["display"],
                "reason": reason,
                "resolution": "gar_source_syntax_" + reason,
            }
        )
        reasons[reason] += 1
    return result, {
        "unlinked_buildings": len(rows),
        "confirmed_groups": len(result),
        "reasons": dict(reasons),
        "skipped": dict(skipped),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--regions", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    facts_bytes = args.regions.read_bytes()
    facts_digest = hashlib.sha256(facts_bytes).hexdigest()
    facts = json.loads(facts_bytes)["regions"]
    with closing(sqlite3.connect(f"file:{args.catalog.resolve().as_posix()}?mode=ro", uri=True)) as db:
        links, summary = mappings(db, facts)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    if args.report.exists():
        raise FileExistsError("Preserve the previous reviewed mappings")
    args.report.write_text(
        json.dumps(
            {"summary": summary, "region_facts_sha256": facts_digest, "links": links}, ensure_ascii=False, indent=2
        ),
        encoding="utf-8",
    )
    print(json.dumps(summary), flush=True)
    if args.output:
        apply(args.catalog, args.output, links, resolution="gar_source_syntax", schema_version=6)
        with sqlite3.connect(args.output) as db:
            stats = json.loads(db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
            stats["source_syntax_region_facts_sha256"] = facts_digest
            db.execute("UPDATE metadata SET value=? WHERE key='build'", (json.dumps(stats),))


if __name__ == "__main__":
    main()
