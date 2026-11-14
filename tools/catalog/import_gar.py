"""Stream only active address objects, houses and their parents; resume per region.

No apartments, rooms, plots or personal data. Each worker owns its own database.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import sqlite3
import time
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

from src.code_service.services.v2.normalize import house_key, text
from src.infrastructure.database.resources import read_sql


def locality(chain: list[tuple[str, str, int]], region: str) -> str:
    # Cities of regional significance may be level 2, not level 5.
    # A district/urban district name is never a substitute for a settlement.
    """Выделить населённый пункт из последовательности адресных объектов."""
    settlement = next((n for n in chain if n[2] in (5, 6)), None)
    if settlement is None:
        settlement = next((n for n in chain if n[2] == 2 and n[1].rstrip(".") in {"г", "город"}), None)
    federal = {"77": "Москва", "78": "Санкт-Петербург", "92": "Севастополь"}
    return settlement[0] if settlement else federal.get(region, next((n[0] for n in reversed(chain)), ""))


def records(path: Path) -> Iterator[dict[str, str]]:
    """Потоково прочитать записи XML без загрузки файла целиком в память."""
    context = ET.iterparse(path, events=("start", "end"))
    _, root = next(context)
    for event, element in context:
        if event == "end" and element is not root:
            yield element.attrib
            element.clear()
            root.clear()


def region_job(args: tuple[str, str, str]) -> dict:
    """Импортировать один регион в отдельный локальный артефакт."""
    source, output, region = args
    target = Path(output) / f"gar-{region}.db"
    if target.exists():
        db = sqlite3.connect(target)
        try:
            if db.execute(r"""
                SELECT
                  value
                FROM
                  metadata
                WHERE
                  key = 'schema'
                """).fetchone() == ("4",):
                db.close()
                return {"region": region, "resumed": True}
        except sqlite3.Error:
            pass
        db.close()
    work = target.with_suffix(".building.db")
    if work.exists():
        work.unlink()
    db = sqlite3.connect(work)
    db.executescript(read_sql("gar-region-sqlite.sql"))
    root = Path(source)
    directory = root / region

    def file(family: str) -> Path:
        """Найти региональный файл нужного вида ГАР."""
        return next(directory.glob(f"AS_{family}_2*.XML"))

    count = 0
    for row in records(file("ADDR_OBJ")):
        if row.get("ISACTUAL") == "1" and row.get("ISACTIVE") == "1":
            db.execute(
                r"""
                INSERT OR REPLACE INTO nodes VALUES (?, ?, ?, ?)
                """,
                (int(row["OBJECTID"]), row["NAME"], row["TYPENAME"], int(row["LEVEL"])),
            )
    db.commit()
    additions = {"1": "к", "2": "с", "3": "соор", "4": "лит"}
    for row in records(file("HOUSES")):
        if row.get("ISACTUAL") != "1" or row.get("ISACTIVE") != "1":
            continue
        number = row.get("HOUSENUM", "")
        for n in ("1", "2"):
            if row.get("ADDNUM" + n):
                number += additions.get(row.get("ADDTYPE" + n), "тип" + str(row.get("ADDTYPE" + n))) + row["ADDNUM" + n]
        if not number:
            continue
        db.execute(
            r"""
            INSERT OR REPLACE INTO houses VALUES (?, ?, ?)
            """,
            (int(row["OBJECTID"]), row["OBJECTGUID"], number),
        )
        count += 1
        if count % 50000 == 0:
            db.commit()
    db.commit()
    wanted = {r[0] for r in db.execute(r"""
        SELECT
          id
        FROM
          houses
        UNION
        SELECT
          id
        FROM
          nodes
        """)}
    for row in records(file("ADM_HIERARCHY")):
        if row.get("ISACTIVE") == "1" and int(row["OBJECTID"]) in wanted:
            db.execute(
                r"""
                INSERT OR REPLACE INTO parents VALUES (?, ?)
                """,
                (int(row["OBJECTID"]), int(row["PARENTOBJID"])),
            )
    db.commit()
    nodes = {r[0]: r[1:] for r in db.execute("SELECT * FROM nodes")}
    parents = dict(db.execute("SELECT * FROM parents"))
    del wanted
    inserted = 0
    for object_id, guid, number in db.execute("SELECT * FROM houses"):
        chain, current, visited = [], parents.get(object_id), set()
        while current and current not in visited:
            visited.add(current)
            if current in nodes:
                chain.append(nodes[current])
            current = parents.get(current)
        if not chain:
            continue
        street_node = next((n for n in chain if n[2] == 8), next((n for n in chain if n[2] == 7), None))
        if not street_node:
            street_node = next((n for n in chain if n[2] >= 6), None)
        if not street_node:
            continue
        city = locality([n for n in chain if n is not street_node], region)
        street = street_node[0]
        display = ", ".join(dict.fromkeys([city, street_node[1] + " " + street, "дом " + number]))
        lineage = " ".join(dict.fromkeys(text(n[0]) for n in chain if n is not street_node))
        db.execute(
            r"""
            INSERT OR REPLACE INTO buildings VALUES (
              ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            ("gar:" + guid, guid, display, city, street, house_key(number), text(street), text(city), region, lineage),
        )
        inserted += 1
    db.execute(
        r"""
        INSERT INTO metadata VALUES ('complete', ?)
        """,
        (str(time.time()),),
    )
    db.execute(r"""
        INSERT INTO metadata VALUES ('schema', '4')
        """)
    db.executescript("DROP TABLE houses; DROP TABLE parents; DROP TABLE nodes; VACUUM;")
    db.commit()
    db.close()
    work.replace(target)
    return {"region": region, "houses": count, "searchable_buildings": inserted, "bytes": target.stat().st_size}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--source", type=Path, default=Path("artifacts/gar_xml"))
    p.add_argument("--output", type=Path, default=Path("artifacts/catalog"))
    p.add_argument("--regions", nargs="+", default=["77", "50", "78"])
    p.add_argument("--workers", type=int, default=3)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    regions = sorted(x.name for x in a.source.iterdir() if x.is_dir()) if a.regions == ["all"] else a.regions
    with concurrent.futures.ProcessPoolExecutor(max_workers=a.workers) as pool:
        for result in pool.map(region_job, [(str(a.source), str(a.output), r) for r in regions]):
            print(json.dumps(result), flush=True)
