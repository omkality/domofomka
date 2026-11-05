"""Build a new index then switch an alias atomically; never clear a live index."""

import argparse
import collections.abc  # noqa: F401 - initializes collections for pyreqwest 0.10 on Python 3.12.
import json
import time
from datetime import timedelta

from pyreqwest.client import SyncClientBuilder

from src.search.engine import MAPPING, documents


def index(path: str, url: str, alias: str):
    name = alias + "-" + str(time.time_ns())
    with SyncClientBuilder().no_proxy().timeout(timedelta(seconds=120)).build() as client:

        def request(method, path, body):
            r = getattr(client, method)(url + path).body_json(body).build().send()
            if r.status >= 300:
                raise RuntimeError(f"OpenSearch {method} status={r.status}")
            return r.json()

        request("put", "/" + name, MAPPING)
        batch = []
        count = 0

        def flush():
            data = "".join(
                json.dumps({"index": {"_index": name, "_id": d["id"]}})
                + "\n"
                + json.dumps(d, ensure_ascii=False)
                + "\n"
                for d in batch
            )
            r = (
                client.post(url + "/_bulk")
                .headers({"Content-Type": "application/x-ndjson"})
                .body_bytes(data.encode("utf8"))
                .build()
                .send()
            )
            if r.status != 200 or r.json().get("errors"):
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
