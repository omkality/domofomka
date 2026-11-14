"""Атомарный обратимый патч каталога dev. Никаких обращений к VK или очереди.

Исполняется отдельным процессом из проверенного API-образа; административный DSN
передаётся только через временное окружение. Дамп кодов в Git не помещается.
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
from pathlib import Path

import psycopg
from psycopg import sql

TABLES = {
    "buildings": (
        ["id", "guid", "display", "city", "street", "house", "street_key", "city_key", "region", "lineage"],
        ["id"],
    ),
    "codes": (["building_id", "entrance", "kind", "code"], ["building_id", "entrance", "kind", "code"]),
    "aliases": (["building_id", "city_key", "street_key", "house"], ["building_id", "city_key", "street_key", "house"]),
    "streets": (["id", "city_key", "street_key", "words", "has_codes"], ["id"]),
    "street_houses": (["id", "data"], ["id"]),
    "alias_search": (["seq", "building_id", "house", "words"], ["seq"]),
    "provenance": (["source_id", "building_id", "resolution"], ["source_id"]),
    "metadata": (["key", "value"], ["key"]),
    "vocabulary": (["term"], ["term"]),
}


def manifest() -> dict:
    """Прочитать описание проверенного обратимого патча каталога."""
    path = Path(__file__).with_name("catalog_release.json")
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"enabled": False}


def row_values(table: str, row: list | None) -> list | None:
    """Привести строку патча к значениям заданной таблицы."""
    if row is not None and table == "street_houses":
        return [row[0], base64.b64decode(row[1])]
    return row


def migrate(dsn: str, patch: dict, reverse: bool = False) -> dict:
    """Применить либо обратить проверенный патч опубликованных адресных связей."""
    source = patch["target_revision"] if reverse else patch["source_revision"]
    target = patch["source_revision"] if reverse else patch["target_revision"]
    before, after = (1, 0) if reverse else (0, 1)
    changes = patch["changes"]
    if set(changes) - TABLES.keys():
        raise ValueError("Unknown catalog table")
    with psycopg.connect(dsn, connect_timeout=5) as db:
        db.execute("SET LOCAL statement_timeout='180s'")
        db.execute("SET LOCAL lock_timeout='5s'")
        db.execute("SELECT pg_advisory_xact_lock(20261109,1)")
        revision = db.execute("SELECT value FROM metadata WHERE key='source_catalog_sha256'").fetchone()[0]
        if revision == target:
            return {"changed": False, "revision": target}
        if revision != source:
            raise ValueError("Unexpected catalog base revision")
        # Проверка внешних ключей при объединении домов иначе перечитывает миллион
        # строк для каждого удаляемого ID. Индексы нужны только этой транзакции;
        # после операции они удаляются и не занимают диск постоянно.
        indexed_references = (
            ("provenance", "alias_search") if any(pair[after] is None for pair in changes.get("buildings", [])) else ()
        )
        for table in indexed_references:
            db.execute(
                sql.SQL("CREATE INDEX {} ON {} (building_id)").format(
                    sql.Identifier("catalog_patch_" + table + "_building"), sql.Identifier(table)
                )
            )
        # Сравнение наборами на сервере, без сетевого запроса на каждую строку.
        for table, pairs in changes.items():
            columns, keys = TABLES[table]
            projection = sql.SQL(",").join(map(sql.Identifier, columns))
            indexes = [columns.index(k) for k in keys]
            expected_name, keys_name = "patch_expected_" + table, "patch_keys_" + table
            db.execute(
                sql.SQL("CREATE TEMP TABLE {} (LIKE {}) ON COMMIT DROP").format(
                    sql.Identifier(expected_name), sql.Identifier(table)
                )
            )
            db.execute(
                sql.SQL("CREATE TEMP TABLE {} ON COMMIT DROP AS SELECT {} FROM {} WITH NO DATA").format(
                    sql.Identifier(keys_name), sql.SQL(",").join(map(sql.Identifier, keys)), sql.Identifier(table)
                )
            )
            with db.cursor().copy(
                sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(expected_name), projection)
            ) as copy:
                for pair in pairs:
                    if pair[before] is not None:
                        copy.write_row(row_values(table, pair[before]))
            with db.cursor().copy(sql.SQL("COPY {} FROM STDIN").format(sql.Identifier(keys_name))) as copy:
                for pair in pairs:
                    candidate = pair[before] if pair[before] is not None else pair[after]
                    copy.write_row([candidate[i] for i in indexes])
            db.execute(
                sql.SQL("CREATE UNIQUE INDEX ON {} ({})").format(
                    sql.Identifier(keys_name), sql.SQL(",").join(map(sql.Identifier, keys))
                )
            )
            for temporary in (expected_name, keys_name):
                db.execute(sql.SQL("ANALYZE {}").format(sql.Identifier(temporary)))
            current = sql.SQL("SELECT {} FROM {} a JOIN {} k USING ({})").format(
                sql.SQL(",").join(sql.SQL("a.{}").format(sql.Identifier(c)) for c in columns),
                sql.Identifier(table),
                sql.Identifier(keys_name),
                sql.SQL(",").join(map(sql.Identifier, keys)),
            )
            expected_rows = sql.SQL("SELECT {} FROM {}").format(projection, sql.Identifier(expected_name))
            if db.execute(
                sql.SQL("({} EXCEPT {}) UNION ALL ({} EXCEPT {}) LIMIT 1").format(
                    current, expected_rows, expected_rows, current
                )
            ).fetchone():
                raise ValueError("Catalog row differs from the reviewed patch")
        for table in ("buildings", "streets"):
            columns, keys = TABLES[table]
            for pair in changes.get(table, []):
                row = row_values(table, pair[after])
                if row is None:
                    continue
                db.execute(
                    sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT ({}) DO UPDATE SET {}").format(
                        sql.Identifier(table),
                        sql.SQL(",").join(map(sql.Identifier, columns)),
                        sql.SQL(",").join(sql.Placeholder() for _ in columns),
                        sql.SQL(",").join(map(sql.Identifier, keys)),
                        sql.SQL(",").join(
                            sql.SQL("{}=EXCLUDED.{}").format(sql.Identifier(c), sql.Identifier(c))
                            for c in columns
                            if c not in keys
                        ),
                    ),
                    row,
                )
        for pair in changes.get("codes", []):
            row = pair[after]
            if row:
                db.execute("INSERT INTO code_sources VALUES(%s) ON CONFLICT DO NOTHING", (row[2],))
                db.execute("INSERT INTO entrances VALUES(%s,%s) ON CONFLICT DO NOTHING", (row[0], row[1]))
        for table in ("codes", "aliases", "alias_search", "provenance", "street_houses", "metadata", "vocabulary"):
            columns, keys = TABLES[table]
            pairs = changes.get(table, [])
            where = sql.SQL(" AND ").join(sql.SQL("{}=%s").format(sql.Identifier(k)) for k in keys)
            with db.cursor() as cursor:
                previous = [row_values(table, pair[before]) for pair in pairs if pair[before] is not None]
                if previous:
                    cursor.executemany(
                        sql.SQL("DELETE FROM {} WHERE {}").format(sql.Identifier(table), where),
                        [[row[columns.index(k)] for k in keys] for row in previous],
                    )
                rows = [row_values(table, pair[after]) for pair in pairs if pair[after] is not None]
                if rows:
                    with cursor.copy(
                        sql.SQL("COPY {} ({}) FROM STDIN").format(
                            sql.Identifier(table), sql.SQL(",").join(map(sql.Identifier, columns))
                        )
                    ) as copy:
                        for row in rows:
                            copy.write_row(row)
        if changes.get("codes"):
            db.execute(r"""
                DELETE
                FROM entrances e
                WHERE NOT EXISTS
                        (SELECT 1
                         FROM codes c
                         WHERE c.building_id = e.building_id
                             AND c.entrance = e.number)
                """)
        for table in ("buildings", "streets"):
            for pair in changes.get(table, []):
                if pair[after] is None:
                    db.execute(sql.SQL("DELETE FROM {} WHERE id=%s").format(sql.Identifier(table)), (pair[before][0],))
        db.execute("UPDATE metadata SET value=%s WHERE key='source_catalog_sha256'", (target,))
        expected = patch["source_counts"] if reverse else patch["target_counts"]
        for table, count in expected.items():
            if table not in TABLES:
                raise ValueError("Unknown expected table")
            query = sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            if table == "metadata":
                query += sql.SQL(
                    " WHERE key NOT IN ('source_catalog_sha256','catalog_schema_version','publication_state')"
                )
            actual = db.execute(query).fetchone()[0]
            if actual != count:
                raise ValueError("Catalog row count differs from review")
        for table in indexed_references:
            db.execute(sql.SQL("DROP INDEX {}").format(sql.Identifier("catalog_patch_" + table + "_building")))
    return {"changed": True, "revision": target}


def main() -> None:
    """Атомарный обратимый патч каталога dev. Никаких обращений к VK или очереди."""
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["inspect", "apply", "reverse"])
    parser.add_argument("--patch", type=Path)
    args = parser.parse_args()
    if args.action == "inspect":
        print(json.dumps(manifest()))  # noqa: T201
        return
    if os.environ.get("APP_ENVIRONMENT") != "development" or os.environ.get("EXPECTED_VK_GROUP_ID") != "223949627":
        raise ValueError("Catalog mutation restricted to dedicated dev")
    data = args.patch.read_bytes()
    release = manifest()
    if not release.get("enabled") or hashlib.sha256(data).hexdigest() != release["patch_sha256"]:
        raise ValueError("Unexpected patch digest")
    with gzip.open(args.patch, "rb") as stream:
        raw = stream.read(67108865)
    if len(raw) > 67108864:
        raise ValueError("Patch exceeds memory budget")
    result = migrate(os.environ["CATALOG_ADMIN_DSN"], json.loads(raw), args.action == "reverse")
    print(json.dumps(result))  # noqa: T201


if __name__ == "__main__":
    import sys

    try:
        main()
    except Exception as error:
        sys.stderr.write("Catalog update failed: " + type(error).__name__ + "\n")
        sys.exit(1)
