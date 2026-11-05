"""Create a reproducible, relational read model without modifying legacy codes.db."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path

from src.search.normalize import address_words, house_key, street_kind, text
from tools.refresh_aliases import refresh

SCHEMA = """
PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;
CREATE TABLE buildings(id TEXT PRIMARY KEY,guid TEXT,display TEXT,city TEXT,street TEXT,house TEXT,street_key TEXT,city_key TEXT,region TEXT,lineage TEXT);
CREATE TABLE streets(id TEXT PRIMARY KEY,city_key TEXT,street_key TEXT,words TEXT);
CREATE TABLE codes(building_id TEXT,entrance TEXT,kind TEXT,code TEXT,UNIQUE(building_id,entrance,kind,code));
CREATE INDEX codes_building ON codes(building_id);
CREATE TABLE provenance(source_id INTEGER PRIMARY KEY,building_id TEXT,resolution TEXT);
CREATE TABLE aliases(building_id TEXT,city_key TEXT,street_key TEXT,house TEXT,UNIQUE(building_id,city_key,street_key,house));
CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,words,tokenize='unicode61');
CREATE VIRTUAL TABLE vocabulary USING fts5vocab(search,'row');
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT);
"""


def link_codes(db: sqlite3.Connection, legacy: Path, stats: dict) -> None:
    source = sqlite3.connect(f"file:{legacy.resolve().as_posix()}?mode=ro", uri=True)
    # Stream grouped rows: all source IDs retained, but identical codes merge.
    last_key, building_id = None, None
    last_raw = None
    key = None
    source.row_factory = sqlite3.Row
    for row in source.execute("SELECT * FROM codes ORDER BY lower(city),lower(street),house"):
        raw = (row["city"], row["street"], row["house"], row["street_type"])
        if raw != last_raw:
            key = (
                text(row["city"] or ""),
                text(row["street"] or ""),
                house_key(row["house"] or ""),
                street_kind(row["street_type"] or ""),
            )
            last_raw = raw
        if key != last_key:
            city, street, house, kind = key
            candidates = db.execute(
                "SELECT id,city_key,lineage,display FROM buildings WHERE city_key=? AND street_key=? AND house=? AND guid IS NOT NULL",
                (city, street, house),
            ).fetchall()
            if not candidates:
                candidates = db.execute(
                    "SELECT id,city_key,lineage,display FROM buildings WHERE street_key=? AND house=? AND guid IS NOT NULL",
                    (street, house),
                ).fetchall()
            city_words = set(address_words(city).split())
            exact = [
                c
                for c in candidates
                if (c[1] == city or (city_words and city_words <= set(address_words(c[2]).split())))
                and street_kind(c[3].split(", ", 1)[1].split(" ", 1)[0]) == kind
            ]
            if len(exact) == 1:
                building_id = exact[0][0]
                resolution = "gar_exact"
                stats["linked_groups"] += 1
            else:
                # Never assume Moscow/Podolsk aliases without a unique GAR identity.
                building_id = "legacy:" + hashlib.sha256("\0".join(key).encode()).hexdigest()[:24]
                resolution = "ambiguous" if len(exact) > 1 else "unresolved"
                stats["ambiguous_groups" if len(exact) > 1 else "unresolved_groups"] += 1
                display = f"{row['city']}, {row['street_type']} {row['street']}, дом {row['house']}"
                db.execute(
                    "INSERT OR IGNORE INTO buildings VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (building_id, None, display, row["city"], row["street"], house, street, city, "", city),
                )
            db.execute("INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)", (building_id, city, street, house))
            last_key = key
        db.execute(
            "INSERT OR IGNORE INTO codes VALUES(?,?,?,?)",
            (building_id, str(row["entrance"]), str(row["code_type"]), str(row["code"])),
        )
        db.execute("INSERT OR REPLACE INTO provenance VALUES(?,?,?)", (row["id"], building_id, resolution))
        stats["legacy_rows"] += 1
        if stats["legacy_rows"] % 100000 == 0:
            db.commit()
            print(json.dumps({"linked_source_rows": stats["legacy_rows"]}), flush=True)
    source.close()
    db.commit()


def index_streets(db: sqlite3.Connection) -> None:
    db.execute("DELETE FROM streets")
    db.execute("DELETE FROM search")
    cursor = db.execute("SELECT city_key,street_key FROM buildings GROUP BY city_key,street_key")
    while rows := cursor.fetchmany(10000):
        streets = [
            (
                hashlib.sha256((city + "\0" + street).encode()).hexdigest()[:32],
                city,
                street,
                address_words(city + " " + street),
            )
            for city, street in rows
        ]
        db.executemany("INSERT INTO streets VALUES(?,?,?,?)", streets)
        db.executemany("INSERT INTO search(id,words) VALUES(?,?)", [(r[0], r[3]) for r in streets])
    refresh(db)


def build(legacy: Path, regions: Path, output: Path):
    output.parent.mkdir(parents=True, exist_ok=True)
    work = output.with_suffix(".building.db")
    if work.exists():
        work.unlink()
    db = sqlite3.connect(work)
    db.executescript(SCHEMA)
    db.execute("PRAGMA cache_size=-524288")
    stats = {"gar_buildings": 0, "legacy_rows": 0, "linked_groups": 0, "unresolved_groups": 0, "ambiguous_groups": 0}
    files = [p for p in sorted(regions.glob("gar-*.db")) if ".building." not in p.name]
    for region in files:
        db.execute("ATTACH DATABASE ? AS region", (str(region.resolve()),))
        stats["gar_buildings"] += db.execute("SELECT count(*) FROM region.buildings").fetchone()[0]
        db.execute("INSERT OR IGNORE INTO buildings SELECT * FROM region.buildings")
        db.commit()
        db.execute("DETACH DATABASE region")
        print(json.dumps({"imported_region": region.stem, "buildings": stats["gar_buildings"]}), flush=True)
    db.executescript(
        "CREATE INDEX building_lookup ON buildings(street_key,house); CREATE INDEX building_street ON buildings(city_key,street_key,house);"
    )
    link_codes(db, legacy, stats)
    index_streets(db)
    stats["street_documents"] = db.execute("SELECT count(*) FROM streets").fetchone()[0]
    stats["unique_codes"] = db.execute("SELECT count(*) FROM codes").fetchone()[0]
    stats["buildings_with_codes"] = db.execute("SELECT count(distinct building_id) FROM codes").fetchone()[0]
    with legacy.open("rb") as source_file:
        stats["source_sha256"] = hashlib.file_digest(source_file, "sha256").hexdigest()
    stats["regions"] = [r.stem.removeprefix("gar-") for r in files]
    db.execute("INSERT INTO metadata VALUES(?,?)", ("build", json.dumps(stats)))
    db.commit()
    db.execute("ANALYZE")
    db.close()
    work.replace(output)
    print(json.dumps(stats), flush=True)


def relink(legacy: Path, output: Path) -> None:
    import shutil

    work = output.with_suffix(".relink.db")
    shutil.copyfile(output, work)
    db = sqlite3.connect(work)
    stats = json.loads(db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
    with legacy.open("rb") as source:
        assert hashlib.file_digest(source, "sha256").hexdigest() == stats["source_sha256"]
    db.execute("PRAGMA cache_size=-524288")
    db.executescript(
        "DELETE FROM codes; DELETE FROM aliases; DELETE FROM provenance; DELETE FROM buildings WHERE guid IS NULL;"
    )
    for k in ["legacy_rows", "linked_groups", "unresolved_groups", "ambiguous_groups"]:
        stats[k] = 0
    link_codes(db, legacy, stats)
    index_streets(db)
    stats["street_documents"] = db.execute("SELECT count(*) FROM streets").fetchone()[0]
    stats["unique_codes"] = db.execute("SELECT count(*) FROM codes").fetchone()[0]
    stats["buildings_with_codes"] = db.execute("SELECT count(distinct building_id) FROM codes").fetchone()[0]
    stats["identity_schema"] = 2
    db.execute("UPDATE metadata SET value=? WHERE key='build'", (json.dumps(stats),))
    db.commit()
    db.close()
    work.replace(output)
    print(json.dumps(stats), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--legacy", type=Path, default=Path("codes.db"))
    p.add_argument("--regions", type=Path, default=Path("artifacts/catalog"))
    p.add_argument("--output", type=Path, default=Path("artifacts/search/catalog.db"))
    p.add_argument("--relink-only", action="store_true")
    a = p.parse_args()
    if a.relink_only:
        relink(a.legacy, a.output)
    else:
        build(a.legacy, a.regions, a.output)
