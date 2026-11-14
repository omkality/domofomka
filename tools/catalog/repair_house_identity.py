"""Разделить ошибочно объединённые номера по исходным строкам, сохранив остальные привязки."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from src.code_service.services.v2.localities import load_facts
from src.code_service.services.v2.normalize import house_key, text
from tools.catalog.build_catalog import (
    index_streets,
    link_codes,
    normalize_house_keys,
    source_street,
)


def repair(source: Path, legacy: Path, output: Path, locality_facts: Path | None = None) -> dict:
    """Создать проверяемое исправление отдельной копии поискового каталога."""
    normalized_house = lru_cache(maxsize=65536)(house_key)
    work = output.with_suffix(".building.db")
    subset = output.with_suffix(".source-subset.db")
    if source.resolve() == output.resolve() or output.exists() or work.exists() or subset.exists():
        raise FileExistsError("Use a fresh candidate; preserve the source snapshots")
    output.parent.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(output.parent).free < source.stat().st_size + 1073741824:
        raise ValueError("Not enough space for an independent candidate")
    with closing(sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)) as db:
        stats = json.loads(db.execute("SELECT value FROM metadata WHERE key='build'").fetchone()[0])
    if stats.get("normalization_schema", 0) > 7:
        raise ValueError("Repair house keys before source field alignment; use the preceding frozen snapshot")
    with legacy.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != stats["source_sha256"]:
            raise ValueError("Legacy source does not match the frozen catalog")
    shutil.copyfile(source, work)
    with closing(sqlite3.connect(work, uri=True)) as db:
        db.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-262144;")
        db.execute(
            "ATTACH DATABASE ? AS legacy",
            (f"file:{legacy.resolve().as_posix()}?mode=ro",),
        )
        before = db.execute("SELECT count(*) FROM provenance").fetchone()[0]
        changed = normalize_house_keys(db)
        db.execute("CREATE TEMP TABLE affected(id TEXT PRIMARY KEY)")
        db.executemany("INSERT INTO affected VALUES(?)", [(identity,) for identity in changed])
        # Also catch ranges previously linked to a plain-number GAR house.
        suspect = db.execute(r"""
            SELECT c.house,
                   p.building_id,
                   b.house
            FROM legacy.codes c
            JOIN provenance p ON p.source_id = c.id
            JOIN buildings b ON b.id = p.building_id
            """)
        mismatched = {identity for raw, identity, actual in suspect if normalized_house(raw) != actual}
        db.executemany(
            "INSERT OR IGNORE INTO affected VALUES(?)",
            [(identity,) for identity in mismatched],
        )
        db.row_factory = sqlite3.Row
        rows = db.execute(r"""
            SELECT c.*,
                   p.building_id AS previous_building,
                   p.resolution AS previous_resolution,
                   b.house AS target_house
            FROM affected a
            JOIN provenance p ON p.building_id = a.id
            JOIN legacy.codes c ON c.id = p.source_id
            JOIN buildings b ON b.id = a.id
            ORDER BY c.id
            """).fetchall()
        db.execute("DELETE FROM codes WHERE building_id IN(SELECT id FROM affected)")
        db.execute("DELETE FROM aliases WHERE building_id IN(SELECT id FROM affected)")
        moved = []
        for row in rows:
            identity = row["previous_building"]
            house = normalized_house(row["house"])
            if house != row["target_house"]:
                moved.append(row["id"])
                db.execute("DELETE FROM provenance WHERE source_id=?", (row["id"],))
                continue
            db.execute(
                "INSERT OR IGNORE INTO codes VALUES(?,?,?,?)",
                (
                    identity,
                    str(row["entrance"]),
                    str(row["code_type"]),
                    str(row["code"]),
                ),
            )
            for street in {
                text(row["street"]),
                source_street(row["street"], row["city"]),
            }:
                db.execute(
                    "INSERT OR IGNORE INTO aliases VALUES(?,?,?,?)",
                    (identity, text(row["city"]), street, house),
                )
        with closing(sqlite3.connect(subset)) as selected:
            schema = db.execute("SELECT sql FROM legacy.sqlite_master WHERE name='codes'").fetchone()[0]
            selected.execute(schema)
            wanted = set(moved)
            selected.executemany(
                "INSERT INTO codes VALUES(?,?,?,?,?,?,?,?)",
                [tuple(row)[:8] for row in rows if row["id"] in wanted],
            )
            selected.commit()
        relink_stats = {
            "legacy_rows": 0,
            "linked_groups": 0,
            "unresolved_groups": 0,
            "ambiguous_groups": 0,
        }
        link_codes(db, subset, relink_stats, load_facts(locality_facts))
        assert db.execute("SELECT count(*) FROM provenance").fetchone()[0] == before
        conflicts = 0
        for raw, actual in db.execute(r"""
            SELECT DISTINCT c.house,
                            b.house
            FROM legacy.codes c
            JOIN provenance p ON p.source_id = c.id
            JOIN buildings b ON b.id = p.building_id
            """):
            conflicts += normalized_house(raw) != actual
        if conflicts:
            raise ValueError(f"Unresolved source house conflicts: {conflicts}")
        index_streets(db)
        stats.update(
            normalization_schema=7,
            house_identity_version=2,
            house_keys_repaired=len(changed),
            house_source_rows_rebuilt=len(rows),
            house_source_rows_moved=len(moved),
            unique_codes=db.execute("SELECT count(*) FROM codes").fetchone()[0],
            buildings_with_codes=db.execute("SELECT count(DISTINCT building_id) FROM codes").fetchone()[0],
            street_documents=db.execute("SELECT count(*) FROM streets").fetchone()[0],
        )
        db.execute("UPDATE metadata SET value=? WHERE key='build'", (json.dumps(stats),))
        db.commit()
        assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    work.replace(output)
    report = {
        "changed_house_keys": len(changed),
        "rebuilt_source_rows": len(rows),
        "moved_source_rows": len(moved),
        "source_rows": before,
        "source_house_conflicts": conflicts,
        "normalization_schema": 7,
    }
    output.with_suffix(".summary.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--legacy", type=Path, default=Path("codes.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--locality-facts", type=Path)
    args = parser.parse_args()
    print(
        json.dumps(repair(args.catalog, args.legacy, args.output, args.locality_facts)),
        flush=True,
    )
