"""Небольшой обратимый патч между двумя замороженными сборками каталога.

Данные кодов остаются в игнорируемом артефакте; в Git публикуются только хеши.
"""

import argparse
import base64
import gzip
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from src.code_service.services.v2.catalog_update import TABLES


def rows(db, table):
    """Прочитать строки указанной таблицы для сравнения снимков каталога."""
    columns, keys = TABLES[table]
    if table == "alias_search":
        return db.execute("SELECT rowid,building_id,house,words FROM alias_search ORDER BY rowid")
    return db.execute("SELECT " + ",".join(columns) + " FROM " + table + " ORDER BY " + ",".join(keys))


def serial(row):
    """Привести строку каталога к детерминированному представлению патча."""
    return [base64.b64encode(v).decode() if isinstance(v, bytes) else v for v in row] if row else None


def difference(left, right, table):
    """Получить изменившиеся строки двух проверяемых снимков."""
    columns, keys = TABLES[table]
    indexes = [columns.index(k) for k in keys]
    a, b = iter(rows(left, table)), iter(rows(right, table))
    old, new = next(a, None), next(b, None)
    result, old_count, new_count = [], 0, 0
    while old is not None or new is not None:
        ka = tuple(old[i] for i in indexes) if old is not None else None
        kb = tuple(new[i] for i in indexes) if new is not None else None
        if old is not None and (new is None or ka < kb):
            result.append([serial(old), None])
            old_count += 1
            old = next(a, None)
        elif new is not None and (old is None or kb < ka):
            result.append([None, serial(new)])
            new_count += 1
            new = next(b, None)
        else:
            if old != new:
                result.append([serial(old), serial(new)])
            old_count += 1
            new_count += 1
            old = next(a, None)
            new = next(b, None)
    return result, old_count, new_count


def build(old_catalog: Path, new_catalog: Path, old_full: Path, new_full: Path, output: Path) -> dict:
    """Подготовить отдельный проверяемый артефакт из исходных данных."""
    changes, old_counts, new_counts = {}, {}, {}
    for old, new, tables in [
        (old_catalog, new_catalog, [t for t in TABLES if t != "provenance"]),
        (old_full, new_full, ["provenance"]),
    ]:
        with (
            closing(sqlite3.connect(f"file:{old.resolve().as_posix()}?mode=ro", uri=True)) as a,
            closing(sqlite3.connect(f"file:{new.resolve().as_posix()}?mode=ro", uri=True)) as b,
        ):
            for table in tables:
                pairs, before, after = difference(a, b, table)
                old_counts[table] = before
                new_counts[table] = after
                if pairs:
                    changes[table] = pairs
                print(json.dumps({"table": table, "changed": len(pairs)}), flush=True)

    def digest(path):
        """Вычислить контрольную сумму проверяемого файла потоковым чтением."""
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()

    patch = {
        "source_revision": digest(old_catalog),
        "target_revision": digest(new_catalog),
        "source_counts": old_counts,
        "target_counts": new_counts,
        "changes": changes,
    }
    raw = json.dumps(patch, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > 67108864:
        raise ValueError("Patch exceeds reviewed client memory budget")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise FileExistsError("Preserve frozen patch")
    data = gzip.compress(raw, mtime=0)
    output.write_bytes(data)
    manifest = {
        "enabled": True,
        "source_revision": patch["source_revision"],
        "target_revision": patch["target_revision"],
        "patch_sha256": hashlib.sha256(data).hexdigest(),
        "patch_bytes": len(data),
        "raw_bytes": len(raw),
        "changed_rows": {t: len(v) for t, v in changes.items()},
    }
    output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    for name in ["old-catalog", "new-catalog", "old-full", "new-full", "output"]:
        p.add_argument("--" + name, type=Path, required=True)
    args = p.parse_args()
    print(json.dumps(build(args.old_catalog, args.new_catalog, args.old_full, args.new_full, args.output)))
