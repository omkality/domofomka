"""Индекс кандидатов для v1, сохраняющий старые проверки и порядок исходных строк."""

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from functools import lru_cache
from pathlib import Path

from src.telemetry import measured, span


@lru_cache(maxsize=2)
@measured("search.v1.index_validation")
def compatible(source: str, index: str, source_stamp: tuple, index_stamp: tuple) -> bool:
    with closing(sqlite3.connect(f"file:{Path(index).resolve().as_posix()}?mode=ro", uri=True)) as db:
        metadata = json.loads(db.execute("SELECT value FROM metadata WHERE key='source'").fetchone()[0])
    with open(source, "rb") as stream:
        return metadata["sha256"] == hashlib.file_digest(stream, "sha256").hexdigest()


@measured("search.v1.index")
def lookup(message: str, source_path: str, index_path: str) -> dict | None:
    from src.services.codes import address_exists, street_types

    source, index = Path(source_path), Path(index_path)
    if not index.is_file():
        return None
    stamps = [(p.stat().st_ino, p.stat().st_size, p.stat().st_mtime_ns) for p in (source, index)]
    if not compatible(str(source), str(index), *stamps):
        return None
    parts = re.split(r"[^а-я]", message)
    for kind in street_types:
        if kind in parts:
            parts.remove(kind)
    if not parts:
        return {}
    word = max(parts, key=len).lower()
    with closing(sqlite3.connect(f"file:{index.resolve().as_posix()}?mode=ro", uri=True)) as db:
        if len(word) >= 3:
            groups = db.execute(
                "SELECT g.id,g.city,g.kind,g.street,g.house FROM legacy_search f "
                "JOIN groups g ON g.id=f.rowid WHERE legacy_search MATCH ? ORDER BY g.id",
                ('"' + word.replace('"', '""') + '"',),
            )
        else:
            groups = db.execute("SELECT id,city,kind,street,house FROM groups ORDER BY id")
        with span("search.v1.candidates_match"):
            matching = [
                r
                for r in groups
                if (word in r[1].lower() or word in r[3].lower()) and address_exists(message, r[1], r[3], r[4], r[2])
            ]
        if not matching:
            return {}
        city = min((r[1] for r in matching), key=len)
        ids = [r[0] for r in matching if city in r[1]]
        source_ids = []
        for gid in ids:
            source_ids.extend(r[0] for r in db.execute("SELECT source_id FROM source_rows WHERE group_id=?", (gid,)))
    result = {}
    with (
        span("search.v1.read_codes"),
        closing(sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)) as db,
    ):
        rows = []
        for offset in range(0, len(source_ids), 500):
            batch = source_ids[offset : offset + 500]
            rows.extend(db.execute("SELECT * FROM codes WHERE id IN (" + ",".join("?" for _ in batch) + ")", batch))
        for _, _, kind, street, house, entrance, code_type, code in sorted(rows, key=lambda r: r[0]):
            result.setdefault("address", f"{city}, {kind} {street}, дом {house}")
            result.setdefault("data", {}).setdefault(entrance, []).append((code, code_type))
    return result
