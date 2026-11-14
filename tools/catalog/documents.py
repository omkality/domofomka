"""Потоковый экспорт сборочного SQLite-каталога для поисковых экспериментов."""

import json
import zlib
from collections.abc import Iterator

from tools.catalog.sqlite_catalog import SqliteCatalog


def documents(path: str, *, structured: bool = False) -> Iterator[dict]:
    """Прочитать подготовленные поисковые документы каталога."""
    catalog = SqliteCatalog(path)
    with catalog.connect() as db:
        compact = catalog._compact(db)
        for row in db.execute("SELECT * FROM streets"):
            if compact:
                packed = db.execute(
                    r"""
                    SELECT
                      data
                    FROM
                      street_houses
                    WHERE
                      id = ?
                    """,
                    (row["id"],),
                ).fetchone()[0]
                houses = sorted({house for b in json.loads(zlib.decompress(packed)) for house in b["houses"]})
            else:
                houses = [
                    r[0]
                    for r in db.execute(
                        r"""
                    SELECT DISTINCT
                      house
                    FROM
                      buildings
                    WHERE
                      city_key = ?
                      AND street_key = ?
                    ORDER BY
                      house
                    """,
                        (row["city_key"], row["street_key"]),
                    )
                ]
            document = {"id": row["id"], "words": row["words"], "house": houses}
            if structured:
                document.update(
                    city=row["city_key"],
                    street=row["street_key"],
                    coded_house=(
                        [
                            r[0]
                            for r in db.execute(
                                r"""
                    SELECT DISTINCT
                      house
                    FROM
                      buildings
                    WHERE
                      city_key = ?
                      AND street_key = ?
                      AND EXISTS
                      (
                        SELECT
                          1
                        FROM
                          codes AS c
                        WHERE
                          c.building_id = buildings.id
                      )
                    """,
                                (row["city_key"], row["street_key"]),
                            )
                        ]
                        if not compact or row["has_codes"]
                        else []
                    ),
                )
            yield document
