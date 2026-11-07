"""Восстановить границы полей источника только при единственном точном доме ГАР.

Проверяется весь адрес, тип, город и полный номер. Опечатки и перестановки слов
здесь не исправляются. Каждая исходная строка должна подтверждать ту же привязку.
"""

import argparse
import collections
import hashlib
import json
import re
import sqlite3
from contextlib import closing
from pathlib import Path

from src.search.normalize import HOUSE_PATTERN, house_key, street_kind, text
from tools.quality_suite import address_fields
from tools.reconcile_street_order import apply

EMBEDDED_STREET = re.compile(
    r",\s*(улица|ул\.|проспект|пр-кт|проезд|пр-д|переулок|пер\.|шоссе|ш\.|бульвар|б-р|"
    r"набережная|наб\.|площадь|пл\.)\s+(.+)$",
    re.I,
)


def shape(city: str, kind: str, street: str, house: str) -> tuple[tuple, bool] | None:
    changed = False
    kind = street_kind(kind)
    if embedded := EMBEDDED_STREET.search(city):
        if street_kind(embedded[1]) != kind:
            return None
        street = embedded[2] + " " + street
        city = city[: embedded.start()]
        changed = True
    if opaque := re.fullmatch(r"([а-яa-z ._-]+)(\d.*)", house, re.I):
        street += " " + opaque[1]
        house = opaque[2]
        changed = True
    number = house_key(house)
    if not re.fullmatch(HOUSE_PATTERN, number):
        return None
    return (text(city), kind, text(street).replace(" ", ""), number), changed


def mappings(db: sqlite3.Connection, legacy: sqlite3.Connection) -> tuple[list[dict], dict]:
    db.row_factory = sqlite3.Row
    legacy.row_factory = sqlite3.Row
    sources = []
    for row in db.execute(
        "SELECT b.* FROM (SELECT DISTINCT building_id FROM codes) c JOIN buildings b ON b.id=c.building_id WHERE b.guid IS NULL"
    ):
        try:
            identity = shape(row["city"], address_fields(row["display"])["kind"], row["street"], row["house"])
        except ValueError:
            continue
        if identity and identity[1]:
            sources.append((dict(row), identity[0]))
    wanted = {(identity[0], identity[2]) for _, identity in sources}
    street_variants = collections.defaultdict(set)
    for city, street in db.execute("SELECT city_key,street_key FROM streets"):
        key = city, street.replace(" ", "")
        if key in wanted:
            street_variants[key].add(street)
    links, skipped = [], collections.Counter()
    for row, identity in sources:
        variants = street_variants.get((identity[0], identity[2]), set())
        if len(variants) != 1:
            skipped["no_unique_official_street"] += 1
            continue
        official = db.execute(
            "SELECT * FROM buildings WHERE city_key=? AND street_key=? AND house=? AND guid IS NOT NULL",
            (identity[0], next(iter(variants)), identity[3]),
        ).fetchall()
        same_kind = [
            candidate
            for candidate in official
            if street_kind(address_fields(candidate["display"])["kind"]) == identity[1]
        ]
        if len(same_kind) != 1:
            skipped["non_unique_gar_identity"] += 1
            continue
        target = same_kind[0]
        ids = [r[0] for r in db.execute("SELECT source_id FROM provenance WHERE building_id=?", (row["id"],))]
        actual = []
        for start in range(0, len(ids), 500):
            batch = ids[start : start + 500]
            actual.extend(
                legacy.execute("SELECT * FROM codes WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch)
            )
        if (
            not ids
            or len(actual) != len(ids)
            or any(
                not (value := shape(r["city"], r["street_type"], r["street"], r["house"])) or value[0] != identity
                for r in actual
            )
        ):
            skipped["source_rows_disagree"] += 1
            continue
        links.append(
            {
                "source": row["id"],
                "target": target["id"],
                "guid": target["guid"],
                "source_address": row["display"],
                "target_address": target["display"],
                "resolution": "gar_source_field_alignment",
                "source_rows": len(ids),
            }
        )
    return links, {
        "confirmed_groups": len(links),
        "source_rows": sum(r["source_rows"] for r in links),
        "skipped": dict(skipped),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, default=Path("codes.db"))
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.report.exists():
        raise FileExistsError("Preserve previous reviewed report")
    with args.legacy.open("rb") as stream:
        revision = hashlib.file_digest(stream, "sha256").hexdigest()
    with (
        closing(sqlite3.connect("file:" + args.catalog.resolve().as_posix() + "?mode=ro", uri=True)) as db,
        closing(sqlite3.connect("file:" + args.legacy.resolve().as_posix() + "?mode=ro", uri=True)) as legacy,
    ):
        stats = json.loads(db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
        if stats["source_sha256"] != revision:
            raise ValueError("Legacy source revision differs")
        links, summary = mappings(db, legacy)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"summary": summary, "links": links}, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(json.dumps(summary), flush=True)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        apply(args.catalog, args.output, links, resolution="gar_source_field_alignment", schema_version=8)


if __name__ == "__main__":
    main()
