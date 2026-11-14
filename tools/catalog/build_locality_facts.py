"""Сверить административные и муниципальные пути населённых пунктов в официальном ГАР."""

import argparse
import json
from pathlib import Path

from src.code_service.services.v2.normalize import address_words, text
from tools.catalog.import_gar import records


def build_region(source: Path, region: str):
    """Подготовить адресные факты одного региона из действующих объектов ГАР."""

    def family(name):
        """Прочитать родительскую цепочку адресного объекта."""
        files = list((source / region).glob("AS_" + name + "_2*.XML"))
        if len(files) != 1:
            raise ValueError("Expected one snapshot file for " + region + "/" + name)
        return files[0]

    nodes = {}
    for row in records(family("ADDR_OBJ")):
        if row.get("ISACTUAL") == "1" and row.get("ISACTIVE") == "1":
            nodes[int(row["OBJECTID"])] = (row["NAME"], row["TYPENAME"], int(row["LEVEL"]))
    cities = {oid for oid, node in nodes.items() if node[2] in (5, 6) or (node[2] == 2 and node[1].rstrip(".") == "г")}
    parents = {}
    for row in records(family("ADM_HIERARCHY")):
        oid = int(row["OBJECTID"])
        if row.get("ISACTIVE") == "1" and oid in nodes:
            parents[oid] = int(row["PARENTOBJID"])
    result = []
    for row in records(family("MUN_HIERARCHY")):
        oid = int(row["OBJECTID"])
        if row.get("ISACTIVE") != "1" or oid not in cities:
            continue
        admin = []
        current: int | None = oid
        seen = set()
        while current and current not in seen:
            seen.add(current)
            if current in nodes:
                admin.append(nodes[current][0])
            current = parents.get(current)
        municipal = [nodes[int(part)][0] for part in row["PATH"].split(".") if int(part) in nodes]
        if not admin or not municipal:
            continue
        result.append(
            {
                "region": region,
                "object_id": oid,
                "city_key": text(nodes[oid][0]),
                "kind": nodes[oid][1],
                "admin_words": sorted(set(address_words(" ".join(admin)).split())),
                "municipal_words": sorted(set(address_words(" ".join(municipal)).split())),
            }
        )
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", type=Path, default=Path("artifacts/gar_xml"))
    p.add_argument("--regions", nargs="+", default=["50"])
    p.add_argument("--output", type=Path, default=Path("artifacts/search/locality-facts.jsonl"))
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError("Keep the previous snapshot; choose another output")
    a.output.parent.mkdir(parents=True, exist_ok=True)
    work = a.output.with_suffix(".building.jsonl")
    with work.open("w", encoding="utf8") as stream:
        for region in a.regions:
            rows = build_region(a.source, region)
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            stream.flush()
            print(json.dumps({"region": region, "localities": len(rows)}), flush=True)
    work.replace(a.output)
