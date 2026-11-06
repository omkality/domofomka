"""Связать перестановки слов только с единственной официальной улицей ГАР.

Работает офлайн с полной сборкой; исходник и v1 не меняет. Без --output только
готовит закрытый отчёт. Не исправляет тип улицы и не угадывает населённый пункт.
"""

import argparse
import collections
import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

from src.search.normalize import street_kind
from tools.build_catalog import index_streets
from tools.quality_suite import address_fields


def key(city: str, street: str) -> tuple:
    return city, tuple(sorted(street.split()))


def mappings(db: sqlite3.Connection) -> tuple[list[dict], dict]:
    db.row_factory = sqlite3.Row
    rows = db.execute("""SELECT b.* FROM buildings b JOIN
        (SELECT DISTINCT building_id FROM codes) c ON c.building_id=b.id WHERE b.guid IS NULL""").fetchall()
    wanted = {key(row["city_key"], row["street_key"]) for row in rows if len(row["street_key"].split()) > 1}
    variants = collections.defaultdict(set)
    for city, street in db.execute("SELECT city_key,street_key FROM streets"):
        identity = key(city, street)
        if identity in wanted:
            variants[identity].add(street)
    official = {}
    for identity, variants_for_street in variants.items():
        names = {
            name
            for name in variants_for_street
            if db.execute(
                "SELECT 1 FROM buildings WHERE city_key=? AND street_key=? AND guid IS NOT NULL LIMIT 1",
                (identity[0], name),
            ).fetchone()
        }
        if len(names) == 1:
            official[identity] = next(iter(names))
    result = []
    skipped = collections.Counter()
    for row in rows:
        canonical = official.get(key(row["city_key"], row["street_key"]))
        if not canonical or canonical == row["street_key"]:
            continue
        try:
            kind = street_kind(address_fields(row["display"])["kind"])
        except ValueError:
            skipped["unparsed_source"] += 1
            continue
        candidates = []
        for target in db.execute(
            """SELECT id,guid,display FROM buildings WHERE city_key=? AND street_key=?
            AND house=? AND guid IS NOT NULL""",
            (row["city_key"], canonical, row["house"]),
        ):
            try:
                target_kind = street_kind(address_fields(target["display"])["kind"])
            except ValueError:
                continue
            if target_kind == kind:
                candidates.append(target)
        if len(candidates) != 1:
            skipped["non_unique_house_or_type"] += 1
            continue
        target = candidates[0]
        result.append(
            {
                "source": row["id"],
                "target": target["id"],
                "guid": target["guid"],
                "source_address": row["display"],
                "target_address": target["display"],
            }
        )
    return result, {"unlinked_buildings": len(rows), "confirmed_groups": len(result), "skipped": dict(skipped)}


def apply(source: Path, output: Path, links: list[dict]) -> None:
    if source.resolve() == output.resolve() or output.exists():
        raise FileExistsError("Use a fresh candidate; source catalog must remain immutable")
    if shutil.disk_usage(output.parent).free < source.stat().st_size + 1073741824:
        raise ValueError("Not enough space for an independent candidate")
    work = output.with_suffix(".building.db")
    if work.exists():
        raise FileExistsError("Candidate work file exists")
    shutil.copyfile(source, work)
    with closing(sqlite3.connect(work)) as db:
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-524288;")
        db.execute("CREATE INDEX IF NOT EXISTS provenance_building ON provenance(building_id)")
        before = db.execute("SELECT count(*) FROM provenance").fetchone()[0]
        for link in links:
            old, new = link["source"], link["target"]
            db.execute(
                "INSERT OR IGNORE INTO codes SELECT ?,entrance,kind,code FROM codes WHERE building_id=?", (new, old)
            )
            db.execute("DELETE FROM codes WHERE building_id=?", (old,))
            db.execute(
                "INSERT OR IGNORE INTO aliases SELECT ?,city_key,street_key,house FROM aliases WHERE building_id=?",
                (new, old),
            )
            db.execute("DELETE FROM aliases WHERE building_id=?", (old,))
            db.execute(
                "UPDATE provenance SET building_id=?,resolution='gar_street_word_order' WHERE building_id=?", (new, old)
            )
            db.execute("DELETE FROM buildings WHERE id=? AND guid IS NULL", (old,))
        assert db.execute("SELECT count(*) FROM provenance").fetchone()[0] == before
        index_streets(db)
        stats = json.loads(db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
        stats.update(
            normalization_schema=5,
            street_order_linked_groups=len(links),
            unique_codes=db.execute("SELECT count(*) FROM codes").fetchone()[0],
            buildings_with_codes=db.execute("SELECT count(DISTINCT building_id) FROM codes").fetchone()[0],
        )
        stats["street_order_linked_source_rows"] = db.execute(
            "SELECT count(*) FROM provenance WHERE resolution='gar_street_word_order'"
        ).fetchone()[0]
        previous_redirects = db.execute("SELECT value FROM metadata WHERE key='building_redirects'").fetchone()
        redirects = json.loads(previous_redirects[0]) if previous_redirects else {}
        remap = {link["source"]: link["target"] for link in links}
        redirects = {old: remap.get(target, target) for old, target in redirects.items()}
        redirects.update(remap)
        db.execute("INSERT OR REPLACE INTO metadata VALUES('building_redirects',?)", (json.dumps(redirects),))
        db.execute("UPDATE metadata SET value=? WHERE key='build'", (json.dumps(stats),))
        db.commit()
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    work.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with closing(sqlite3.connect(f"file:{args.catalog.resolve().as_posix()}?mode=ro", uri=True)) as db:
        links, summary = mappings(db)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps({"summary": summary, "links": links}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(summary), flush=True)
    if args.output:
        apply(args.catalog, args.output, links)


if __name__ == "__main__":
    main()
