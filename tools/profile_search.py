"""Небольшой замер HTTP-поиска: без отправки сообщений VK и без геокодирования."""

import argparse
import collections.abc
import concurrent.futures
import json
import time

import httpx


def measure(url: str, version: str, queries: collections.abc.Iterable[str], workers: int) -> dict:
    def request(query: str) -> tuple:
        with httpx.Client(trust_env=False, timeout=30) as client:
            started = time.perf_counter()
            response = client.get(f"{url}/api/{version}/codes/msg", params={"message": query})
            data = response.json()
            return response.status_code, round((time.perf_counter() - started) * 1000, 3), bool(data.get("data"))

    values = list(queries)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        rows = list(pool.map(request, values))
    times = sorted(r[1] for r in rows)
    return {
        "api": version,
        "requests": len(rows),
        "workers": workers,
        "http_errors": sum(r[0] != 200 for r in rows),
        "p50_ms": times[len(times) // 2],
        "p95_ms": times[int(len(times) * 0.95)],
        "p99_ms": times[min(len(times) - 1, int(len(times) * 0.99))],
    }


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:8000")
    p.add_argument("--requests", type=int, default=60)
    p.add_argument("--workers", type=int, choices=range(1, 5), default=2)
    a = p.parse_args()
    if not 10 <= a.requests <= 1000:
        p.error("Use between 10 and 1000 requests")
    queries = [
        "мичурина 1а",
        "мск Перерва 62к1",
        "СПБ Плесецкая 10",
        "Москва Коломенская 2",
        "Москва улица Крупской 4к2",
    ]
    for version in ("v1", "v2"):
        print(
            json.dumps(measure(a.url, version, (queries[i % len(queries)] for i in range(a.requests)), a.workers)),
            flush=True,
        )
