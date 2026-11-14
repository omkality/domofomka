"""Publish a small serving catalog. Full GAR GUID/provenance stays in the offline build.

Code-bearing identities retain exact GUIDs. Known houses without codes are represented
as GAR address keys, never invented physical GUIDs. No geography is inferred.
"""

import argparse
import json
import sqlite3
import zlib
from pathlib import Path

from src.infrastructure.database.resources import read_sql
from tools.catalog.refresh_aliases import refresh


def compact(source: Path, output: Path) -> None:
    """Собрать компактный каталог со сжатыми домами без кодов."""
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
    db.executescript(read_sql("compact-sqlite.sql"))
    coded = {(r[0], r[1]) for r in db.execute(r"""
        SELECT DISTINCT
          city_key,
          street_key
        FROM
          buildings
        """)}
    count = 0
    houses = 0
    for street in src.execute("SELECT * FROM streets"):
        groups = {}
        row_count = 0
        for house in src.execute(
            r"""
            SELECT
              display,
              house,
              city,
              street,
              region,
              lineage
            FROM
              buildings
            WHERE
              city_key = ?
              AND street_key = ?
            """,
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
    refresh(db)
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
