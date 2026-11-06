"""Сохранить опубликованный дамп каталога на ПК, оставив две последние версии."""

import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path


def archive(source: Path, output: Path, expected: str) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Expected SHA-256 required")
    digest = hashlib.sha256()
    with source.open("rb") as stream:
        if stream.read(5) != b"PGDMP":
            raise ValueError("Not a PostgreSQL custom dump")
        stream.seek(0)
        for block in iter(lambda: stream.read(1048576), b""):
            digest.update(block)
    if digest.hexdigest() != expected:
        raise ValueError("Catalog digest mismatch")
    output.mkdir(parents=True, exist_ok=True)
    target = output / ("catalog-" + expected + ".dump")
    if not target.exists():
        part = target.with_suffix(".part")
        try:
            shutil.copyfile(source, part)
            with part.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
                    raise ValueError("Copied catalog digest mismatch")
            part.replace(target)
        finally:
            part.unlink(missing_ok=True)
    with target.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
            raise ValueError("Existing catalog copy is corrupt")
    target.touch()
    for path in sorted(output.glob("catalog-*.dump"), key=lambda p: p.stat().st_mtime, reverse=True)[2:]:
        if re.fullmatch(r"catalog-[0-9a-f]{64}\.dump", path.name) and not path.is_symlink():
            path.unlink()
    return {"bytes": target.stat().st_size, "sha256": expected, "versions_retained": 2}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, default=Path("artifacts/private/backups/catalog"))
    args = parser.parse_args()
    print(json.dumps(archive(args.source, args.output, args.sha256)))
