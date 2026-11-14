"""Build a new index then switch an alias atomically; never clear a live index."""

import argparse
import json
import time

import httpx

from src.code_service.services.v2.engine import MAPPING, documents


def index(path: str, url: str, alias: str):
    """Загрузить подготовленные документы в отдельный поисковый индекс."""
    name = alias + "-" + str(time.time_ns())
    with httpx.Client(trust_env=False, timeout=120) as client:

        def request(method, path, body):
            """Выполнить ограниченный HTTP-запрос с выбранным сетевым бюджетом."""
            r = getattr(client, method)(url + path, json=body)
            if r.status_code >= 300:
                raise RuntimeError(f"OpenSearch {method} status={r.status_code}")
            return r.json()

        request("put", "/" + name, MAPPING)
        batch = []
        count = 0

        def flush():
            """Записать накопленную ограниченную пачку результатов."""
            data = "".join(
                json.dumps({"index": {"_index": name, "_id": d["id"]}})
                + "\n"
                + json.dumps(d, ensure_ascii=False)
                + "\n"
                for d in batch
            )
            r = client.post(
                url + "/_bulk", headers={"Content-Type": "application/x-ndjson"}, content=data.encode("utf8")
            )
            if r.status_code != 200 or r.json().get("errors"):
                raise RuntimeError("OpenSearch bulk failed; alias unchanged")

        for doc in documents(path):
            batch.append(doc)
            count += 1
            if len(batch) >= 2000:
                flush()
                batch = []
            if count % 100000 == 0:
                print(json.dumps({"indexed": count}), flush=True)
        if batch:
            flush()
        request("put", "/" + name + "/_settings", {"index": {"refresh_interval": "1s"}})
        request("post", "/" + name + "/_refresh", {})
        result = request("post", "/" + name + "/_count", {})
        assert result["count"] == count
        request(
            "post",
            "/_aliases",
            {
                "actions": [
                    {"remove": {"index": "*", "alias": alias, "must_exist": False}},
                    {"add": {"index": name, "alias": alias}},
                ]
            },
        )
        print(json.dumps({"complete": True, "documents": count, "index": name, "alias": alias}), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", default="artifacts/search/catalog.db")
    p.add_argument("--url", default="http://127.0.0.1:19200")
    p.add_argument("--alias", default="domofomka-buildings")
    a = p.parse_args()
    index(a.catalog, a.url, a.alias)
