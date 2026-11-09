"""Извлечь действующие субъекты РФ из ADDR_OBJ, не читая XML домов."""

import argparse
import concurrent.futures
import hashlib
import json
from pathlib import Path

from tools.catalog.import_gar import records


def region_facts(directory: Path) -> dict:
    files = list(directory.glob("AS_ADDR_OBJ_2*.XML"))
    if len(files) != 1:
        raise ValueError("Expected one address-object snapshot per region")
    roots = [
        row
        for row in records(files[0])
        if row.get("LEVEL") == "1" and row.get("ISACTUAL") == "1" and row.get("ISACTIVE") == "1"
    ]
    with files[0].open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if len(roots) != 1:
        return {
            "region": directory.name,
            "skipped": "non_unique_current_root",
            "root_count": len(roots),
            "source": files[0].name,
            "source_sha256": digest,
        }
    row = roots[0]
    return {
        "region": directory.name,
        "object_id": int(row["OBJECTID"]),
        "guid": row["OBJECTGUID"],
        "name": row["NAME"],
        "kind": row["TYPENAME"],
        "source": files[0].name,
        "source_sha256": digest,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path("artifacts/gar_xml"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=range(1, 17), default=4)
    args = parser.parse_args()
    if args.output.exists():
        raise FileExistsError("Preserve the previous frozen facts")
    directories = sorted(p for p in args.source.iterdir() if p.is_dir() and p.name.isdigit())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ProcessPoolExecutor(max_workers=args.workers) as pool:
        result = list(pool.map(region_facts, directories))
    available = [r for r in result if "skipped" not in r]
    skipped = [r for r in result if "skipped" in r]
    args.output.write_text(
        json.dumps({"regions": available, "skipped": skipped}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "regions": len(available),
                "skipped": [{"region": r["region"], "root_count": r["root_count"]} for r in skipped],
            }
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
