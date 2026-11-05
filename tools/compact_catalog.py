"""Publish a small serving catalog. Full GAR GUID/provenance stays in the offline build.

Code-bearing identities retain exact GUIDs. Known houses without codes are represented
as GAR address keys, never invented physical GUIDs. No geography is inferred.
"""

import argparse
import json
import sqlite3
import zlib
from pathlib import Path


def compact(source: Path, output: Path):
    work = output.with_suffix(".building.db")
    output.parent.mkdir(parents=True, exist_ok=True)
    if work.exists():
        work.unlink()
    src = sqlite3.connect("file:" + source.resolve().as_posix() + "?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    src.execute("PRAGMA cache_size=-524288")
    db = sqlite3.connect(work)
    db.execute("PRAGMA journal_mode=OFF")
    db.execute("PRAGMA synchronous=OFF")
    db.execute("PRAGMA cache_size=-262144")
    db.execute("ATTACH DATABASE ? AS source", (str(source.resolve()),))
    db.executescript("""
    CREATE TABLE buildings AS SELECT b.* FROM (SELECT DISTINCT building_id FROM source.codes) c JOIN source.buildings b ON b.id=c.building_id;
    CREATE UNIQUE INDEX building_id ON buildings(id);
    CREATE INDEX building_lookup ON buildings(street_key,house);
    CREATE INDEX building_street ON buildings(city_key,street_key,house);
    CREATE TABLE codes AS SELECT * FROM source.codes;
    CREATE INDEX codes_building ON codes(building_id);
    CREATE TABLE aliases AS SELECT * FROM source.aliases;
    CREATE TABLE streets(id TEXT PRIMARY KEY,city_key TEXT,street_key TEXT,words TEXT,has_codes INTEGER);
    CREATE TABLE street_houses(id TEXT PRIMARY KEY,data BLOB);
    CREATE VIRTUAL TABLE search USING fts5(id UNINDEXED,words,tokenize='unicode61');
    CREATE VIRTUAL TABLE vocabulary USING fts5vocab(search,'row');
    CREATE TABLE metadata AS SELECT * FROM source.metadata;
    INSERT INTO metadata VALUES('format','compact');
    """)
    coded = {(r[0], r[1]) for r in db.execute("SELECT DISTINCT city_key,street_key FROM buildings")}
    count = 0
    houses = 0
    for street in src.execute("SELECT * FROM streets"):
        groups = {}
        row_count = 0
        for house in src.execute(
            "SELECT display,house,city,street,region,lineage FROM buildings WHERE city_key=? AND street_key=?",
            (street["city_key"], street["street_key"]),
        ):
            prefix = house["display"].rsplit(", дом ", 1)[0]
            group_key = (prefix, house["region"], house["lineage"])
            if group_key not in groups:
                groups[group_key] = {
                    "prefix": prefix,
                    "city": house["city"],
                    "street": house["street"],
                    "region": house["region"],
                    "lineage": house["lineage"],
                    "houses": set(),
                }
            groups[group_key]["houses"].add(house["house"])
            row_count += 1
        bundles = []
        for group_key in sorted(groups):
            group = groups[group_key]
            group["houses"] = sorted(group["houses"])
            bundles.append(group)
        db.execute(
            "INSERT INTO streets VALUES(?,?,?,?,?)", (*street, int((street["city_key"], street["street_key"]) in coded))
        )
        db.execute("INSERT INTO search VALUES(?,?)", (street["id"], street["words"]))
        data = zlib.compress(json.dumps(bundles, ensure_ascii=False, separators=(",", ":")).encode(), 6)
        db.execute("INSERT INTO street_houses VALUES(?,?)", (street["id"], data))
        count += 1
        houses += row_count
        if count % 50000 == 0:
            db.commit()
            print(json.dumps({"streets": count, "houses": houses}), flush=True)
    db.commit()
    db.execute("ANALYZE")
    db.close()
    src.close()
    work.replace(output)
    print(
        json.dumps(
            {"complete": True, "street_documents": count, "source_buildings": houses, "bytes": output.stat().st_size}
        ),
        flush=True,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("artifacts/search/catalog.db"))
    p.add_argument("--output", type=Path, default=Path("artifacts/search/serving.db"))
    a = p.parse_args()
    compact(a.source, a.output)
